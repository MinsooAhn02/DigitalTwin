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
from pathlib import Path

import numpy as np
import cv2
from config import PIXEL_POINTS, GPS_POINTS, REAL_WORLD_WIDTH_M, REAL_WORLD_HEIGHT_M, CAMERA_BEARING_DEG

CALIBRATION_PATH = Path(__file__).parent / "calibration_data.json"

logger = logging.getLogger(__name__)


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

    def auto_calibrate_from_frame(
        self,
        frame: np.ndarray,
        bearing_deg: float = 0.0,
        road_width_m: float = 7.0,
    ) -> bool:
        """차선 감지(Hough)로 원근 파라미터 자동 추정.

        road_width_m: 노드링크 lanes × 차선폭(m) — 수평 스케일 기준값.
        반환: True = 성공(homography 갱신), False = 실패(기존 상태 유지).
        """
        h, w = frame.shape[:2]
        fy = h * 1.2  # 대략적 초점거리 (픽셀) — 66° 수직 화각 기준

        # ── 1. Edge detection ────────────────────────────────────────
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
        blur = cv2.GaussianBlur(gray, (5, 5), 0)
        edges = cv2.Canny(blur, 50, 150)

        # ROI: 하단 55% (하늘/원거리 영역 제외)
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
            return False

        # 차선 방향 직선만 필터: 수직에서 60° 이내 (사선 차선마킹)
        diag: list[tuple] = []
        for l in lines:
            x1, y1, x2, y2 = l[0]
            if abs(y2 - y1) < 5:
                continue
            from_vert = math.degrees(math.atan2(abs(x2 - x1), abs(y2 - y1)))
            if from_vert < 60:
                diag.append((x1, y1, x2, y2))

        if len(diag) < 2:
            return False

        # ── 3. 소실점 추정 ───────────────────────────────────────────
        vp_cands: list[tuple[float, float]] = []
        for i in range(len(diag)):
            for j in range(i + 1, min(i + 8, len(diag))):
                pt = _line_intersection(diag[i], diag[j])
                if pt is not None:
                    px, py = pt
                    if -w * 0.5 < px < w * 1.5 and -h * 0.2 < py < h * 0.6:
                        vp_cands.append((px, py))

        vp_y = float(np.median([p[1] for p in vp_cands])) if len(vp_cands) >= 2 else 0.0
        vp_y = min(vp_y, h * 0.55)

        # ── 4. 카메라 틸트 각도 추정 ─────────────────────────────────
        pitch_deg = math.degrees(math.atan2(h / 2 - vp_y, fy))
        pitch_deg = max(3.0, min(50.0, pitch_deg))
        vfov_half = math.degrees(math.atan2(h / 2, fy))  # ≈22.6° for fy=1.2h

        # ── 5. 하단 도로 경계 검출 ───────────────────────────────────
        xs_bot: list[float] = []
        for x1, y1, x2, y2 in diag:
            if y2 == y1:
                continue
            xb = x1 + (h - y1) * (x2 - x1) / (y2 - y1)
            if -w * 0.1 <= xb <= w * 1.1:
                xs_bot.append(xb)

        if len(xs_bot) < 2:
            return False

        road_left  = float(np.percentile(xs_bot, 10))
        road_right = float(np.percentile(xs_bot, 90))
        road_px_w  = road_right - road_left
        if road_px_w < w * 0.08:
            return False

        # ── 6. 카메라 높이 역산 → near_m / far_m ─────────────────────
        # Pinhole 공식: road_width_m = road_px_w × d_near / fy
        # → d_near(하단 도로면까지 거리) = road_width_m × fy / road_px_w
        # → cam_h(설치 높이) = d_near × tan(pitch + vfov_half)
        d_near = road_width_m * fy / road_px_w
        beta_near = math.radians(pitch_deg + vfov_half)
        cam_h = d_near * math.tan(beta_near)
        cam_h = max(3.0, min(40.0, cam_h))   # 현실 범위 클램프 (3-40m)

        near_m = d_near  # 하단 = 최근거리
        near_m = max(3.0, min(30.0, near_m))

        far_angle = pitch_deg - vfov_half
        if far_angle > 1.0:  # 지평선 위 → 유한한 원거리
            far_m = cam_h / math.tan(math.radians(far_angle))
            far_m = min(300.0, far_m)
        else:                # 지평선 근처 → 사실상 무한 (도로가 수평에 가깝게 보임)
            far_m = near_m + road_width_m * 8.0
        far_m = max(near_m + 10.0, far_m)

        # ── 7. 원근 사다리꼴 픽셀 좌표 ──────────────────────────────
        half_w  = road_width_m / 2.0
        road_cx = (road_left + road_right) / 2.0

        if vp_y < h:
            far_y       = max(roi_top, int(vp_y + (h - vp_y) * 0.08))
            far_px_half = (road_px_w / 2) * max(0.04, (far_y - vp_y) / max(1.0, h - vp_y))
        else:
            far_y       = roi_top
            far_px_half = road_px_w * 0.12

        src_pts = np.float32([
            [road_cx - far_px_half, far_y],   # TL far-left
            [road_cx + far_px_half, far_y],   # TR far-right
            [road_right,            h      ],  # BR near-right
            [road_left,             h      ],  # BL near-left
        ])

        # ── 8. GPS 코너 계산 ─────────────────────────────────────────
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
        H_gps, _ = cv2.findHomography(src_pts, dst_gps)
        if H_gps is None:
            return False

        self._H_gps = H_gps
        dst_meter = self._gps_pts_to_local_meters(dst_gps)
        H_m, _ = cv2.findHomography(src_pts, dst_meter)
        if H_m is not None:
            self._H_meter = H_m
        self._bearing_rad = 0.0
        self._is_calibrated = False  # 자동 추정 = 수동 캘리브레이션 아님

        logger.info(
            "차선 감지 자동 캘리브레이션: pitch=%.1f° cam_h=%.1fm near=%.1fm far=%.1fm half_w=%.1fm",
            pitch_deg, cam_h, near_m, far_m, half_w,
        )
        return True

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
