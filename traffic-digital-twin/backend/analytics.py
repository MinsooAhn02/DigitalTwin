"""
analytics.py — 교통 지표 계산 엔진
  입력: VehicleState 리스트 (GPS 좌표 포함)
  출력: FrameAnalytics (JSON 직렬화 가능)

  속도 계산:
    · Live 모드  → Bird-eye 미터 좌표 유클리드 거리
    · Replay 모드 → GPS Haversine 거리 (더 정확)
"""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from collections import defaultdict
import math

from config import (
    FPS,
    SPEED_LIMIT_KPH,
    TAILGATING_THRESHOLD_M,
    BOTTLENECK_DWELL_FRAMES,
    LOS_THRESHOLDS,
)

# ── Haversine 거리 계산 ───────────────────────────────────────────────
_R_EARTH = 6_371_000.0  # 지구 반지름 (m)

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
    headway_m:     float = float("inf")
    is_tailgating: bool  = False
    dwell_frames:  int   = 0
    is_bottleneck: bool  = False
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
        d = asdict(self)
        # float("inf") → JSON 직렬화 불가 → 치환
        for v in d.get("vehicles", []):
            if v.get("headway_m", 0) == float("inf"):
                v["headway_m"] = -1
        return d


# ── 분석 엔진 ──────────────────────────────────────────────────────────
class TrafficAnalytics:

    def __init__(self):
        # track_id → (lat, lon, x_m, y_m)
        self._prev: dict[int, tuple[float, float, float, float]] = {}
        self._dwell: dict[int, int] = defaultdict(int)

    def update(
        self,
        frame_id: int,
        timestamp_ms: float,
        vehicles: list[VehicleState],
        in_count: int,
        out_count: int,
    ) -> FrameAnalytics:

        active = {v.track_id for v in vehicles}
        self._gc(active)

        for v in vehicles:
            self._speed(v)
            self._dwell_update(v)

        self._headway(vehicles)

        result = FrameAnalytics(
            frame_id=frame_id,
            timestamp_ms=timestamp_ms,
            vehicles=[asdict(v) for v in vehicles],
            vehicle_count=len(vehicles),
            avg_speed_kph=self._avg_speed(vehicles),
            los_grade=self._los(len(vehicles)),
            in_count=in_count,
            out_count=out_count,
            class_counts=self._class_counts(vehicles),
        )

        for v in vehicles:
            self._prev[v.track_id] = (v.lat, v.lon, v.x_m, v.y_m)

        # inf → -1 for JSON
        for vd in result.vehicles:
            if vd.get("headway_m") == float("inf"):
                vd["headway_m"] = -1

        return result

    # ──────────────────────────────────────────────────────────────────
    def _speed(self, v: VehicleState) -> None:
        prev = self._prev.get(v.track_id)
        if prev is None:
            return
        plat, plon, px_m, py_m = prev

        # GPS가 유효하면 Haversine 우선, 아니면 미터 좌표
        if plat != 0.0 and v.lat != 0.0:
            dist_m = haversine_m(plat, plon, v.lat, v.lon)
        else:
            dist_m = math.hypot(v.x_m - px_m, v.y_m - py_m)

        v.speed_kph = round(dist_m * FPS * 3.6, 1)
        v.is_speeding = v.speed_kph > SPEED_LIMIT_KPH

    def _dwell_update(self, v: VehicleState) -> None:
        self._dwell[v.track_id] += 1
        v.dwell_frames = self._dwell[v.track_id]
        v.is_bottleneck = v.dwell_frames >= BOTTLENECK_DWELL_FRAMES

    def _headway(self, vehicles: list[VehicleState]) -> None:
        if len(vehicles) < 2:
            return
        by_lane: dict[int, list[VehicleState]] = defaultdict(list)
        for v in vehicles:
            by_lane[v.lane_id].append(v)
        for group in by_lane.values():
            sorted_v = sorted(group, key=lambda v: v.y_m)
            for i, v in enumerate(sorted_v):
                if i == 0:
                    continue
                leader = sorted_v[i - 1]
                v.headway_m = round(
                    haversine_m(v.lat, v.lon, leader.lat, leader.lon), 2
                ) if v.lat != 0 else math.hypot(v.x_m - leader.x_m, v.y_m - leader.y_m)
                v.is_tailgating = 0 < v.headway_m < TAILGATING_THRESHOLD_M

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
        c: dict[str, int] = defaultdict(int)
        for v in vehicles:
            c[v.class_name] += 1
        return dict(c)

    def _gc(self, active: set[int]) -> None:
        lost = set(self._prev) - active
        for tid in lost:
            self._prev.pop(tid, None)
            self._dwell.pop(tid, None)
