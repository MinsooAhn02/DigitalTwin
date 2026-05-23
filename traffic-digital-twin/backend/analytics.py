"""
analytics.py — 교통 지표 계산 엔진
  입력: VehicleState 리스트 (GPS 좌표 포함)
  출력: FrameAnalytics (JSON 직렬화 가능)

  경보: 과속(speed > limit) + 병목(연속 정지 >= threshold)
"""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from collections import defaultdict, Counter, deque
import math
import threading

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
        self.speed_limit_kph: float = SPEED_LIMIT_KPH
        self.road_bearing_deg: float | None = None  # 도로 진행 방향 (0=북, 시계방향)
        self.cam_lat: float | None = None
        self.cam_lon: float | None = None
        # track_id → (lat, lon, x_m, y_m, timestamp_s)
        self._prev: dict[int, tuple[float, float, float, float, float]] = {}
        self._dwell: dict[int, int] = defaultdict(int)
        # GC grace period: 미감지 프레임 수 (GC_GRACE_FRAMES 이후 실제 삭제)
        self._lost_frames: dict[int, int] = {}
        # 주차 확정된 픽셀 위치 목록 — track_id 변경에도 유지
        self._parked_positions: list[tuple[float, float]] = []
        # C: 슬라이딩 윈도우 — (x_m, y_m, timestamp_s) 이력
        self._pos_window: dict[int, deque] = {}
        # LineZone 교차 기반 per-vehicle direction
        self._vehicle_direction: dict[int, str] = {}

    def reset(self) -> None:
        """카메라 전환 시 상태 초기화."""
        with self._lock:
            self._prev.clear()
            self._dwell.clear()
            self._lost_frames.clear()
            self._parked_positions.clear()
            self._pos_window.clear()
            self._vehicle_direction.clear()

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

        # LineZone 교차 이벤트로 direction 갱신 (교차 순간에만 업데이트)
        for tid in crossed_in_ids:
            self._vehicle_direction[tid] = "In"
        for tid in crossed_out_ids:
            self._vehicle_direction[tid] = "Out"

        current_ts = timestamp_ms / 1000.0

        for v in vehicles:
            if self._is_near_parked(v.center_px):
                v.is_parked = True
                self._dwell[v.track_id] = PARKED_FRAMES_THRESHOLD
            self._speed(v, current_ts)
            self._dwell_update(v)
            # 교차 기록이 있으면 direction 적용
            if v.track_id in self._vehicle_direction:
                v.direction = self._vehicle_direction[v.track_id]

        # GPS 좌표를 도로 bearing 축에 투영 (지도 표시용)
        self._project_to_road_axis(vehicles)

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

    def _speed(self, v: VehicleState, current_ts: float) -> None:
        if v.is_parked:
            return

        tid = v.track_id

        # C: 슬라이딩 윈도우에 현재 위치 추가
        if tid not in self._pos_window:
            self._pos_window[tid] = deque(maxlen=SPEED_WINDOW_FRAMES)
        self._pos_window[tid].append((v.x_m, v.y_m, current_ts))

        window = self._pos_window[tid]
        if len(window) < 2:
            return

        # 윈도우 전체 변위가 너무 작으면 지터로 간주한다.
        oldest_x, oldest_y, oldest_ts = window[0]
        newest_x, newest_y, newest_ts = window[-1]
        dt = newest_ts - oldest_ts
        if dt <= 0:
            return

        dist_m = math.hypot(newest_x - oldest_x, newest_y - oldest_y)

        if dist_m < SPEED_JITTER_THRESHOLD_M:
            v.speed_kph = 0.0
            v.is_speeding = False
            return

        kph = self._estimate_speed_kph(window)

        # 물리적으로 불가능한 속도는 노이즈 — 이번 프레임 스킵
        if kph > MAX_REASONABLE_KPH:
            return

        v.speed_kph = round(kph, 1)
        v.is_speeding = v.speed_kph > self.speed_limit_kph

    def _project_to_road_axis(self, vehicles: list[VehicleState]) -> None:
        """차량 GPS를 도로 bearing 축에 투영 → 횡방향 흔들림 제거."""
        if self.road_bearing_deg is None or not vehicles:
            return
        b = math.radians(self.road_bearing_deg)
        sin_b, cos_b = math.sin(b), math.cos(b)

        # 현재 프레임 차량 위치의 중심을 도로축 기준점으로 사용
        ref_lat = sum(v.lat for v in vehicles) / len(vehicles)
        ref_lon = sum(v.lon for v in vehicles) / len(vehicles)
        R_lat = 110574.0
        R_lon = 111320.0 * math.cos(math.radians(ref_lat))

        for v in vehicles:
            dx = (v.lon - ref_lon) * R_lon  # 동(East) 오프셋 (m)
            dy = (v.lat - ref_lat) * R_lat  # 북(North) 오프셋 (m)
            along = dx * sin_b + dy * cos_b  # 도로 방향 성분
            v.lon = ref_lon + (along * sin_b) / R_lon
            v.lat = ref_lat + (along * cos_b) / R_lat

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
                self._lost_frames.pop(tid, None)
                self._pos_window.pop(tid, None)
        # 재등장한 track의 grace counter 초기화
        for tid in active:
            self._lost_frames.pop(tid, None)
