"""
analytics.py — 교통 지표 계산 엔진
  입력: VehicleState 리스트 (GPS 좌표 포함)
  출력: FrameAnalytics (JSON 직렬화 가능)

  경보: 과속(speed > limit) + 병목(연속 정지 >= threshold)
"""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from collections import defaultdict, Counter
import math
import threading

from config import (
    SPEED_LIMIT_KPH,
    BOTTLENECK_DWELL_FRAMES,
    LOS_THRESHOLDS,
    SPEED_JITTER_THRESHOLD_M,
    SPEED_SMOOTHING_ALPHA,
    MAX_REASONABLE_KPH,
    GC_GRACE_FRAMES,
    PARKED_FRAMES_THRESHOLD,
    PARKED_POSITION_RADIUS_PX,
)

# ── Haversine 거리 계산 ───────────────────────────────────────────────
_R_EARTH = 6_371_000.0

def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """두 GPS 좌표 간 거리 (미터)."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return _R_EARTH * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


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
        # track_id → (lat, lon, x_m, y_m, timestamp_s)
        self._prev: dict[int, tuple[float, float, float, float, float]] = {}
        self._dwell: dict[int, int] = defaultdict(int)
        self._speed_ema: dict[int, float] = {}
        # GC grace period: 미감지 프레임 수 (GC_GRACE_FRAMES 이후 실제 삭제)
        self._lost_frames: dict[int, int] = {}
        # 주차 확정된 픽셀 위치 목록 — track_id 변경에도 유지
        self._parked_positions: list[tuple[float, float]] = []

    def reset(self) -> None:
        """카메라 전환 시 상태 초기화."""
        with self._lock:
            self._prev.clear()
            self._dwell.clear()
            self._speed_ema.clear()
            self._lost_frames.clear()
            self._parked_positions.clear()

    def update(
        self,
        frame_id: int,
        timestamp_ms: float,
        vehicles: list[VehicleState],
        in_count: int,
        out_count: int,
    ) -> FrameAnalytics:
        with self._lock:
            return self._update_locked(frame_id, timestamp_ms, vehicles, in_count, out_count)

    def _update_locked(
        self,
        frame_id: int,
        timestamp_ms: float,
        vehicles: list[VehicleState],
        in_count: int,
        out_count: int,
    ) -> FrameAnalytics:
        active = {v.track_id for v in vehicles}
        self._gc(active)

        current_ts = timestamp_ms / 1000.0

        for v in vehicles:
            if self._is_near_parked(v.center_px):
                v.is_parked = True
                self._dwell[v.track_id] = PARKED_FRAMES_THRESHOLD
            self._speed(v, current_ts)
            self._dwell_update(v)

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
            class_counts=self._class_counts(active_vehicles),
        )

        for v in vehicles:
            self._prev[v.track_id] = (v.lat, v.lon, v.x_m, v.y_m, current_ts)

        return result

    # ──────────────────────────────────────────────────────────────────
    def _is_near_parked(self, center_px: tuple[float, float]) -> bool:
        cx, cy = center_px
        return any(
            math.hypot(cx - px, cy - py) < PARKED_POSITION_RADIUS_PX
            for px, py in self._parked_positions
        )

    def _speed(self, v: VehicleState, current_ts: float) -> None:
        if v.is_parked:
            return
        prev = self._prev.get(v.track_id)
        if prev is None:
            return
        plat, plon, px_m, py_m, prev_ts = prev

        dt = current_ts - prev_ts
        if dt <= 0:
            return

        if plat != 0.0 and v.lat != 0.0:
            dist_m = haversine_m(plat, plon, v.lat, v.lon)
        else:
            dist_m = math.hypot(v.x_m - px_m, v.y_m - py_m)

        # 지터 미만 이동은 즉시 정지 처리
        if dist_m < SPEED_JITTER_THRESHOLD_M:
            self._speed_ema[v.track_id] = 0.0
            v.speed_kph = 0.0
            v.is_speeding = False
            return

        raw_kph = dist_m / dt * 3.6

        # 물리적으로 불가능한 속도는 노이즈 — 이번 프레임 스킵 (EMA 유지)
        if raw_kph > MAX_REASONABLE_KPH:
            prev_ema = self._speed_ema.get(v.track_id, 0.0)
            v.speed_kph = round(prev_ema, 1)
            v.is_speeding = v.speed_kph > SPEED_LIMIT_KPH
            return

        prev_ema = self._speed_ema.get(v.track_id, raw_kph)
        smoothed = SPEED_SMOOTHING_ALPHA * raw_kph + (1.0 - SPEED_SMOOTHING_ALPHA) * prev_ema
        self._speed_ema[v.track_id] = smoothed

        v.speed_kph = round(smoothed, 1)
        v.is_speeding = v.speed_kph > SPEED_LIMIT_KPH

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
    def _avg_speed(vehicles: list[VehicleState]) -> float:
        s = [v.speed_kph for v in vehicles if v.speed_kph > 0]
        return round(sum(s) / len(s), 1) if s else 0.0

    @staticmethod
    def _class_counts(vehicles: list[VehicleState]) -> dict[str, int]:
        return dict(Counter(v.class_name for v in vehicles))

    def _gc(self, active: set[int]) -> None:
        # 재등장 시 연속성 유지: grace period 동안 _prev/_speed_ema 보존
        lost = set(self._prev) - active
        for tid in lost:
            self._lost_frames[tid] = self._lost_frames.get(tid, 0) + 1
            if self._lost_frames[tid] > GC_GRACE_FRAMES:
                self._prev.pop(tid, None)
                self._dwell.pop(tid, None)
                self._speed_ema.pop(tid, None)
                self._lost_frames.pop(tid, None)
        # 재등장한 track의 grace counter 초기화
        for tid in active:
            self._lost_frames.pop(tid, None)
