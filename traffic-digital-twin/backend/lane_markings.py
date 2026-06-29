"""
lane_markings.py — detect lane-line positions and dashed-marking period from a frame.

Step 3: detection + logging only (no solver changes).
Step 4 will consume the returned observations to anchor focal/scale.

Two outputs:
  lane_width_obs  — list[(row_y, lane_width_px)]: lateral lane spacing at each sample row.
  dash_period_obs — list[(row_center, period_px)]: longitudinal dashing period per lane strip.

Real-world mapping (Step 4):
  lane_width_px  / lane_w_m(road_rank) → mpp_lateral at row_y
  dash_period_px / mark_period_m(road_rank) → mpp_longitudinal at row_center
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

import cv2
import numpy as np

from config import (
    MARK_PERIOD_M,
    MARK_PAINT_M,
    MARK_GAP_M,
    MARK_WIDTH_M,
    MARK_PERIOD_TOL,
)

logger = logging.getLogger(__name__)

# ── 검출 파라미터 ──────────────────────────────────────────────────────────────
_WHITE_THRESH   = 200      # 흰색 판정 임계값 (0–255 gray)
_STRIP_HALF_W   = 8        # 점선 주기 검출용 수직 스트립 반폭 (px)
_MIN_PERIOD_ROW = 6        # 자기상관 피크 최소 lag (px)
_SAMPLE_ROWS    = 5        # 차선폭 샘플링 행 수
_MIN_LINE_WIDTH_PX = 3     # 흰색 연속 구간 최소 폭 (노이즈 제거)
_MIN_LANE_WIDTH_PX = 20    # 유효 차선폭 최소 px (너무 좁은 쌍 제거)
_MAX_LANE_WIDTH_PX_FRAC = 0.6  # frame_w 대비 최대 차선폭 비율


@dataclass
class LaneMarkingResult:
    lane_width_obs: list[tuple[float, float]] = field(default_factory=list)
    dash_period_obs: list[tuple[float, float]] = field(default_factory=list)
    detected_line_xs: list[list[float]] = field(default_factory=list)  # per-row line x positions
    mark_period_m: float = 8.0
    mark_paint_m: float = 3.0
    mark_gap_m: float = 5.0
    n_lanes_detected: int = 0


def _marking_constants(road_rank: str) -> tuple[float, float, float]:
    """(period_m, paint_m, gap_m) for given road_rank."""
    key = road_rank if road_rank in MARK_PERIOD_M else "default"
    return MARK_PERIOD_M[key], MARK_PAINT_M[key], MARK_GAP_M[key]


def _white_column_peaks(row_strip: np.ndarray, min_width: int = _MIN_LINE_WIDTH_PX) -> list[float]:
    """1행 gray 배열에서 흰색(>_WHITE_THRESH) 연속 구간의 중심 x좌표 목록 반환."""
    mask = row_strip > _WHITE_THRESH
    peaks: list[float] = []
    start = None
    for i, v in enumerate(mask):
        if v and start is None:
            start = i
        elif not v and start is not None:
            run = i - start
            if run >= min_width:
                peaks.append(start + run / 2.0)
            start = None
    if start is not None:
        run = len(mask) - start
        if run >= min_width:
            peaks.append(start + run / 2.0)
    return peaks


def _autocorr_period(signal: np.ndarray, min_lag: int = _MIN_PERIOD_ROW) -> float | None:
    """1D 신호의 자기상관에서 지배 주기(px) 추출. 신뢰할 수 없으면 None."""
    n = len(signal)
    if n < min_lag * 3:
        return None
    s = signal - signal.mean()
    if s.std() < 1.0:
        return None
    # 전체 자기상관 (lag 0 ~ n//2)
    full = np.correlate(s, s, mode="full")
    ac = full[n - 1:]          # lag ≥ 0
    ac = ac / (ac[0] + 1e-9)  # 정규화

    # min_lag 이후의 첫 번째 양의 극대값 탐색
    search = ac[min_lag:]
    if len(search) < 2:
        return None
    diff = np.diff(search)
    # 상승→하강 전환 지점 = 극대값
    peaks = np.where((diff[:-1] > 0) & (diff[1:] <= 0))[0] + 1  # +1: diff offset
    if len(peaks) == 0:
        return None
    best_lag = int(peaks[0]) + min_lag
    peak_val = float(ac[best_lag])
    if peak_val < 0.15:  # 피크가 너무 약하면 신뢰 불가
        return None
    return float(best_lag)


def detect_lane_markings(
    frame: np.ndarray,
    road_rank: str = "",
    roi_top_frac: float = 0.45,
) -> LaneMarkingResult:
    """
    frame: BGR 또는 grayscale 이미지.
    road_rank: NodeLink road_rank 문자열 (예: "101", "103").
    roi_top_frac: ROI 상단 경계 (frame height 비율). 수평선 아래만 분석.

    반환: LaneMarkingResult
      .lane_width_obs  — [(row_y, lane_width_px), ...]
      .dash_period_obs — [(row_center, period_px), ...]
    """
    result = LaneMarkingResult()
    period_m, paint_m, gap_m = _marking_constants(road_rank)
    result.mark_period_m = period_m
    result.mark_paint_m  = paint_m
    result.mark_gap_m    = gap_m

    h, w = frame.shape[:2]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame

    roi_top = int(h * roi_top_frac)
    roi = gray[roi_top:h, :]

    # 명도 정규화 (조명 조건 변화 완화)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    roi_eq = clahe.apply(roi)

    roi_h = roi.shape[0]
    max_lane_w_px = int(w * _MAX_LANE_WIDTH_PX_FRAC)

    # ── 1. 차선폭 관측 (가로 방향) ─────────────────────────────────────────
    sample_rows_abs: list[int] = []
    for frac in np.linspace(0.9, 0.1, _SAMPLE_ROWS):
        r = int(roi_h * frac)
        if 0 <= r < roi_h:
            sample_rows_abs.append(r)

    all_line_xs: list[list[float]] = []
    for r in sample_rows_abs:
        row_strip = roi_eq[r, :]
        xs = _white_column_peaks(row_strip)
        # 유효 쌍 → 차선폭 obs
        row_y_abs = roi_top + r
        valid_xs = [x for x in xs if 0 < x < w]
        all_line_xs.append(valid_xs)
        for i in range(len(valid_xs) - 1):
            lw_px = valid_xs[i + 1] - valid_xs[i]
            if _MIN_LANE_WIDTH_PX <= lw_px <= max_lane_w_px:
                result.lane_width_obs.append((float(row_y_abs), float(lw_px)))

    result.detected_line_xs = all_line_xs

    # ── 2. 점선 주기 관측 (세로 방향) ──────────────────────────────────────
    # 감지된 차선 x 위치(대표값)에서 수직 스트립을 추출해 자기상관으로 주기 측정.
    # 대표 x: 전체 샘플 행의 평균 x 클러스터링 (단순히 중간값 사용)
    flat_xs: list[float] = [x for row in all_line_xs for x in row]
    if flat_xs:
        # x 좌표 정렬 후 인접 차이 기반 클러스터링 (갭 > w*0.05 이면 새 클러스터)
        flat_xs.sort()
        clusters: list[list[float]] = [[flat_xs[0]]]
        for x in flat_xs[1:]:
            if x - clusters[-1][-1] < w * 0.05:
                clusters[-1].append(x)
            else:
                clusters.append([x])

        line_centers = [float(np.median(c)) for c in clusters]
        result.n_lanes_detected = max(0, len(line_centers) - 1)

        for cx in line_centers:
            x0 = max(0, int(cx) - _STRIP_HALF_W)
            x1 = min(w, int(cx) + _STRIP_HALF_W)
            if x1 - x0 < 3:
                continue
            strip = roi_eq[:, x0:x1].mean(axis=1).astype(float)
            period_px = _autocorr_period(strip)
            if period_px is None:
                continue
            row_center = roi_top + roi_h // 2
            result.dash_period_obs.append((float(row_center), float(period_px)))

    if result.lane_width_obs or result.dash_period_obs:
        logger.debug(
            "lane_markings: rank=%s lane_w_obs=%d dash_obs=%d lanes≈%d "
            "period_m=%.1f",
            road_rank or "?",
            len(result.lane_width_obs),
            len(result.dash_period_obs),
            result.n_lanes_detected,
            period_m,
        )

    return result


def validate_dash_period(
    period_px: float,
    row_y: float,
    frame_h: int,
    mpp_estimate: float,
    road_rank: str = "",
) -> bool:
    """
    검출된 점선 주기(px)가 실세계 규격과 ±TOL 이내인지 검증.

    mpp_estimate: 해당 행에서의 종방향 meters-per-pixel 추정값.
    반환: True면 신뢰 가능한 관측.
    """
    period_m, _, _ = _marking_constants(road_rank)
    measured_m = period_px * mpp_estimate
    ratio = measured_m / period_m
    return (1.0 - MARK_PERIOD_TOL) <= ratio <= (1.0 + MARK_PERIOD_TOL)


# ── 자체 테스트 ────────────────────────────────────────────────────────────────
def _self_test() -> None:
    """합성 프레임으로 기본 동작 검증."""
    h, w = 720, 1280
    frame = np.zeros((h, w), dtype=np.uint8)

    # 두 개의 흰 수직 차선 (x=400, x=680) + 점선 패턴
    period_px = 60          # 점선 주기 (px)
    paint_px  = 23          # paint 구간
    for y in range(h // 2, h):
        if (y // period_px) % 2 == 0 and (y % period_px) < paint_px:
            frame[y, 395:405] = 240
            frame[y, 675:685] = 240

    bgr = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
    res = detect_lane_markings(bgr, road_rank="103", roi_top_frac=0.45)

    assert len(res.lane_width_obs) > 0, "차선폭 관측 없음"
    lw_px_vals = [lw for _, lw in res.lane_width_obs]
    assert all(270 <= lw <= 290 for lw in lw_px_vals), f"차선폭 이상: {lw_px_vals}"

    if res.dash_period_obs:
        detected_period = res.dash_period_obs[0][1]
        assert abs(detected_period - period_px) < period_px * 0.3, \
            f"점선 주기 오차 큼: {detected_period:.1f} vs {period_px}"
        print(f"  점선 주기 감지: {detected_period:.1f}px (기대 {period_px}px)")

    print(f"  차선폭 관측 {len(res.lane_width_obs)}개, "
          f"점선 주기 관측 {len(res.dash_period_obs)}개 — OK")


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    print("lane_markings self-test:")
    _self_test()
    print("PASS")
