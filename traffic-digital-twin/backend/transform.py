"""
transform.py — Perspective Transform (픽셀 → 실세계 좌표)
  · OpenCV getPerspectiveTransform으로 단응행렬(Homography) 계산
  · pixel_to_gps()  : 픽셀 (u, v) → (lat, lon)
  · pixel_to_meter(): 픽셀 (u, v) → (x_m, y_m)  속도 계산용
"""

import logging

import numpy as np
import cv2
from config import PIXEL_POINTS, GPS_POINTS, REAL_WORLD_WIDTH_M, REAL_WORLD_HEIGHT_M

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

    # ──────────────────────────────────────────────────────────────────
    def pixel_to_gps(self, u: float, v: float) -> tuple[float, float]:
        """픽셀 (u, v) → (latitude, longitude)"""
        pt = np.float32([[[u, v]]])
        result = cv2.perspectiveTransform(pt, self._H_gps)  # (1,1,2)
        lat, lon = float(result[0, 0, 0]), float(result[0, 0, 1])
        return lat, lon

    def pixel_to_meter(self, u: float, v: float) -> tuple[float, float]:
        """픽셀 (u, v) → (x_m, y_m) — 속도 계산용 실세계 미터 좌표"""
        pt = np.float32([[[u, v]]])
        result = cv2.perspectiveTransform(pt, self._H_meter)
        x_m, y_m = float(result[0, 0, 0]), float(result[0, 0, 1])
        return x_m, y_m

    def batch_pixel_to_gps(
        self, points: list[tuple[float, float]]
    ) -> list[tuple[float, float]]:
        """여러 픽셀 좌표를 한 번에 변환 (OpenCV 벡터 연산 활용)"""
        if not points:
            return []
        arr = np.float32([[p] for p in points])          # (N, 1, 2)
        res = cv2.perspectiveTransform(arr, self._H_gps) # (N, 1, 2)
        return [(float(r[0, 0]), float(r[0, 1])) for r in res]

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
        logger.info("GPS 캘리브레이션 갱신: 중심 (%.4f, %.4f)", center_lat, center_lon)

    def batch_pixel_to_meter(
        self, points: list[tuple[float, float]]
    ) -> list[tuple[float, float]]:
        if not points:
            return []
        arr = np.float32([[p] for p in points])
        res = cv2.perspectiveTransform(arr, self._H_meter)
        return [(float(r[0, 0]), float(r[0, 1])) for r in res]
