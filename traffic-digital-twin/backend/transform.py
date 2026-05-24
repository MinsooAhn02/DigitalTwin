"""
transform.py — Perspective Transform (픽셀 → 실세계 좌표)
  · OpenCV getPerspectiveTransform으로 단응행렬(Homography) 계산
  · pixel_to_gps()  : 픽셀 (u, v) → (lat, lon)
  · pixel_to_meter(): 픽셀 (u, v) → (x_m, y_m)  속도 계산용
  · update_from_calibration(): 사용자 4-point 캘리브레이션으로 행렬 재계산
"""

import json
import logging
import math
from pathlib import Path

import numpy as np
import cv2
from config import PIXEL_POINTS, GPS_POINTS, REAL_WORLD_WIDTH_M, REAL_WORLD_HEIGHT_M, CAMERA_BEARING_DEG

CALIBRATION_PATH = Path(__file__).parent / "calibration_data.json"

logger = logging.getLogger(__name__)


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

    def pixel_to_gps(self, u: float, v: float) -> tuple[float, float]:
        """픽셀 (u, v) → (latitude, longitude), 카메라 베어링 보정 포함."""
        lat, lon = self._transform_point(self._H_gps, u, v)

        if self._bearing_rad == 0.0:
            return lat, lon

        # GPS 중심 기준 델타를 bearing 각도로 회전
        dlat = lat - self._gps_center_lat
        dlon = lon - self._gps_center_lon
        cos_b = math.cos(self._bearing_rad)
        sin_b = math.sin(self._bearing_rad)
        new_dlat = cos_b * dlat - sin_b * dlon
        new_dlon = sin_b * dlat + cos_b * dlon
        return self._gps_center_lat + new_dlat, self._gps_center_lon + new_dlon

    def pixel_to_meter(self, u: float, v: float) -> tuple[float, float]:
        """픽셀 (u, v) → (x_m, y_m) — 속도 계산용 실세계 미터 좌표"""
        return self._transform_point(self._H_meter, u, v)

    def batch_pixel_to_gps(
        self, points: list[tuple[float, float]]
    ) -> list[tuple[float, float]]:
        """여러 픽셀 좌표를 한 번에 변환 (OpenCV 벡터 연산 활용)"""
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

    def update_gps_center(self, center_lat: float, center_lon: float, bearing_deg: float = 0.0) -> None:
        """카메라 GPS + bearing으로 근사 GPS 그리드 재계산.

        보정 데이터 없을 때 fallback으로 사용.
        bearing_deg 방향으로 그리드를 회전시켜 차량이 카메라 시야 방향에 맞게 배치됨.
          near=15m ~ far=80m (along bearing), width=±25m (lateral)
        """
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
        logger.info("GPS 근사 캘리브레이션: 중심 (%.4f, %.4f) bearing=%.1f°", center_lat, center_lon, bearing_deg)

    def batch_pixel_to_meter(
        self, points: list[tuple[float, float]]
    ) -> list[tuple[float, float]]:
        return self._batch_transform(self._H_meter, points)
