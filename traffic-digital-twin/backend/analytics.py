"""
analytics.py — 교통 지표 계산 엔진
  입력: VehicleState 리스트 (GPS 좌표 포함)
  출력: FrameAnalytics (JSON 직렬화 가능)

  경보: 과속(speed > limit) + 병목(연속 정지 >= threshold)
"""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from collections import defaultdict, Counter, deque
import logging
import math
import os
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# ── 속도 진단 로거 ─────────────────────────────────────────────────────
# 깜빡임(0↔100) 원인을 데이터로 확정하기 위한 per-frame 진단. 기본 off(운영 영향 0).
# 켜는 법(둘 중 하나, 재시작 불필요):
#   1) backend/ 에 빈 파일 'speed_debug.on' 생성  ← 권장(즉시 반영, env 타이밍 무관)
#   2) 환경변수 SPEED_DEBUG=1 로 백엔드 시작
# 끄는 법: 파일 삭제 / env 해제. 결과는 backend/speed_debug.log.
_SPD_DIR = Path(__file__).resolve().parent
_SPD_FLAG = _SPD_DIR / "speed_debug.on"
_SPD_LOGFILE = _SPD_DIR / "speed_debug.log"
_spd_log = logging.getLogger("speed_debug")
_spd_log.setLevel(logging.DEBUG)
_spd_log.propagate = False
_spd_enabled_cache: tuple[float, bool] = (0.0, False)
_spd_override: bool | None = None   # API 토글: None=env/플래그파일, True/False=강제


def _spd_attach_handler() -> None:
    if not _spd_log.handlers:
        _h = logging.FileHandler(_SPD_LOGFILE, encoding="utf-8")
        _h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        _spd_log.addHandler(_h)


def set_speed_debug(on: bool) -> str:
    """API/코드에서 진단 로깅을 강제 on/off. 로그 파일 경로 반환."""
    global _spd_override, _spd_enabled_cache
    _spd_override = bool(on)
    _spd_enabled_cache = (0.0, False)   # 캐시 무효화
    if on:
        _spd_attach_handler()
        _spd_log.debug("=== speed debug enabled via API ===")
    return str(_SPD_LOGFILE)


def speed_debug_status() -> dict:
    p = str(_SPD_LOGFILE)
    ex = os.path.exists(p)
    return {
        "enabled": _speed_debug_enabled(),
        "logfile": p,
        "exists": ex,
        "bytes": os.path.getsize(p) if ex else 0,
    }


def _speed_debug_enabled() -> bool:
    """진단 로깅 활성 여부 (API 강제 > env > 플래그 파일). 2초 캐시로 stat 부담 제거."""
    global _spd_enabled_cache
    if _spd_override is not None:
        return _spd_override
    now = time.monotonic()
    last_ts, val = _spd_enabled_cache
    if now - last_ts < 2.0:
        return val
    try:
        enabled = (
            os.getenv("SPEED_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")
            or _SPD_FLAG.exists()
        )
    except Exception:
        enabled = False
    if enabled:
        _spd_attach_handler()
    _spd_enabled_cache = (now, enabled)
    return enabled

from config import (
    SPEED_LIMIT_KPH,
    BOTTLENECK_DWELL_FRAMES,
    LOS_THRESHOLDS,
    SPEED_JITTER_THRESHOLD_M,
    MAX_REASONABLE_KPH,
    GC_GRACE_FRAMES,
    PARKED_FRAMES_THRESHOLD,
    PARKED_POSITION_RADIUS_PX,
    SPEED_WINDOW_FRAMES,
    SPEED_EMA_ALPHA,
    SPEED_STOP_SPAN_S,
    SPEED_SPIKE_FACTOR,
    SPEED_MIN_KPH,
    SPEED_OUTLIER_MAD_K,
    DIR_DEADZONE_M,
    DIR_EMA_ALPHA,
    BEARING_REFINE_MIN_SAMPLES,
    BEARING_REFINE_EMA_ALPHA,
    ROAD_PTS_REFINE_MIN_SAMPLES,
    ROAD_PTS_REFINE_NBINS,
)

from utils import haversine_m


# ── 차량 단위 상태 ────────────────────────────────────────────────────
@dataclass
class VehicleState:
    track_id:      int
    class_name:    str
    bbox_xyxy:     list[float]
    center_px:     tuple[float, float]
    lat:           float
    lon:           float
    x_m:           float = 0.0
    y_m:           float = 0.0
    direction:     str   = "Unknown"   # "In" / "Out" / "Unknown"
    speed_kph:     float = 0.0
    is_speeding:   bool  = False
    dwell_frames:  int   = 0
    is_bottleneck: bool  = False
    is_parked:     bool  = False
    lane_id:       int   = -1


# ── 프레임 단위 집계 결과 ─────────────────────────────────────────────
@dataclass
class FrameAnalytics:
    frame_id:      int
    timestamp_ms:  float
    vehicles:      list[dict] = field(default_factory=list)
    vehicle_count: int        = 0
    avg_speed_kph: float      = 0.0
    los_grade:     str        = "A"
    in_count:      int        = 0
    out_count:     int        = 0
    class_counts:  dict       = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# ── 분석 엔진 ──────────────────────────────────────────────────────────
class TrafficAnalytics:

    def __init__(self):
        self._lock = threading.Lock()
        self.speed_limit_kph: float = SPEED_LIMIT_KPH
        self.road_bearing_deg: float | None = None  # 도로 진행 방향 (0=북, 시계방향)
        self.cam_lat: float | None = None
        self.cam_lon: float | None = None
        # track_id → (lat, lon, x_m, y_m, timestamp_s)
        self._prev: dict[int, tuple[float, float, float, float, float]] = {}
        self._dwell: dict[int, int] = defaultdict(int)
        # GC grace period: 미감지 프레임 수 (GC_GRACE_FRAMES 이후 실제 삭제)
        self._lost_frames: dict[int, int] = {}
        # 주차 확정된 픽셀 위치 목록 — track_id 변경에도 유지.
        # bounded: 장시간 구동 시 무한 증가 방지(+ _is_near_parked O(n) 스캔 상한).
        self._parked_positions: deque = deque(maxlen=200)
        # C: 슬라이딩 윈도우 — (x_m, y_m, timestamp_s) 이력
        self._pos_window: dict[int, deque] = {}
        # 트랙별 평활(EMA) 속도 — 프레임마다 재생성되는 VehicleState를 넘어 값 유지
        self._speed_ema: dict[int, float] = {}
        # Per-vehicle direction state (LineZone fallback + movement-based)
        self._vehicle_direction: dict[int, str] = {}
        # Movement-based direction: along-axis EMA per track
        self._along_prev: dict[int, float] = {}
        self._dir_ema: dict[int, float] = {}
        # Bearing auto-refinement: axial flow accumulator (double-angle statistics)
        self._flow_sin2: float = 0.0
        self._flow_cos2: float = 0.0
        self._flow_n: int = 0
        # Road-shape learning: GPS positions of moving vehicles for road_pts refinement
        self._gps_trace: deque = deque(maxlen=1000)
        # ITS 구간속도 비교 자동 보정
        self.speed_scale: float = 1.0          # 보정 계수 (1.0 = 보정 없음)
        # 10분 분량 버퍼: ITS 5분 창 타이밍 불일치 흡수를 위해 2배 확보
        # analytics.update()가 ~10fps로 호출 → 6000샘플 ≈ 10분
        self._speed_samples: deque = deque(maxlen=6000)
        self._stable_count: int = 0            # 연속 안정 갱신 횟수 (>= 3 이면 수렴 간주)
        self._overspeed_count: int = 0         # over-limit 스킵 누적 (진단용)
        self._spd_log_last: dict[int, float] = {}   # 트랙별 진단 로그 스로틀 타임스탬프

    def reset(self) -> None:
        """카메라 전환 시 상태 초기화."""
        with self._lock:
            self._prev.clear()
            self._dwell.clear()
            self._lost_frames.clear()
            self._parked_positions.clear()
            self._pos_window.clear()
            self._speed_ema.clear()
            self._vehicle_direction.clear()
            self._along_prev.clear()
            self._dir_ema.clear()
            self._flow_sin2 = 0.0
            self._flow_cos2 = 0.0
            self._flow_n = 0
            self._gps_trace.clear()
            self._speed_samples.clear()
            self._spd_log_last.clear()
            self._stable_count = 0
            # speed_scale은 reset하지 않음 — main.py에서 저장된 값을 복원함

    def update(
        self,
        frame_id: int,
        timestamp_ms: float,
        vehicles: list[VehicleState],
        in_count: int,
        out_count: int,
        crossed_in_ids: set[int] | None = None,
        crossed_out_ids: set[int] | None = None,
    ) -> FrameAnalytics:
        with self._lock:
            return self._update_locked(
                frame_id, timestamp_ms, vehicles, in_count, out_count,
                crossed_in_ids or set(), crossed_out_ids or set(),
            )

    def _update_locked(
        self,
        frame_id: int,
        timestamp_ms: float,
        vehicles: list[VehicleState],
        in_count: int,
        out_count: int,
        crossed_in_ids: set[int] = set(),
        crossed_out_ids: set[int] = set(),
    ) -> FrameAnalytics:
        active = {v.track_id for v in vehicles}
        self._gc(active)

        # LineZone 교차 이벤트 — 도로 bearing 미설정 시 fallback direction으로 유지
        for tid in crossed_in_ids:
            self._vehicle_direction[tid] = "In"
        for tid in crossed_out_ids:
            self._vehicle_direction[tid] = "Out"

        current_ts = timestamp_ms / 1000.0

        for v in vehicles:
            # Task 4: 주차 latch — 이미 느린 차에만 적용(이동 중인 차가 주차 지점 통과해도 0 되지 않도록)
            if (self._is_near_parked(v.center_px)
                    and self._speed_ema.get(v.track_id, 0.0) < SPEED_MIN_KPH):
                v.is_parked = True
                self._dwell[v.track_id] = PARKED_FRAMES_THRESHOLD
            self._speed(v, current_ts)
            self._dwell_update(v)

        # Task 1: GPS 투영 + 이동 기반 방향 분류
        along_map = self._project_to_road_axis(vehicles)
        self._assign_directions(vehicles, along_map)

        # Road-shape learning: GPS trace 누적
        self._accumulate_gps_trace(vehicles)

        # Task 3: 흐름 벡터 누적 (bearing 자동 보정용)
        self._accumulate_flow(vehicles)

        # 통계·경보는 주차 차량 제외
        active_vehicles = [v for v in vehicles if not v.is_parked]

        result = FrameAnalytics(
            frame_id=frame_id,
            timestamp_ms=timestamp_ms,
            vehicles=[asdict(v) for v in vehicles],        # 지도 표시는 전체
            vehicle_count=len(active_vehicles),
            avg_speed_kph=self._avg_speed(active_vehicles),
            los_grade=self._los(len(active_vehicles)),
            in_count=in_count,
            out_count=out_count,
            class_counts=self._class_counts(vehicles),
        )

        for v in vehicles:
            self._prev[v.track_id] = (v.lat, v.lon, v.x_m, v.y_m, current_ts)

        # ITS 보정용 속도 샘플 수집 (이동 차량만, 보정 계수 적용된 값)
        if result.avg_speed_kph > 0:
            self._speed_samples.append((result.avg_speed_kph, time.monotonic()))

        return result

    # ──────────────────────────────────────────────────────────────────
    def _is_near_parked(self, center_px: tuple[float, float]) -> bool:
        cx, cy = center_px
        return any(
            math.hypot(cx - px, cy - py) < PARKED_POSITION_RADIUS_PX
            for px, py in self._parked_positions
        )

    @staticmethod
    def _estimate_speed_kph(window: deque) -> float:
        """최근 위치들의 선형 회귀 기울기로 지터에 덜 민감한 속도를 추정한다."""
        t0 = window[0][2]
        n = len(window)
        sum_t = sum_x = sum_y = sum_t2 = sum_tx = sum_ty = 0.0
        for x, y, ts in window:
            t = ts - t0
            sum_t += t; sum_x += x; sum_y += y
            sum_t2 += t * t; sum_tx += t * x; sum_ty += t * y
        mean_t = sum_t / n
        denom = sum_t2 - n * mean_t * mean_t
        if denom <= 1e-9:
            return 0.0
        vx = (sum_tx - n * mean_t * (sum_x / n)) / denom
        vy = (sum_ty - n * mean_t * (sum_y / n)) / denom
        return math.hypot(vx, vy) * 3.6

    def _spd_debug(self, tid, ts, dec, inst_dt, step_m, span, disp_m, raw, n,
                   force: bool = False) -> None:
        """SPEED_DEBUG 시 per-frame 진단을 speed_debug.log 에 1줄 기록.

        force=True(jump_clear/overlimit 등 중요 이벤트)는 스로틀을 무시한다.
        그 외(OK/jitter0/win<2)는 트랙당 0.2s 간격으로 제한해 파일 폭증 방지.
        """
        if not _speed_debug_enabled():
            return
        if not force:
            if ts - self._spd_log_last.get(tid, 0.0) < 0.2:
                return
        self._spd_log_last[tid] = ts
        def _f(x):
            return "-" if x is None else f"{x:.3f}"
        scaled = None if raw is None else raw * self.speed_scale
        _spd_log.debug(
            "tid=%d dec=%s n=%s instdt=%s step_m=%s span=%s disp_m=%s "
            "raw=%s scale=%.3f scaled=%s",
            tid, dec, ("-" if n is None else n), _f(inst_dt), _f(step_m),
            _f(span), _f(disp_m), _f(raw), self.speed_scale, _f(scaled),
        )

    def _speed(self, v: VehicleState, current_ts: float) -> None:
        """트랙별 위치 윈도우 + EMA 평활화로 안정적인 속도를 산출한다.

        핵심 설계(진단 로그로 확정):
          - 위치(미터좌표) 지터로 한 프레임 변위가 크게 튐 → 윈도우를 *비우지 말고* 그
            이상치 샘플만 버린다(예전엔 통째로 clear → 47% 윈도우 파괴 → 속도 0).
          - 출력은 트랙별 EMA. 노이즈/엣지 프레임에선 0으로 덤프하지 않고 직전 값을 유지,
            진짜 정지(변위<jitter가 SPEED_STOP_SPAN_S 이상 지속)일 때만 0으로 감쇠.
          - EMA에 스파이크 거부를 둬 단발 큰 값이 표시를 끌어올리지 못하게 한다.
        """
        tid = v.track_id
        if v.is_parked:
            self._speed_ema.pop(tid, None)
            v.speed_kph = 0.0
            self._spd_debug(tid, current_ts, "parked", None, None, None, None, None, None)
            return

        win = self._pos_window.setdefault(tid, deque(maxlen=SPEED_WINDOW_FRAMES))
        prev_ema = self._speed_ema.get(tid)

        # 직전 프레임 대비 순간 dt/변위
        inst_dt = step_m = None
        append = True
        if win:
            last_x, last_y, last_ts = win[-1]
            inst_dt = current_ts - last_ts
            step_m = math.hypot(v.x_m - last_x, v.y_m - last_y)
            if inst_dt > 2.0:
                win.clear()                      # 트랙 끊김/재등장 → 리셋
                inst_dt = step_m = None
            elif inst_dt > 0:
                raw_max_mps = (MAX_REASONABLE_KPH / 3.6) / max(self.speed_scale, 0.1)
                if step_m > raw_max_mps * inst_dt * 3.0:
                    # ID-switch-level teleport: clear window to prevent corrupted regression slope
                    win.clear()
                    append = False
                    self._spd_debug(tid, current_ts, "teleport_reset", inst_dt, step_m,
                                    None, None, None, 0, force=True)
                elif step_m > raw_max_mps * inst_dt * 1.5:
                    append = False               # ★ 이상치 샘플만 버림(윈도우 유지)
                    self._spd_debug(tid, current_ts, "outlier_skip", inst_dt, step_m,
                                    None, None, None, len(win), force=True)

        # 중복 위치(비탐지 프레임) 스킵
        if append and not (win and abs(v.x_m - win[-1][0]) < 1e-6
                           and abs(v.y_m - win[-1][1]) < 1e-6):
            win.append((v.x_m, v.y_m, current_ts))

        # 윈도우가 부족하면 직전 EMA 유지(0으로 떨어뜨리지 않음).
        # 중요: EMA를 0.0으로 '시드'하지 않는다 — 0 시드는 이후 스파이크 거부가 모든 실제
        # 속도를 막아 영구 0 고착을 유발하므로, 미확정 상태는 dict에 기록하지 않는다.
        if len(win) < 2:
            v.speed_kph = round(self._speed_ema.get(tid, 0.0), 1)
            self._spd_debug(tid, current_ts, "win<2", inst_dt, step_m, None, None, None, len(win))
            return

        oldest_x, oldest_y, oldest_ts = win[0]
        newest_x, newest_y, newest_ts = win[-1]
        span = newest_ts - oldest_ts
        disp_m = math.hypot(newest_x - oldest_x, newest_y - oldest_y)

        if span <= 0:
            v.speed_kph = round(self._speed_ema.get(tid, 0.0), 1)
            self._spd_debug(tid, current_ts, "dt<=0", inst_dt, step_m, span, disp_m, None, len(win))
            return

        moving = disp_m >= SPEED_JITTER_THRESHOLD_M
        raw = self._estimate_speed_kph(win) if moving else 0.0
        scaled = raw * self.speed_scale

        if moving and SPEED_MIN_KPH <= scaled <= MAX_REASONABLE_KPH:
            # 스파이크 거부는 EMA가 '확정'(>5)된 경우에만 — 시드 단계는 그대로 수용.
            if prev_ema is not None and prev_ema > 5.0 and scaled > prev_ema * SPEED_SPIKE_FACTOR + 20:
                dec = "spike_skip"                       # 기존 EMA 유지(미기록)
            else:
                self._speed_ema[tid] = (
                    scaled if prev_ema is None
                    else prev_ema * (1 - SPEED_EMA_ALPHA) + scaled * SPEED_EMA_ALPHA
                )
                dec = "OK"
        elif moving and scaled > MAX_REASONABLE_KPH:   # 물리적 불가 → 노이즈 무시
            self._overspeed_count += 1
            if self._overspeed_count % 64 == 1:
                logger.debug("speed over-limit: raw=%.1f scale=%.3f span=%.3fs win=%d (누적 %d)",
                             raw, self.speed_scale, span, len(win), self._overspeed_count)
            dec = "overlimit"
        else:
            # 변위<jitter 이거나 측정속도<MIN(정지차 지터) → 정지로 간주, stale 값 0 감쇠.
            decayed = self._speed_ema.get(tid, 0.0) * 0.6
            self._speed_ema[tid] = 0.0 if decayed < SPEED_MIN_KPH else decayed
            dec = "stop" if self._speed_ema[tid] == 0.0 else "decay"

        v.speed_kph = round(self._speed_ema.get(tid, 0.0), 1)
        v.is_speeding = v.speed_kph > self.speed_limit_kph * 1.10
        self._spd_debug(tid, current_ts, dec, inst_dt, step_m, span, disp_m, raw, len(win))

    def _project_to_road_axis(self, vehicles: list[VehicleState]) -> dict[int, float]:
        """차량 GPS를 도로 bearing 축에 투영 → 횡방향 흔들림 제거.

        카메라 GPS를 고정 기준점으로 사용해 차량 수 변동에 따른 기준점 흔들림 방지.
        반환: {track_id: along_m} — 방향 분류에 사용.
        along > 0: bearing 방향(카메라 앞쪽), along < 0: 반대 방향.
        """
        along_map: dict[int, float] = {}
        if self.road_bearing_deg is None or not vehicles:
            return along_map
        b = math.radians(self.road_bearing_deg)
        sin_b, cos_b = math.sin(b), math.cos(b)

        # 카메라 GPS를 고정 기준점으로 사용 (cam_lat/lon 없으면 차량 centroid 대체)
        if self.cam_lat is not None and self.cam_lon is not None:
            ref_lat = self.cam_lat
            ref_lon = self.cam_lon
        else:
            ref_lat = sum(v.lat for v in vehicles) / len(vehicles)
            ref_lon = sum(v.lon for v in vehicles) / len(vehicles)
        R_lat = 110574.0
        R_lon = 111320.0 * math.cos(math.radians(ref_lat))

        for v in vehicles:
            dx = (v.lon - ref_lon) * R_lon  # 동(East) 오프셋 (m)
            dy = (v.lat - ref_lat) * R_lat  # 북(North) 오프셋 (m)
            along = dx * sin_b + dy * cos_b  # 도로 방향 성분
            along_map[v.track_id] = along
            v.lon = ref_lon + (along * sin_b) / R_lon
            v.lat = ref_lat + (along * cos_b) / R_lat
        return along_map

    def _assign_directions(self, vehicles: list[VehicleState], along_map: dict[int, float]) -> None:
        """Task 1: 이동 벡터 기반 In/Out 분류.

        bearing 방향으로 이동(along 증가) = Out(outbound).
        bearing 반대(along 감소) = In(inbound, approaching camera).
        road_bearing_deg 미설정 시 LineZone fallback 사용.
        """
        if self.road_bearing_deg is None:
            for v in vehicles:
                if v.track_id in self._vehicle_direction:
                    v.direction = self._vehicle_direction[v.track_id]
            return

        for v in vehicles:
            if v.is_parked:
                continue
            tid = v.track_id
            along = along_map.get(tid)
            if along is None:
                v.direction = self._vehicle_direction.get(tid, "Unknown")
                continue

            prev_along = self._along_prev.get(tid)
            self._along_prev[tid] = along

            if prev_along is None:
                v.direction = self._vehicle_direction.get(tid, "Unknown")
                continue

            d_along = along - prev_along
            prev_ema = self._dir_ema.get(tid)
            new_ema = d_along if prev_ema is None else (
                prev_ema * (1 - DIR_EMA_ALPHA) + d_along * DIR_EMA_ALPHA
            )
            self._dir_ema[tid] = new_ema

            if new_ema < -DIR_DEADZONE_M:
                dir_now = "In"
            elif new_ema > DIR_DEADZONE_M:
                dir_now = "Out"
            else:
                dir_now = self._vehicle_direction.get(tid, "Unknown")

            self._vehicle_direction[tid] = dir_now
            v.direction = dir_now

    def _accumulate_flow(self, vehicles: list[VehicleState]) -> None:
        """Task 3: 이동 차량의 방위각을 이중각 통계로 누적 (bearing 자동 보정용).

        x_m(East), y_m(North) delta를 사용. _prev는 이전 프레임 값이므로 프레임간 이동 벡터가 정확.
        """
        for v in vehicles:
            if v.is_parked or v.speed_kph < SPEED_MIN_KPH:
                continue
            prev = self._prev.get(v.track_id)
            if prev is None:
                continue
            prev_x, prev_y = prev[2], prev[3]  # x_m, y_m (projection에 의해 변경되지 않음)
            dx = v.x_m - prev_x  # East delta (m)
            dy = v.y_m - prev_y  # North delta (m)
            if math.hypot(dx, dy) < SPEED_JITTER_THRESHOLD_M:
                continue
            theta = math.atan2(dx, dy)  # heading (East, North) → atan2 기준
            self._flow_sin2 += math.sin(2.0 * theta)
            self._flow_cos2 += math.cos(2.0 * theta)
            self._flow_n += 1

    def _accumulate_gps_trace(self, vehicles: list[VehicleState]) -> None:
        """Road-shape learning: GPS positions of moving vehicles를 누적.

        속도 조건을 두지 않음 — 속도 버그 등으로 speed=0이어도 위치는 유효하며
        도로 형상 학습에 기여 가능. 주차 확정 차량만 제외.
        """
        for v in vehicles:
            if not v.is_parked and v.lat and v.lon:
                self._gps_trace.append((v.lat, v.lon))

    def refine_road_pts(
        self, cam_lat: float | None, cam_lon: float | None,
    ) -> tuple[list[list[float]], float] | None:
        """GPS trace에서 도로 중심선 polyline을 추정한다.

        차량 GPS 위치를 현재 road_bearing_deg 축으로 투영 → 정렬 → N bin 평균으로 polyline 생성.
        Returns (road_pts [[lat,lon],...], snap_along_m) or None.
        """
        with self._lock:
            if (len(self._gps_trace) < ROAD_PTS_REFINE_MIN_SAMPLES
                    or self.road_bearing_deg is None
                    or cam_lat is None or cam_lon is None):
                return None

            bearing_rad = math.radians(self.road_bearing_deg)
            cos_b = math.cos(bearing_rad)
            sin_b = math.sin(bearing_rad)
            R_lat = 110574.0
            R_lon = 111320.0 * math.cos(math.radians(cam_lat))

            pts_along: list[tuple[float, float, float]] = []
            for lat, lon in self._gps_trace:
                d_north = (lat - cam_lat) * R_lat
                d_east  = (lon - cam_lon) * R_lon
                along = d_east * sin_b + d_north * cos_b
                pts_along.append((along, lat, lon))

            pts_along.sort(key=lambda x: x[0])

            n = ROAD_PTS_REFINE_NBINS
            bin_size = len(pts_along) / n
            road_pts: list[list[float]] = []
            for i in range(n):
                start = int(i * bin_size)
                end   = int((i + 1) * bin_size)
                bucket = pts_along[start:end]
                if not bucket:
                    continue
                mean_lat = sum(p[1] for p in bucket) / len(bucket)
                mean_lon = sum(p[2] for p in bucket) / len(bucket)
                road_pts.append([round(mean_lat, 7), round(mean_lon, 7)])

            if len(road_pts) < 2:
                return None

            # snap_along_m: cumulative distance from road_pts[0] to camera projection
            R_lat_m = 110574.0
            cum = 0.0
            best_d = float("inf")
            snap_along_m = 0.0
            for i in range(len(road_pts) - 1):
                f, t = road_pts[i], road_pts[i + 1]
                R_lon_m = 111320.0 * math.cos(math.radians(f[0]))
                px = (cam_lon - f[1]) * R_lon_m
                py = (cam_lat - f[0]) * R_lat_m
                bx = (t[1] - f[1]) * R_lon_m
                by = (t[0] - f[0]) * R_lat_m
                seg_sq = bx * bx + by * by
                tt = max(0.0, min(1.0, (px * bx + py * by) / seg_sq)) if seg_sq > 1e-9 else 0.0
                d = math.hypot(px - tt * bx, py - tt * by)
                seg_len = math.sqrt(seg_sq)
                if d < best_d:
                    best_d = d
                    snap_along_m = cum + tt * seg_len
                cum += seg_len

            return road_pts, round(snap_along_m, 1)

    def refine_bearing(self) -> float | None:
        """Task 3: 흐름 벡터로 추정한 도로축으로 road_bearing_deg를 EMA 보정.

        표본이 BEARING_REFINE_MIN_SAMPLES 미만이거나 bearing 미설정 시 None 반환.
        성공 시 갱신된 road_bearing_deg 반환.
        """
        with self._lock:
            if self._flow_n < BEARING_REFINE_MIN_SAMPLES or self.road_bearing_deg is None:
                return None

            axis_rad = 0.5 * math.atan2(self._flow_sin2, self._flow_cos2)
            axis_deg = math.degrees(axis_rad)  # -90 ~ 90

            # 180° 모호성 해소: 현재 bearing에 가까운 쪽 선택
            cand1 = axis_deg % 360
            cand2 = (axis_deg + 180.0) % 360
            current = self.road_bearing_deg
            diff1 = abs(((cand1 - current + 180) % 360) - 180)
            diff2 = abs(((cand2 - current + 180) % 360) - 180)
            chosen = cand1 if diff1 <= diff2 else cand2

            # 각도 차이를 EMA로 점진 보정 (wrap-around 안전)
            angle_diff = ((chosen - current + 180) % 360) - 180
            self.road_bearing_deg = round(
                (current + BEARING_REFINE_EMA_ALPHA * angle_diff) % 360, 2
            )
            return self.road_bearing_deg

    def _dwell_update(self, v: VehicleState) -> None:
        if v.is_parked:
            v.dwell_frames = PARKED_FRAMES_THRESHOLD
            v.is_bottleneck = False
            return

        if v.speed_kph == 0.0:
            self._dwell[v.track_id] += 1
        else:
            self._dwell[v.track_id] = 0

        v.dwell_frames = self._dwell[v.track_id]
        v.is_bottleneck = v.dwell_frames >= BOTTLENECK_DWELL_FRAMES
        v.is_parked = v.dwell_frames >= PARKED_FRAMES_THRESHOLD

        # 새로 주차 확정 시 위치 등록
        if v.is_parked:
            v.is_bottleneck = False
            cx, cy = v.center_px
            if not self._is_near_parked((cx, cy)):
                self._parked_positions.append((cx, cy))

    def _los(self, count: int) -> str:
        for grade, threshold in LOS_THRESHOLDS.items():
            if count <= threshold:
                return grade
        return "F"

    @staticmethod
    def _reject_speed_outliers(speeds: list[float]) -> list[float]:
        """MAD 기반 속도 outlier 제거 (한계3 A): 트랙 ID 스왑 등으로 튀는 샘플 차단.

        |x − median| > K·1.4826·MAD 인 값을 제거. 샘플 <3이면 robust 추정 불가 → 원본.
        ITS 없는 도로에서도 self-consistency로 이상치를 자체 검증한다.
        """
        n = len(speeds)
        if n < 3:
            return speeds
        srt = sorted(speeds)
        med = srt[n // 2] if n % 2 else (srt[n // 2 - 1] + srt[n // 2]) / 2.0
        devs = sorted(abs(x - med) for x in speeds)
        mad = devs[n // 2] if n % 2 else (devs[n // 2 - 1] + devs[n // 2]) / 2.0
        if mad < 1e-6:
            return speeds
        thr = SPEED_OUTLIER_MAD_K * 1.4826 * mad   # 1.4826: MAD→정규 σ 환산
        kept = [x for x in speeds if abs(x - med) <= thr]
        return kept or speeds

    @staticmethod
    def _avg_speed(vehicles: list[VehicleState]) -> float:
        s = [v.speed_kph for v in vehicles if v.speed_kph > 0]
        s = TrafficAnalytics._reject_speed_outliers(s)
        return round(sum(s) / len(s), 1) if s else 0.0

    @staticmethod
    def _class_counts(vehicles: list[VehicleState]) -> dict[str, int]:
        return dict(Counter(v.class_name for v in vehicles))

    @property
    def speed_scale_converged(self) -> bool:
        """True이면 보정 계수가 수렴해 학습률이 크게 낮아진 상태."""
        return self._stable_count >= 3

    def calibrate_from_its(self, its_speed_kph: float, window_s: float = 600.0) -> float | None:
        """ITS 구간속도(its_speed_kph)와 측정 평균을 비교해 speed_scale 자동 보정.

        반환: 갱신된 speed_scale (샘플 부족·분산 과다 시 None)

        window_s=600(10분): ITS 5분 집계 창이 우리 창에 포함되도록 2배 확보.
        ITS API 업데이트 주기(5분)와 우리 폴링 타이밍이 맞지 않아도
        10분 창 안에 ITS 집계 구간이 반드시 겹침.

        속도 분산이 30% 초과(교통량 급변)이면 보정 스킵 — 과도기 데이터로
        잘못된 방향으로 보정되는 것을 막기 위해.
        """
        with self._lock:
            now = time.monotonic()
            recent = [s for s, t in self._speed_samples if now - t <= window_s]
            if len(recent) < 50:  # 10분 창 기준 최소 샘플 수 상향
                return None
            our_avg = sum(recent) / len(recent)
            if our_avg < 3.0:
                return None

            # 교통량 급변 구간은 보정 스킵 (ITS 창과 우리 창이 같은 상황을 반영하지 않음)
            variance = sum((s - our_avg) ** 2 for s in recent) / len(recent)
            cv = math.sqrt(variance) / our_avg  # 변동계수
            if cv > 0.4:  # 40% 이상 분산 → 불안정 구간
                return None

            old_scale = self.speed_scale
            raw_target = old_scale * its_speed_kph / our_avg
            target = max(0.3, min(5.0, raw_target))
            if raw_target != target:
                # 클램프 도달 = 호모그래피가 크게 어긋났을 가능성 → 무음 열화 방지
                logger.warning(
                    "speed_scale 클램프 도달: raw=%.2f → %.2f "
                    "(our_avg=%.1f, ITS=%.1f — 보정/호모그래피 점검 권장)",
                    raw_target, target, our_avg, its_speed_kph,
                )

            # 수렴 여부에 따라 학습률 조정 (미수렴 시 0.5로 더 빠른 초기 수렴)
            alpha = 0.95 if self.speed_scale_converged else 0.5
            self.speed_scale = round(old_scale * alpha + target * (1 - alpha), 4)

            # 변화율 1% 미만 → 안정, 그 이상 → 안정 카운트 리셋
            change_ratio = abs(self.speed_scale - old_scale) / max(old_scale, 0.01)
            if change_ratio < 0.01:
                self._stable_count += 1
            else:
                self._stable_count = 0

            return self.speed_scale

    def _gc(self, active: set[int]) -> None:
        # 재등장 시 연속성 유지: grace period 동안 _prev/_speed_ema 보존
        lost = set(self._prev) - active
        for tid in lost:
            self._lost_frames[tid] = self._lost_frames.get(tid, 0) + 1
            if self._lost_frames[tid] > GC_GRACE_FRAMES:
                self._prev.pop(tid, None)
                self._dwell.pop(tid, None)
                self._lost_frames.pop(tid, None)
                self._pos_window.pop(tid, None)
                self._speed_ema.pop(tid, None)
                self._spd_log_last.pop(tid, None)
                self._along_prev.pop(tid, None)
                self._dir_ema.pop(tid, None)
        # 재등장한 track의 grace counter 초기화
        for tid in active:
            self._lost_frames.pop(tid, None)
