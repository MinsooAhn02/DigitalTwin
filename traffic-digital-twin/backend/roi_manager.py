"""
roi_manager.py — 카메라별 ROI polygon 로드/저장/자동추정

ROI 좌표는 정규화 좌표(0.0~1.0)로 저장.
실제 픽셀 변환: pixel = relative * frame_width (or height)
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent / "roi_config.json"


def camera_key(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:12]


def load_roi(camera_url: str) -> list[list[float]] | None:
    """저장된 ROI 정규화 좌표 반환. 없으면 None."""
    if not _CONFIG_PATH.exists():
        return None
    try:
        data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        entry = data.get(camera_key(camera_url))
        return entry.get("polygon") if entry else None
    except Exception:
        return None


def save_roi(camera_url: str, polygon: list[list[float]], auto: bool = False) -> None:
    """ROI 정규화 좌표를 JSON에 저장."""
    data: dict = {}
    if _CONFIG_PATH.exists():
        try:
            data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    key = camera_key(camera_url)
    data[key] = {
        "polygon": polygon,
        "auto_generated": auto,
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }
    _CONFIG_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info("ROI 저장: key=%s (%s)", key, "자동" if auto else "수동")


def roi_to_pixels(polygon: list[list[float]], w: int, h: int) -> np.ndarray:
    """정규화 좌표 → 픽셀 좌표 변환."""
    return np.array([[int(x * w), int(y * h)] for x, y in polygon], dtype=np.int32)


def estimate_roi(frame: np.ndarray) -> list[list[float]]:
    """
    OpenCV edge detection으로 도로 영역을 자동 추정.
    성공 시 정규화 좌표 polygon 반환, 실패 시 기본 사다리꼴 반환.
    """
    h, w = frame.shape[:2]

    try:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)
        edges = cv2.Canny(blurred, 50, 150)

        # 하단 65%만 관심 영역으로 설정 (하늘/건물 제외)
        roi_mask = np.zeros_like(edges)
        roi_mask[int(h * 0.35):, :] = 255
        edges = cv2.bitwise_and(edges, roi_mask)

        kernel = np.ones((3, 3), np.uint8)
        dilated = cv2.dilate(edges, kernel, iterations=2)

        contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if contours:
            largest = max(contours, key=cv2.contourArea)
            if cv2.contourArea(largest) > h * w * 0.05:
                hull = cv2.convexHull(largest)
                epsilon = 0.03 * cv2.arcLength(hull, True)
                approx = cv2.approxPolyDP(hull, epsilon, True)
                if len(approx) >= 3:
                    pts = approx.reshape(-1, 2)
                    polygon = [[float(x) / w, float(y) / h] for x, y in pts]
                    logger.info("ROI 자동 추정 완료: %d개 꼭짓점", len(polygon))
                    return polygon
    except Exception as exc:
        logger.warning("ROI 자동 추정 실패: %s", exc)

    return _default_trapezoid()


def _default_trapezoid() -> list[list[float]]:
    """해상도 무관 기본 사다리꼴 (정규화 좌표)."""
    logger.info("ROI 기본 사다리꼴 사용")
    return [
        [0.05, 0.95],
        [0.95, 0.95],
        [0.75, 0.35],
        [0.25, 0.35],
    ]
