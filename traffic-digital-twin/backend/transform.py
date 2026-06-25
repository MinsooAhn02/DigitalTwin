"""
transform.py — Perspective Transform (픽셀 → 실세계 좌표)
  · OpenCV getPerspectiveTransform으로 단응행렬(Homography) 계산
  · pixel_to_gps()  : 픽셀 (u, v) → (lat, lon)
  · pixel_to_meter(): 픽셀 (u, v) → (x_m, y_m)  속도 계산용
  · update_from_calibration(): 사용자 4-point 캘리브레이션으로 행렬 재계산
  · auto_calibrate_from_frame(): 차선 감지(Hough) 기반 자동 원근 보정
"""

import json
import logging
import math
from collections import deque
from pathlib import Path

import numpy as np
import cv2
from config import PIXEL_POINTS, GPS_POINTS, REAL_WORLD_WIDTH_M, REAL_WORLD_HEIGHT_M, CAMERA_BEARING_DEG
from config import POSE_RESIDUAL_MAX_PX, SCALE_MIN_OBS, FAR_CAP_M
import math
import camera_pose

CALIBRATION_PATH = Path(__file__).parent / "calibration_data.json"

logger = logging.getLogger(__name__)

# 차종별 알려진 폭 (m) — vehicle apparent size calibration에 사용
VEHICLE_WIDTHS_M: dict[str, float] = {"car": 1.8, "truck": 2.5, "bus": 2.5}


def _line_intersection(l1: tuple, l2: tuple) -> tuple[float, float] | None:
    """두 직선(x1,y1,x2,y2)의 교점. 평행하면 None."""
    x1, y1, x2, y2 = l1
    x3, y3, x4, y4 = l2
    denom = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if abs(denom) < 1e-6:
        return None
    t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / denom
    return x1 + t * (x2 - x1), y1 + t * (y2 - y1)


class PerspectiveTransformer:
    """
    캘리브레이션 포인트 4쌍으로 단응행렬을 사전 계산하고,
    이후 픽셀 좌표를 GPS 또는 미터 좌표로 변환한다.
    """

    def __init__(
        self,
        pixel_pts: list[list[float]] = PIXEL_POINTS,
        gps_pts: list[list[float]]   = GPS_POINTS,
        real_w_m: float = REAL_WORLD_WIDTH_M,
        real_h_m: float = REAL_WORLD_HEIGHT_M,
    ):
        src = np.float32(pixel_pts)          # (4, 2) 픽셀
        dst_gps = np.float32(gps_pts)        # (4, 2) GPS

        # ── GPS 단응행렬 ──────────────────────────────────────────────
        self._H_gps, _ = cv2.findHomography(src, dst_gps)

        # ── 미터 단응행렬 (Bird's-eye view 기준) ─────────────────────
        dst_meter = np.float32([
            [0,        0       ],
            [real_w_m, 0       ],
            [real_w_m, real_h_m],
            [0,        real_h_m],
        ])
        self._H_meter, _ = cv2.findHomography(src, dst_meter)

        # 카메라 베어링 보정을 위한 캐시
        self._bearing_rad: float = math.radians(CAMERA_BEARING_DEG)
        gps_arr = np.float32(gps_pts)
        self._gps_center_lat: float = float(np.mean(gps_arr[:, 0]))
        self._gps_center_lon: float = float(np.mean(gps_arr[:, 1]))
        self._is_calibrated: bool = False  # 4-point 사용자 캘리브레이션 여부

        # ── Vehicle apparent-size scale model ──────────────────────────
        self._scale_obs: deque[tuple[float, float]] = deque(maxlen=200)  # (v_px, scale_m/px)
        self._scale_model: tuple[float, float] | None = None             # (B, C): 1/scale = B*v + C
        self._scale_obs_since_fit: int = 0
        self._speed_corr_cache: dict[int, float] = {}  # round(v_px) → corr factor
        self._frame_h: int = 0
        self._frame_w: int = 0

        # ── Road-model 카메라 포즈 (camera_pose.solve_pose) ─────────────
        self._pose: camera_pose.Pose | None = None         # 현재 적용된 포즈
        self._pose_residual: float = float("inf")          # 마지막 솔브 reprojection RMS(px)
        self._pose_prior: camera_pose.Pose | None = None   # 저장값 seed (다음 세션 재사용)

        # ── 도로 중심선 기반 곡선 GPS 매핑 ──────────────────────────────
        # 단일 Homography의 직선 근사 대신, 도로 곡선을 따라 차량 GPS를 계산.
        # Stage 1: pixel → (x_m, y_m) via H_meter  (기존, 평면 근사)
        # Stage 2: (x_m, y_m) → (d_along, d_lateral) → road_pts 보간 → GPS
        self._road_pts: list[list[float]] | None = None    # 도로 중심선 [[lat,lon],...]
        self._road_cum_dist: list[float]  | None = None    # 누적 거리 (m)
        self._snap_along_m:  float        | None = None    # road_pts[0]에서 snap까지 거리
        self._snap_meter_x:  float        | None = None    # snap의 H_meter x좌표 (동쪽)
        self._snap_meter_y:  float        | None = None    # snap의 H_meter y좌표 (북쪽)
        self._curve_bearing_rad: float = 0.0               # 도로 방위각 (라디안)
        self._curve_dir_sign: float = 1.0

    # ──────────────────────────────────────────────────────────────────
    def _transform_point(self, H: np.ndarray, u: float, v: float) -> tuple[float, float]:
        result = cv2.perspectiveTransform(np.float32([[[u, v]]]), H)
        return float(result[0, 0, 0]), float(result[0, 0, 1])

    def _batch_transform(
        self, H: np.ndarray, points: list[tuple[float, float]]
    ) -> list[tuple[float, float]]:
        if not points:
            return []
        arr = np.float32([[p] for p in points])
        res = cv2.perspectiveTransform(arr, H)
        return [(float(r[0, 0]), float(r[0, 1])) for r in res]

    # ── 도로 중심선 곡선 GPS 매핑 헬퍼 ────────────────────────────────

    def set_road_corridor(
        self,
        road_pts: list[list[float]] | None,
        snap_along_m: float | None,
    ) -> None:
        """도로 중심선 포인트를 설정. 이후 pixel_to_gps가 곡선 매핑을 사용.

        road_pts: [[lat, lon], ...] F→T 순서, nodelink shape_pts 기반
        snap_along_m: road_pts[0]에서 snap(카메라 도로 투영점)까지의 거리(m)
        """
        if not road_pts or len(road_pts) < 2 or snap_along_m is None:
            self._road_pts = None
            self._road_cum_dist = None
            self._snap_along_m = None
            self._curve_dir_sign = 1.0
            return
        self._road_pts = road_pts
        self._snap_along_m = float(snap_along_m)
        cum: list[float] = [0.0]
        for i in range(1, len(road_pts)):
            R_lon = 111320.0 * math.cos(math.radians(road_pts[i - 1][0]))
            cum.append(cum[-1] + math.hypot(
                (road_pts[i][0] - road_pts[i - 1][0]) * 110574.0,
                (road_pts[i][1] - road_pts[i - 1][1]) * R_lon,
            ))
        self._road_cum_dist = cum
        self._refresh_curve_direction_sign()
        logger.info(
            "도로 중심선 설정: %d점 %.1fm, snap=%.1fm 위치",
            len(road_pts), cum[-1], snap_along_m,
        )

    def _refresh_curve_direction_sign(self) -> None:
        """Map camera-view distance to the F->T road_pts arc direction."""
        if (self._road_pts is None or self._road_cum_dist is None
                or self._snap_along_m is None):
            self._curve_dir_sign = 1.0
            return
        road_ft_bearing = self._road_bearing_at(self._snap_along_m)
        view_bearing = math.degrees(self._curve_bearing_rad) % 360.0
        diff = abs((view_bearing - road_ft_bearing + 180.0) % 360.0 - 180.0)
        self._curve_dir_sign = 1.0 if diff < 90.0 else -1.0

    def _road_interp(self, arc: float) -> tuple[float, float]:
        """arc 거리(m)에 해당하는 도로 중심선 GPS를 보간."""
        pts, cum = self._road_pts, self._road_cum_dist  # type: ignore[assignment]
        arc = max(0.0, min(cum[-1], arc))
        for i in range(len(cum) - 1):
            if cum[i] <= arc <= cum[i + 1]:
                frac = (arc - cum[i]) / max(1e-9, cum[i + 1] - cum[i])
                return (
                    pts[i][0] + frac * (pts[i + 1][0] - pts[i][0]),
                    pts[i][1] + frac * (pts[i + 1][1] - pts[i][1]),
                )
        return float(pts[-1][0]), float(pts[-1][1])

    def _road_bearing_at(self, arc: float) -> float:
        """arc 거리(m) 위치의 도로 진행 방위각(0=N, 시계방향)."""
        pts, cum = self._road_pts, self._road_cum_dist  # type: ignore[assignment]
        for i in range(len(cum) - 1):
            if cum[i] <= arc <= cum[i + 1]:
                la1, lo1 = pts[i][0], pts[i][1]
                la2, lo2 = pts[i + 1][0], pts[i + 1][1]
                break
        else:
            la1, lo1 = pts[-2][0], pts[-2][1]
            la2, lo2 = pts[-1][0], pts[-1][1]
        R_lon = 111320.0 * math.cos(math.radians(la1))
        return math.degrees(math.atan2(
            (lo2 - lo1) * R_lon,
            (la2 - la1) * 110574.0,
        )) % 360.0

    @staticmethod
    def _image_curve_sign(
        left_pts: list[tuple[float, float]],
        right_pts: list[tuple[float, float]],
        frame_w: int,
    ) -> tuple[int | None, float]:
        """Return visual road curve sign: -1=left, +1=right, None=unclear."""
        if len(left_pts) < 3 or len(right_pts) < 3:
            return None, 0.0

        centers = sorted(
            [(l[0], (l[1] + r[1]) / 2.0) for l, r in zip(left_pts, right_pts)],
            key=lambda p: p[0],
            reverse=True,
        )
        near_y, near_x = centers[0]
        far_y, far_x = centers[-1]
        y_span = near_y - far_y
        if y_span < 20:
            return None, 0.0

        residuals: list[float] = []
        for y, x in centers[1:-1]:
            s = (near_y - y) / y_span
            straight_x = near_x + s * (far_x - near_x)
            residuals.append(x - straight_x)

        if not residuals:
            return None, 0.0

        curve_px = float(np.median(residuals))
        threshold_px = max(6.0, frame_w * 0.012)
        if abs(curve_px) < threshold_px:
            return None, curve_px
        return (1 if curve_px > 0 else -1), curve_px

    def _map_curve_sign_for_dir(
        self,
        dir_sign: float,
        lookahead_m: float = 90.0,
    ) -> tuple[int | None, float]:
        """Return map curve sign for a candidate view direction: -1=left, +1=right."""
        if (self._road_pts is None or self._road_cum_dist is None
                or self._snap_along_m is None):
            return None, 0.0

        total = self._road_cum_dist[-1]
        available = (total - self._snap_along_m) if dir_sign > 0 else self._snap_along_m
        if available < 25.0:
            return None, 0.0

        far_d = min(lookahead_m, available)
        if far_d < 25.0:
            return None, 0.0

        base_lat, base_lon = self._road_interp(self._snap_along_m)
        mid_lat, mid_lon = self._road_interp(self._snap_along_m + dir_sign * far_d * 0.5)
        far_lat, far_lon = self._road_interp(self._snap_along_m + dir_sign * far_d)

        base_bearing = self._road_bearing_at(self._snap_along_m)
        if dir_sign < 0:
            base_bearing = (base_bearing + 180.0) % 360.0
        b = math.radians(base_bearing)
        sin_b, cos_b = math.sin(b), math.cos(b)
        r_lon = 111320.0 * math.cos(math.radians(base_lat))

        def lateral_m(lat: float, lon: float) -> float:
            dx = (lon - base_lon) * r_lon
            dy = (lat - base_lat) * 110574.0
            return dx * cos_b - dy * sin_b

        curve_m = 0.35 * lateral_m(mid_lat, mid_lon) + 0.65 * lateral_m(far_lat, far_lon)
        threshold_m = max(1.5, far_d * 0.025)
        if abs(curve_m) < threshold_m:
            return None, curve_m
        return (1 if curve_m > 0 else -1), curve_m

    def _curvature_flip_candidate(
        self,
        image_sign: int | None,
        lookahead_m: float = 90.0,
    ) -> tuple[bool | None, dict]:
        """Choose F->T/T->F by matching image curve sign to map curve sign."""
        ft_sign, ft_curve_m = self._map_curve_sign_for_dir(1.0, lookahead_m)
        tf_sign, tf_curve_m = self._map_curve_sign_for_dir(-1.0, lookahead_m)
        info: dict = {
            "map_ft_sign": ft_sign,
            "map_tf_sign": tf_sign,
            "map_ft_curve_m": ft_curve_m,
            "map_tf_curve_m": tf_curve_m,
        }

        if image_sign is None:
            return None, info

        ft_matches = ft_sign == image_sign
        tf_matches = tf_sign == image_sign
        if ft_matches and not tf_matches:
            return False, info
        if tf_matches and not ft_matches:
            return True, info
        return None, info


    
    def _pixel_to_gps_curved(self, u: float, v: float):
        """2단계 곡선 GPS 변환 (수직투영 방식 v2 — snap 방향 가드 포함)."""
        if (self._road_pts is None or self._road_cum_dist is None
                or self._snap_along_m is None
                or self._snap_meter_x is None or self._snap_meter_y is None):
            return None

        x_m, y_m = self._transform_point(self._H_meter, u, v)
        dx = x_m - self._snap_meter_x
        dy = y_m - self._snap_meter_y
        enu_lat, enu_lon = self._enu_from_snap_to_latlon(dx, dy)

        arc, lateral = self._project_latlon_to_centreline(enu_lat, enu_lon)
        # H_meter 캘리브레이션 오차는 snap에서 멀수록 선형 누적됨.
        # 도로 반폭(±6m) 초과 lateral은 오차로 판단하여 클램프.
        lateral = max(-6.0, min(6.0, lateral))

        center_lat, center_lon = self._road_interp(arc)
        b_local = math.radians(self._road_bearing_at(arc))
        R_lat = 110574.0
        R_lon = 111320.0 * math.cos(math.radians(center_lat))
        east_off  =  math.cos(b_local) * lateral
        north_off = -math.sin(b_local) * lateral

        return (
            center_lat + north_off / R_lat,
            center_lon + east_off  / R_lon,
        )

    def _enu_from_snap_to_latlon(self, dx_m: float, dy_m: float) -> tuple:
        """snap 기준 east/north(m) → (lat, lon)."""
        R_lat = 110574.0
        R_lon = 111320.0 * math.cos(math.radians(self._gps_center_lat))
        return (
            self._gps_center_lat + dy_m / R_lat,
            self._gps_center_lon + dx_m / R_lon,
        )

    def _project_latlon_to_centreline(self, lat: float, lon: float) -> tuple:
        """(lat, lon)을 중심선 polyline에 수직투영.

        반환 (arc_m, signed_lateral_m) — arc는 road_pts[0]에서의 누적거리, lateral은 오른쪽 양수.
        """
        pts = self._road_pts
        cum = self._road_cum_dist
        R_lat = 110574.0
        R_lon = 111320.0 * math.cos(math.radians(lat))

        best_d2 = float("inf")
        best_arc = 0.0
        best_lat_off = 0.0

        for i in range(len(pts) - 1):
            f_lat, f_lon = pts[i]
            t_lat, t_lon = pts[i + 1]
            bx = (t_lon - f_lon) * R_lon
            by = (t_lat - f_lat) * R_lat
            px = (lon - f_lon) * R_lon
            py = (lat - f_lat) * R_lat
            seg_sq = bx * bx + by * by
            if seg_sq < 1e-9:
                continue
            u = (px * bx + py * by) / seg_sq
            u = max(0.0, min(1.0, u))
            sx, sy = u * bx, u * by
            d2 = (px - sx) ** 2 + (py - sy) ** 2
            if d2 < best_d2:
                best_d2 = d2
                best_arc = cum[i] + u * math.sqrt(seg_sq)
                cross = bx * py - by * px  # 양수=왼쪽(ENU 기준)
                best_lat_off = math.copysign(math.sqrt(d2), -cross)  # 오른쪽 양수

        return best_arc, best_lat_off

    # ── 좌표 변환 공개 API ───────────────────────────────────────────────

    def pixel_to_gps(self, u: float, v: float) -> tuple[float, float]:
        """픽셀 (u, v) → (latitude, longitude).

        도로 중심선이 설정돼 있고 수동 캘리브레이션이 아닐 때는
        2단계 곡선 매핑을 사용해 도로 곡선을 따른 GPS를 반환.
        """
        if not self._is_calibrated and self._road_pts is not None:
            result = self._pixel_to_gps_curved(u, v)
            if result is not None:
                return result

        lat, lon = self._transform_point(self._H_gps, u, v)
        if self._bearing_rad == 0.0:
            return lat, lon
        # GPS 중심 기준 델타를 bearing 각도로 회전 (레거시 경로용)
        dlat = lat - self._gps_center_lat
        dlon = lon - self._gps_center_lon
        cos_b = math.cos(self._bearing_rad)
        sin_b = math.sin(self._bearing_rad)
        new_dlat = cos_b * dlat - sin_b * dlon
        new_dlon = sin_b * dlat + cos_b * dlon
        return self._gps_center_lat + new_dlat, self._gps_center_lon + new_dlon

    def pixel_to_meter(self, u: float, v: float) -> tuple[float, float]:
        """픽셀 (u, v) → (x_m, y_m) — 속도 계산용 실세계 미터 좌표.

        H_meter 호모그래피 결과를 그대로 반환한다. scale model의 depth-varying 계수를
        절대 좌표에 곱하면 프레임 간 위치 불연속(speed=0 고착)이 발생하므로 적용하지 않는다.
        """
        return self._transform_point(self._H_meter, u, v)

    def batch_pixel_to_gps(
        self, points: list[tuple[float, float]]
    ) -> list[tuple[float, float]]:
        """여러 픽셀 좌표를 GPS로 변환. 곡선 매핑 활성화 시 각 점에 개별 적용."""
        if not self._is_calibrated and self._road_pts is not None:
            results = []
            for u, v in points:
                r = self._pixel_to_gps_curved(u, v)
                if r is not None:
                    results.append(r)
                else:
                    results.append(self._transform_point(self._H_gps, u, v))
            return results
        return self._batch_transform(self._H_gps, points)

    @staticmethod
    def _gps_pts_to_local_meters(gps_pts: np.ndarray) -> np.ndarray:
        """GPS 4점을 첫 번째 점 기준 로컬 ENU 미터 좌표로 변환."""
        R = 6_371_000.0
        lat0, lon0 = float(gps_pts[0, 0]), float(gps_pts[0, 1])
        lat0_r = math.radians(lat0)
        pts = []
        for lat, lon in gps_pts:
            x = R * math.radians(lon - lon0) * math.cos(lat0_r)
            y = R * math.radians(lat - lat0)
            pts.append([x, y])
        return np.float32(pts)

    @property
    def is_calibrated(self) -> bool:
        return self._is_calibrated

    # ── Phase 3: ROI → GPS ring ─────────────────────────────────────────

    def roi_to_gps_ring(
        self,
        roi_norm_pts: list[list[float]],
        w: int,
        h: int,
    ) -> list[list[float]] | None:
        """ROI 정규화 좌표(0~1)를 GPS [[lat,lon],...] ring으로 변환.

        수평선 초과(d > FAR_CAP_M) 또는 카메라 뒤(d < 0.5m) 꼭짓점은
        v 좌표를 이진 탐색으로 조정해 유효 깊이 범위 안으로 clamp.
        """
        if self._H_meter is None:
            return None
        ring: list[list[float]] = []
        for nx, ny in roi_norm_pts:
            u = nx * w
            v = self._clamp_v_to_depth(nx * w, ny * h, float(h))
            lat, lon = self.pixel_to_gps(u, v)
            ring.append([lat, lon])
        return ring if len(ring) >= 3 else None

    def _clamp_v_to_depth(self, u: float, v: float, h: float) -> float:
        """v 좌표를 [0.5m, FAR_CAP_M] 깊이 범위로 조정 (이진 탐색).

        일반 도로 카메라에서 v 증가 → 깊이 감소 (아래로 갈수록 가까움).
        """
        x_m, y_m = self.pixel_to_meter(u, v)
        d = math.hypot(x_m, y_m)
        if 0.5 <= d <= FAR_CAP_M:
            return v
        # v_lo → v_hi 범위에서 깊이가 FAR_CAP_M 이하가 되는 v 탐색
        # (너무 멀거나 음수: v를 아래로 이동해 깊이 줄임)
        v_lo = max(v, 0.0)
        v_hi = h - 1.0
        if v_lo >= v_hi:
            return v_hi
        for _ in range(20):
            v_mid = (v_lo + v_hi) * 0.5
            x_m, y_m = self.pixel_to_meter(u, v_mid)
            d_mid = math.hypot(x_m, y_m)
            if d_mid > FAR_CAP_M or d_mid < 0.5:
                v_lo = v_mid  # 아직 범위 밖 → v 더 증가
            else:
                v_hi = v_mid  # 유효 범위 → v 감소해 상단 경계 좁힘
        return (v_lo + v_hi) * 0.5

    # ── Vehicle apparent-size scale model ──────────────────────────────

    def accumulate_scale_obs(
        self,
        v: float,
        bbox_w_px: float,
        class_name: str,
        frame_h: int,
        frame_w: int,
    ) -> None:
        """차량 bbox 폭 관측을 scale model 피팅용 버퍼에 추가."""
        real_w = VEHICLE_WIDTHS_M.get(class_name)
        if real_w is None or bbox_w_px < 20.0:
            return
        self._frame_h, self._frame_w = frame_h, frame_w
        self._scale_obs.append((float(v), real_w / bbox_w_px))
        self._scale_obs_since_fit += 1

    def fit_scale_model(self, min_obs: int = SCALE_MIN_OBS) -> bool:
        """누적 관측으로 1/scale = B*v + C 선형 모델 피팅.

        성공 시 _scale_model 업데이트 후 True 반환.
        유효성 조건: B > 0, vp_y = -C/B ∈ (0, 0.7*frame_h).

        min_obs: 적응형 최소 관측수 (교통량 적으면 낮춰 호출 — 한계2 C). 이제 스케일
        모델은 포즈 캘리브의 *보조*(미세보정)이므로 임계값을 낮춰도 안전.
        """
        if len(self._scale_obs) < min_obs:
            return False
        vs    = np.array([o[0] for o in self._scale_obs], dtype=np.float64)
        invsc = np.array([1.0 / o[1] for o in self._scale_obs], dtype=np.float64)
        A = np.column_stack([vs, np.ones_like(vs)])
        result = np.linalg.lstsq(A, invsc, rcond=None)
        B, C = float(result[0][0]), float(result[0][1])
        if B <= 0:
            logger.debug("fit_scale_model: B=%.5f <= 0, 피팅 거부", B)
            return False
        vp_y = -C / B
        if not (0 < vp_y < 0.7 * self._frame_h):
            logger.debug("fit_scale_model: vp_y=%.1f 유효 범위 벗어남, 피팅 거부", vp_y)
            return False
        self._scale_model = (B, C)
        self._scale_obs_since_fit = 0
        self._speed_corr_cache.clear()
        logger.info("fit_scale_model 완료: B=%.5f C=%.3f vp_y=%.1fpx (obs=%d)",
                    B, C, vp_y, len(self._scale_obs))
        return True

    def _scale_correction_at(self, v: float) -> float:
        """픽셀 y=v에서의 스케일 보정 계수 (fitted/homography). [0.3, 3.0] 클램프."""
        if self._scale_model is None or self._frame_w == 0:
            return 1.0
        B, C = self._scale_model
        denom = B * v + C
        if denom <= 0:
            return 1.0
        fitted_scale = 1.0 / denom
        w2 = self._frame_w / 2.0
        x0m, _ = self._transform_point(self._H_meter, w2, v)
        x1m, _ = self._transform_point(self._H_meter, w2 + 1.0, v)
        h_scale = abs(x1m - x0m)
        if h_scale < 1e-6:
            return 1.0
        return min(max(fitted_scale / h_scale, 0.6), 1.8)

    def load_scale_params(self, params: dict) -> None:
        """vehicle_calib.json 엔트리에서 scale model 복원."""
        try:
            B, C = float(params["B"]), float(params["C"])
            self._scale_model = (B, C)
            self._frame_h = int(params.get("frame_h", self._frame_h))
            self._frame_w = int(params.get("frame_w", self._frame_w))
            self._speed_corr_cache.clear()
            logger.info("load_scale_params: B=%.5f C=%.3f (frame %dx%d)",
                        B, C, self._frame_w, self._frame_h)
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("load_scale_params 실패: %s", exc)

    def get_scale_params(self) -> dict | None:
        """현재 scale model을 직렬화 가능한 dict로 반환. 모델 없으면 None."""
        if self._scale_model is None:
            return None
        B, C = self._scale_model
        return {"B": B, "C": C, "frame_h": self._frame_h, "frame_w": self._frame_w}

    def reset_scale_obs(self, clear_model: bool = False) -> None:
        """카메라 전환 시 관측 버퍼 초기화.

        clear_model=True: 이전 카메라 모델도 제거 (load_scale_params로 새 모델 로드 전).
        """
        self._scale_obs.clear()
        self._scale_obs_since_fit = 0
        self._speed_corr_cache.clear()
        if clear_model:
            self._scale_model = None

    def speed_correction_at(self, v_px: float, frame_h: int = 0) -> float:
        """픽셀 y=v_px에서 속도 보정 계수 반환.

        _scale_correction_at의 velocity-domain 공개 래퍼. 좌표가 아닌 속도값에 곱한다.
        frame_h가 0이 아니고 피팅 당시 해상도(_frame_h)와 다르면 좌표를 환산해 적용.
        모델 미확보 시 1.0 반환.
        """
        if self._scale_model is None:
            return 1.0
        # 해상도 불일치 환산 (ws/detect 경로가 다른 해상도로 보낼 수 있음)
        actual_v = v_px
        if frame_h > 0 and self._frame_h > 0 and frame_h != self._frame_h:
            actual_v = v_px * self._frame_h / frame_h
        key = round(actual_v)
        cached = self._speed_corr_cache.get(key)
        if cached is not None:
            return cached
        result = self._scale_correction_at(actual_v)
        self._speed_corr_cache[key] = result
        return result

    # ── Road-model 카메라 포즈 영속화 / 적용 ───────────────────────────

    def get_pose_params(self) -> dict | None:
        """현재 포즈를 직렬화 가능한 dict로 반환. 포즈 없으면 None."""
        if self._pose is None:
            return None
        d = self._pose.to_dict()
        d["residual_px"] = round(self._pose_residual, 2)
        d["frame_w"] = self._frame_w
        d["frame_h"] = self._frame_h
        return d

    def load_pose_params(self, params: dict) -> None:
        """camera_pose.json 엔트리를 prior로 복원 (solve_pose 초기값 seed)."""
        try:
            self._pose_prior = camera_pose.Pose(
                H_m=float(params["H_m"]),
                pitch_deg=float(params["pitch_deg"]),
                yaw_deg=float(params["yaw_deg"]),
                focal_px=float(params["focal_px"]),
                x0_m=float(params["x0_m"]),
            )
            logger.info(
                "load_pose_params: H=%.1f pitch=%.1f yaw=%.1f f=%.0f (prior seed)",
                self._pose_prior.H_m, self._pose_prior.pitch_deg,
                self._pose_prior.yaw_deg, self._pose_prior.focal_px,
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("load_pose_params 실패: %s", exc)

    def apply_prior_pose(
        self, road_width_m: float, bearing_deg: float, frame_wh: tuple[int, int],
    ) -> bool:
        """저장된 prior 포즈를 직접 적용 (엣지 약함 + prior 있음 — 결정트리 3단계)."""
        if self._pose_prior is None:
            return False
        road_model = camera_pose.RoadModel(
            road_width_m=road_width_m, bearing_deg=bearing_deg,
            snap_lat=self._gps_center_lat, snap_lon=self._gps_center_lon,
        )
        corners = camera_pose.pose_to_corners(self._pose_prior, road_model, frame_wh)
        if corners is None:
            return False
        src_pts, dst_gps = corners
        if self._apply_homography_corners(src_pts, dst_gps, bearing_deg):
            self._pose = self._pose_prior
            logger.info("prior 포즈 직접 적용: H=%.1fm pitch=%.1f° (엣지 폴백)",
                        self._pose_prior.H_m, self._pose_prior.pitch_deg)
            return True
        return False

    def reset_pose(self, clear_prior: bool = True) -> None:
        """카메라 전환 시 포즈 상태 초기화. clear_prior=False면 prior seed 유지."""
        self._pose = None
        self._pose_residual = float("inf")
        if clear_prior:
            self._pose_prior = None

    @property
    def pose_residual(self) -> float:
        return self._pose_residual

    def update_from_calibration(
        self,
        pixel_pts: list[list[float]],
        gps_pts: list[list[float]],
    ) -> None:
        """
        사용자가 지정한 4쌍의 (pixel → GPS) 대응점으로 Homography 재계산.
        pixel_pts: [[u,v], ...] (4개, 정규화 아님 — 실제 픽셀)
        gps_pts:   [[lat,lon], ...] (4개)
        """
        if len(pixel_pts) != 4 or len(gps_pts) != 4:
            raise ValueError("정확히 4쌍의 대응점이 필요합니다")
        src = np.float32(pixel_pts)
        dst_gps = np.float32(gps_pts)
        H, _ = cv2.findHomography(src, dst_gps)
        if H is None:
            raise RuntimeError("Homography 계산 실패 — 점들이 동일선상에 있을 수 있습니다")
        self._H_gps = H
        self._gps_center_lat = float(np.mean(dst_gps[:, 0]))
        self._gps_center_lon = float(np.mean(dst_gps[:, 1]))
        self._bearing_rad = 0.0

        # 캘리브레이션 GPS점으로 로컬 미터 좌표계 계산 → H_meter 재계산
        dst_meter = self._gps_pts_to_local_meters(dst_gps)
        H_m, _ = cv2.findHomography(src, dst_meter)
        if H_m is not None:
            self._H_meter = H_m

        self._is_calibrated = True
        logger.info(
            "캘리브레이션 적용 완료: pixel=%s gps=%s",
            pixel_pts, gps_pts,
        )

    def _apply_homography_corners(
        self,
        src_pts: np.ndarray,
        dst_gps: np.ndarray,
        bearing_deg: float,
    ) -> bool:
        """이미지 4코너 ↔ GPS 4코너 대응으로 _H_gps/_H_meter + 곡선매핑 상태 갱신.

        포즈 캘리브와 휴리스틱 캘리브가 공유하는 공통 꼬리 로직.
        """
        H_gps, _ = cv2.findHomography(src_pts, dst_gps)
        if H_gps is None:
            return False
        self._H_gps = H_gps
        dst_meter = self._gps_pts_to_local_meters(np.float32(dst_gps))
        H_m, _ = cv2.findHomography(src_pts, dst_meter)
        if H_m is not None:
            self._H_meter = H_m
        self._speed_corr_cache.clear()
        self._bearing_rad = 0.0
        self._is_calibrated = False

        # snap(=_gps_center)의 H_meter ENU 좌표 갱신 (곡선 GPS 매핑용)
        _R = 6_371_000.0
        _tl_lat, _tl_lon = float(dst_gps[0][0]), float(dst_gps[0][1])
        _lat0_r = math.radians(_tl_lat)
        self._snap_meter_x = _R * math.radians(self._gps_center_lon - _tl_lon) * math.cos(_lat0_r)
        self._snap_meter_y = _R * math.radians(self._gps_center_lat - _tl_lat)
        self._curve_bearing_rad = math.radians(bearing_deg)
        self._refresh_curve_direction_sign()
        return True

    def auto_calibrate_from_frame(
        self,
        frame: np.ndarray,
        bearing_deg: float = 0.0,
        road_width_m: float = 7.0,
        fix_direction: bool = False,
        cam_lat: float | None = None,
        cam_lon: float | None = None,
    ) -> tuple[bool, float, dict | None]:
        """차선 감지(Hough)로 원근 파라미터 자동 추정.

        road_width_m: 노드링크 lanes × 차선폭(m) — 수평 스케일 기준값.
        반환: (성공여부, 실제사용된bearing_deg, calib_info_dict | None)
          calib_info: {cam_h_m, near_m, far_m, road_width_m, pitch_deg, road_length_m}
        """
        h, w = frame.shape[:2]
        self._frame_h, self._frame_w = h, w
        fy = h * 1.2  # assumed focal length (≈45° vFoV)

        # ── 1. Edge detection ────────────────────────────────────────
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
        blur = cv2.GaussianBlur(gray, (5, 5), 0)
        edges = cv2.Canny(blur, 50, 150)

        roi_top = int(h * 0.45)
        roi_mask = np.zeros_like(edges)
        cv2.fillPoly(
            roi_mask,
            [np.array([[0, roi_top], [w, roi_top], [w, h], [0, h]], dtype=np.int32)],
            255,
        )
        edges = cv2.bitwise_and(edges, roi_mask)

        # ── 2. Hough 직선 검출 ───────────────────────────────────────
        lines = cv2.HoughLinesP(
            edges, 1, np.pi / 180, threshold=35,
            minLineLength=int(h * 0.08), maxLineGap=int(h * 0.06),
        )
        if lines is None or len(lines) < 3:
            return False, bearing_deg, None

        diag: list[tuple] = []
        for l in lines:
            x1, y1, x2, y2 = l[0]
            if abs(y2 - y1) < 5:
                continue
            from_vert = math.degrees(math.atan2(abs(x2 - x1), abs(y2 - y1)))
            if from_vert < 60:
                diag.append((x1, y1, x2, y2))

        if len(diag) < 2:
            return False, bearing_deg, None

        # ── 3. Multi-level road edge detection ───────────────────────
        # Sample at 5 y levels from bottom to upper ROI for robust linear fit.
        # Each line is extrapolated to the sample y; percentiles give left/right edge.
        sample_ratios = (1.0, 0.88, 0.76, 0.64, 0.52)
        left_pts:  list[tuple[float, float]] = []  # (y, x)
        right_pts: list[tuple[float, float]] = []

        for ratio in sample_ratios:
            y_lvl = min(int(h * ratio), h - 1)
            xs_at: list[float] = []
            for x1, y1, x2, y2 in diag:
                dy = y2 - y1
                if dy == 0:
                    continue
                t = (y_lvl - y1) / dy
                if -0.5 <= t <= 2.0:
                    xv = x1 + t * (x2 - x1)
                    if -w * 0.1 <= xv <= w * 1.1:
                        xs_at.append(xv)
            if len(xs_at) >= 2:
                lx = float(np.percentile(xs_at, 15))
                rx = float(np.percentile(xs_at, 85))
                if rx - lx > w * 0.08:
                    left_pts.append((float(y_lvl), lx))
                    right_pts.append((float(y_lvl), rx))

        if len(left_pts) < 2:
            return False, bearing_deg, None

        # ── 4. Linear fit: x = a*y + b for left and right edges ─────
        # More robust than single-row detection; VP = intersection of the two fitted lines.
        ys_l = np.array([p[0] for p in left_pts])
        xs_l = np.array([p[1] for p in left_pts])
        ys_r = np.array([p[0] for p in right_pts])
        xs_r = np.array([p[1] for p in right_pts])

        A_l = np.column_stack([ys_l, np.ones_like(ys_l)])
        a_l, b_l = np.linalg.lstsq(A_l, xs_l, rcond=None)[0]
        A_r = np.column_stack([ys_r, np.ones_like(ys_r)])
        a_r, b_r = np.linalg.lstsq(A_r, xs_r, rcond=None)[0]

        # ── 5. VP from fitted line intersection ──────────────────────
        denom = a_l - a_r
        if abs(denom) > 1e-6:
            vp_y_fit = (b_r - b_l) / denom
            vp_x_fit = a_l * vp_y_fit + b_l
            if -h * 0.5 < vp_y_fit < h * 0.85 and -w * 0.3 < vp_x_fit < w * 1.3:
                vp_y = min(float(vp_y_fit), h * 0.55)
                vp_x = float(vp_x_fit)
            else:
                vp_y, vp_x = h * 0.35, w / 2.0
        else:
            # Parallel lines — fall back to pairwise Hough intersection median
            vp_cands: list[tuple[float, float]] = []
            for i in range(len(diag)):
                for j in range(i + 1, min(i + 8, len(diag))):
                    pt = _line_intersection(diag[i], diag[j])
                    if pt is not None:
                        px, py = pt
                        if -w * 0.5 < px < w * 1.5 and -h * 0.2 < py < h * 0.6:
                            vp_cands.append((px, py))
            vp_x = float(np.median([p[0] for p in vp_cands])) if len(vp_cands) >= 2 else w / 2.0
            vp_y_raw = float(np.median([p[1] for p in vp_cands])) if len(vp_cands) >= 2 else h * 0.35
            vp_y = min(vp_y_raw, h * 0.55)

        # ── 5b. VP + 지도 기반 카메라 방향 결정 ────────────────────────────
        # name_bearing 확정(fix_direction=True)이면 skip.
        # 그 외: VP 수평각(phi)과 카메라→snap 방위를 비교해 F→T / T→F 중 선택.
        #
        # 원리:
        #   도로의 먼 쪽이 VP 방향으로 수렴함.
        #   VP의 수평 오프셋 phi = atan2(vp_x - w/2, fy)
        #   → 도로 방향이 카메라 광축보다 phi만큼 오른쪽에 있음.
        #   따라서 카메라 방위 B_cam = B_road_toward_vp - phi.
        #   두 후보:
        #     cand_A: B_cam = bearing_deg - phi         (F→T 방향이 VP 방향)
        #     cand_B: B_cam = bearing_deg + 180° - phi  (T→F 방향이 VP 방향)
        #   카메라 GPS → snap GPS 방위(B_to_road)에 더 가까운 후보를 선택.
        #   (CCTV는 도로 쪽을 향해 설치되므로 B_cam ≈ B_to_road)
        image_curve_sign, image_curve_px = self._image_curve_sign(left_pts, right_pts, w)
        curve_flip, curve_info = self._curvature_flip_candidate(image_curve_sign)
        direction_source = "fixed" if fix_direction else "vp"

        if curve_flip is not None:
            direction_source = "curvature"
            if curve_flip:
                bearing_deg = (bearing_deg + 180.0) % 360.0
            logger.info(
                "curve direction: image=%s(%.1fpx) map_ft=%s(%.1fm) "
                "map_tf=%s(%.1fm) -> %s",
                image_curve_sign, image_curve_px,
                curve_info.get("map_ft_sign"), curve_info.get("map_ft_curve_m", 0.0),
                curve_info.get("map_tf_sign"), curve_info.get("map_tf_curve_m", 0.0),
                "T->F" if curve_flip else "F->T",
            )
        elif not fix_direction:
            phi_deg = math.degrees(math.atan2(vp_x - w / 2.0, fy))

            cand_a_cam = (bearing_deg - phi_deg) % 360.0          # F→T 방향이 VP
            cand_b_cam = (bearing_deg + 180.0 - phi_deg) % 360.0  # T→F 방향이 VP

            flip = False
            if (cam_lat is not None and cam_lon is not None
                    and self._gps_center_lat != 0.0):
                d_north = (self._gps_center_lat - cam_lat) * 110574.0
                d_east  = ((self._gps_center_lon - cam_lon)
                           * 111320.0 * math.cos(math.radians(cam_lat)))
                cam_dist = math.hypot(d_north, d_east)

                if cam_dist > 2.0:
                    b_to_road = math.degrees(math.atan2(d_east, d_north)) % 360.0
                    diff_a = abs(((cand_a_cam - b_to_road + 180) % 360) - 180)
                    diff_b = abs(((cand_b_cam - b_to_road + 180) % 360) - 180)
                    flip = diff_b < diff_a
                    logger.info(
                        "VP+지도 방향: phi=%.1f° cam→road=%.1f° "
                        "F→T_cam=%.1f°(Δ%.0f°) T→F_cam=%.1f°(Δ%.0f°) → %s",
                        phi_deg, b_to_road,
                        cand_a_cam, diff_a, cand_b_cam, diff_b,
                        "T→F 선택" if flip else "F→T 선택",
                    )
                else:
                    flip = vp_x > w * 0.55
                    logger.info("VP 방향 fallback(cam≈snap): vp_x=%.0f/%.0f → %s",
                                vp_x, w, "반전" if flip else "유지")
            else:
                flip = vp_x > w * 0.55
                logger.info("VP 방향 fallback(cam GPS 없음): vp_x=%.0f/%.0f → %s",
                            vp_x, w, "반전" if flip else "유지")

            if flip:
                bearing_deg = (bearing_deg + 180.0) % 360.0
                logger.info("bearing %.1f°로 반전", bearing_deg)

        # ── 5c. Road-model 포즈 솔버 (主 캘리브) ───────────────────────
        # 차선 엣지 + VP + 도로폭으로 카메라 물리 포즈를 역산(camera_pose.solve_pose).
        # 성공·품질충족 시 휴리스틱(섹션 6~10)을 건너뛰고 포즈→호모그래피로 즉시 반환.
        # 실패 시 아래 기존 휴리스틱으로 폴백(behavior 비퇴행).
        road_model = camera_pose.RoadModel(
            road_width_m=road_width_m,
            bearing_deg=bearing_deg,
            snap_lat=self._gps_center_lat,
            snap_lon=self._gps_center_lon,
        )
        pose, residual = camera_pose.solve_pose(
            left_pts, right_pts, (vp_x, vp_y), road_model, (w, h),
            prior=self._pose_prior,
        )
        if pose is not None and residual < POSE_RESIDUAL_MAX_PX:
            corners = camera_pose.pose_to_corners(pose, road_model, (w, h))
            if corners is not None:
                src_pts, dst_gps = corners
                if self._apply_homography_corners(src_pts, dst_gps, bearing_deg):
                    self._pose = pose
                    self._pose_residual = residual
                    # FOV 표시용 near/far (snap → 코너 지오데식 거리). dst_gps: TL,TR,BR,BL
                    def _dist_from_snap(latlon) -> float:
                        dn = (latlon[0] - self._gps_center_lat) * 110574.0
                        de = ((latlon[1] - self._gps_center_lon) * 111320.0
                              * math.cos(math.radians(self._gps_center_lat)))
                        return math.hypot(dn, de)
                    near_m = min(_dist_from_snap(dst_gps[2]), _dist_from_snap(dst_gps[3]))
                    far_m = max(_dist_from_snap(dst_gps[0]), _dist_from_snap(dst_gps[1]))
                    logger.info(
                        "포즈 캘리브 적용: H=%.1fm pitch=%.1f° yaw=%.1f° "
                        "residual=%.1fpx bearing=%.1f° near=%.1f far=%.1f",
                        pose.H_m, pose.pitch_deg, pose.yaw_deg, residual, bearing_deg,
                        near_m, far_m,
                    )
                    return True, bearing_deg, {
                        "method":           "pose",
                        "cam_h_m":          round(pose.H_m, 1),
                        "pitch_deg":        round(pose.pitch_deg, 1),
                        "yaw_deg":          round(pose.yaw_deg, 1),
                        "focal_px":         round(pose.focal_px, 1),
                        "residual_px":      round(residual, 2),
                        "road_width_m":     round(road_width_m, 1),
                        "near_m":           round(near_m, 1),
                        "far_m":            round(far_m, 1),
                        "road_length_m":    round(max(0.0, far_m - near_m), 1),
                        "direction_source": direction_source,
                        "image_curve_sign": image_curve_sign,
                    }
        logger.info("포즈 솔브 실패/품질미달(residual=%.1f) — 휴리스틱 폴백", residual)

        # ── 6. Camera tilt estimation ────────────────────────────────
        pitch_deg = math.degrees(math.atan2(h / 2 - vp_y, fy))
        pitch_deg = max(3.0, min(50.0, pitch_deg))
        vfov_half = math.degrees(math.atan2(h / 2, fy))

        # ── 7. Road boundaries from fitted model at frame bottom ─────
        road_left  = float(a_l * h + b_l)
        road_right = float(a_r * h + b_r)
        road_px_w  = road_right - road_left
        if road_px_w < w * 0.08:
            return False, bearing_deg, None

        # ── 8. Camera height → near_m / far_m ────────────────────────
        # Pinhole: road_px_w at bottom = road_width_m * fy / d_near
        d_near = road_width_m * fy / road_px_w
        beta_near = math.radians(pitch_deg + vfov_half)
        cam_h = d_near * math.tan(beta_near)
        cam_h = max(3.0, min(40.0, cam_h))

        near_m = max(3.0, min(30.0, d_near))

        far_angle = pitch_deg - vfov_half
        if far_angle > 1.0:
            far_m = cam_h / math.tan(math.radians(far_angle))
            far_m = min(300.0, far_m)
        else:
            far_m = near_m + road_width_m * 8.0
        far_m = max(near_m + 10.0, far_m)

        # ── 9. Trapezoid src_pts using fitted edge model ─────────────
        # far corner x positions come directly from the fitted lines at far_y
        # (avoids the vp_y-dependent linear interpolation that magnifies VP estimation error).
        half_w = road_width_m / 2.0
        far_y  = max(roi_top, int(vp_y + (h - vp_y) * 0.08))

        far_left  = float(a_l * far_y + b_l)
        far_right = float(a_r * far_y + b_r)
        far_cx    = (far_left + far_right) / 2.0
        far_half  = max((far_right - far_left) / 2.0, 2.0)

        src_pts = np.float32([
            [far_cx - far_half, far_y],   # TL far-left
            [far_cx + far_half, far_y],   # TR far-right
            [road_right,        h      ],  # BR near-right
            [road_left,         h      ],  # BL near-left
        ])

        # ── 10. GPS corners ──────────────────────────────────────────
        R_lat = 110574.0
        R_lon = 111320.0 * math.cos(math.radians(self._gps_center_lat))
        b = math.radians(bearing_deg)
        sin_b, cos_b = math.sin(b), math.cos(b)

        gps_pts: list[list[float]] = []
        for lateral, along in [(-half_w, far_m), (half_w, far_m),
                                 (half_w, near_m), (-half_w, near_m)]:
            dlat = (along * cos_b - lateral * sin_b) / R_lat
            dlon = (along * sin_b + lateral * cos_b) / R_lon
            gps_pts.append([self._gps_center_lat + dlat, self._gps_center_lon + dlon])

        dst_gps = np.float32(gps_pts)
        if not self._apply_homography_corners(src_pts, dst_gps, bearing_deg):
            return False, bearing_deg, None

        road_length_m = far_m - near_m
        logger.info(
            "차선 감지 자동 캘리브레이션: pitch=%.1f° cam_h=%.1fm near=%.1fm far=%.1fm "
            "road_len=%.1fm half_w=%.1fm bearing=%.1f°",
            pitch_deg, cam_h, near_m, far_m, road_length_m, half_w, bearing_deg,
        )
        calib_info = {
            "method":        "heuristic",
            "cam_h_m":       round(cam_h, 1),
            "near_m":        round(near_m, 1),
            "far_m":         round(far_m, 1),
            "road_width_m":  round(road_width_m, 1),
            "pitch_deg":     round(pitch_deg, 1),
            "road_length_m": round(road_length_m, 1),
            "direction_source": direction_source,
            "image_curve_sign": image_curve_sign,
            "image_curve_px": round(image_curve_px, 1),
            "map_ft_sign": curve_info.get("map_ft_sign"),
            "map_tf_sign": curve_info.get("map_tf_sign"),
            "map_ft_curve_m": round(curve_info.get("map_ft_curve_m", 0.0), 1),
            "map_tf_curve_m": round(curve_info.get("map_tf_curve_m", 0.0), 1),
        }
        return True, bearing_deg, calib_info

    def update_gps_center(self, center_lat: float, center_lon: float, bearing_deg: float = 0.0) -> None:
        """카메라 GPS + bearing으로 근사 GPS 그리드 재계산.

        보정 데이터 없을 때 fallback으로 사용.
        bearing_deg 방향으로 그리드를 회전시켜 차량이 카메라 시야 방향에 맞게 배치됨.
          near=15m ~ far=80m (along bearing), width=±25m (lateral)
        """
        # Must update GPS center so that auto_calibrate_from_frame later uses correct origin.
        self._gps_center_lat = center_lat
        self._gps_center_lon = center_lon
        R_lat = 110574.0
        R_lon = 111320.0 * math.cos(math.radians(center_lat))
        b = math.radians(bearing_deg)
        sin_b, cos_b = math.sin(b), math.cos(b)

        near_m, far_m, half_w = 15.0, 80.0, 25.0

        # PIXEL_POINTS 순서: TL, TR, BR, BL
        # top of frame = far (카메라에서 먼 쪽), bearing 방향
        offsets = [
            (-half_w, far_m),   # TL: left-far
            ( half_w, far_m),   # TR: right-far
            ( half_w, near_m),  # BR: right-near
            (-half_w, near_m),  # BL: left-near
        ]
        new_gps = []
        for lateral, along in offsets:
            dlat = (along * cos_b - lateral * sin_b) / R_lat
            dlon = (along * sin_b + lateral * cos_b) / R_lon
            new_gps.append([center_lat + dlat, center_lon + dlon])

        new_gps_arr = np.float32(new_gps)

        # 사각형 PIXEL_POINTS → 사다리꼴로 교체: 원근 투영 보정 활성화
        # 사각형-대-사각형 Homography는 선형 스케일로 퇴화하여 근거리 과속/원거리 저속 오차 발생.
        # 사다리꼴(상단 좁음/하단 넓음)을 쓰면 findHomography가 투시 변환을 계산한다.
        _pp = np.float32(PIXEL_POINTS)
        w = float(_pp[:, 0].max())
        h = float(_pp[:, 1].max())
        # 상단(원거리): 프레임 폭의 50%  /  하단(근거리): 100%
        fallback_src = np.float32([
            [w * 0.25, 0.0],  # TL far-left
            [w * 0.75, 0.0],  # TR far-right
            [w,        h  ],  # BR near-right
            [0.0,      h  ],  # BL near-left
        ])
        self._H_gps, _ = cv2.findHomography(fallback_src, new_gps_arr)
        dst_meter = self._gps_pts_to_local_meters(new_gps_arr)
        H_m, _ = cv2.findHomography(fallback_src, dst_meter)
        if H_m is not None:
            self._H_meter = H_m
        self._bearing_rad = 0.0  # 회전을 그리드에 반영했으므로 pixel_to_gps 내 추가 회전 불필요
        self._gps_center_lat = center_lat
        self._gps_center_lon = center_lon
        self._is_calibrated = False

        # snap의 H_meter 좌표 갱신 (곡선 GPS 매핑용)
        # H_meter는 ENU(동/북 미터)를 TL GPS 코너 기준으로 계산.
        # snap = center이므로 TL에서 snap까지의 ENU를 _gps_pts_to_local_meters와 동일 공식으로 계산.
        _R = 6_371_000.0
        _tl_lat, _tl_lon = new_gps[0]
        _lat0_r = math.radians(_tl_lat)
        self._snap_meter_x = _R * math.radians(center_lon - _tl_lon) * math.cos(_lat0_r)
        self._snap_meter_y = _R * math.radians(center_lat - _tl_lat)
        self._curve_bearing_rad = b
        self._refresh_curve_direction_sign()
        logger.info("GPS 근사 캘리브레이션: 중심 (%.4f, %.4f) bearing=%.1f°", center_lat, center_lon, bearing_deg)

    def batch_pixel_to_meter(
        self, points: list[tuple[float, float]]
    ) -> list[tuple[float, float]]:
        # scale model correction은 절대 좌표에 적용하지 않음 — 프레임 간 depth 변화로
        # corr 계수가 달라져 인위적 위치 점프(teleport_reset/outlier_skip → speed=0) 유발.
        return self._batch_transform(self._H_meter, points)
