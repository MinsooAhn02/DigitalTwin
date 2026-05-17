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

        logger.info(
            "캘리브레이션 적용 완료: pixel=%s gps=%s",
            pixel_pts, gps_pts,
        )

    def update_gps_center(self, center_lat: float, center_lon: float) -> None:
        """카메라 GPS 중심점으로 GPS 단응행렬 재계산 (근사 캘리브레이션).
        카메라 뷰가 약 ±0.0006° lat × ±0.0004° lon(약 130m × 70m) 범위 커버 가정.
        """
        dlat, dlon = 0.0006, 0.0004
        new_gps = np.float32([
            [center_lat + dlat, center_lon - dlon],
            [center_lat + dlat, center_lon + dlon],
            [center_lat - dlat, center_lon + dlon],
            [center_lat - dlat, center_lon - dlon],
        ])
        src = np.float32(PIXEL_POINTS)
        self._H_gps, _ = cv2.findHomography(src, new_gps)
        self._bearing_rad = math.radians(CAMERA_BEARING_DEG)
        self._gps_center_lat = float(np.mean(new_gps[:, 0]))
        self._gps_center_lon = float(np.mean(new_gps[:, 1]))
        logger.info("GPS 캘리브레이션 갱신: 중심 (%.4f, %.4f)", center_lat, center_lon)

    def batch_pixel_to_meter(
        self, points: list[tuple[float, float]]
    ) -> list[tuple[float, float]]:
        return self._batch_transform(self._H_meter, points)
