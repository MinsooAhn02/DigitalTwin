"""
detector.py — 영상 소스 관리 + YOLOv8x 탐지
  1. ITS OpenAPI로 RTSP URL 취득
  2. OpenCV로 스트림 열기 (실패 시 로컬 폴백)
  3. YOLOv8x로 프레임 추론 → Supervision Detections 반환
"""

from __future__ import annotations
import asyncio
import logging
from pathlib import Path

import cv2
import httpx
import numpy as np
from ultralytics import YOLO
import supervision as sv

from config import (
    ITS_API_KEY,
    ITS_BASE_URL,
    ITS_CCTV_IDS,
    FALLBACK_VIDEO_PATH,
    YOLO_MODEL,
    YOLO_CONF,
    YOLO_IOU,
    VEHICLE_CLASSES,
)

logger = logging.getLogger(__name__)


# ── ITS API RTSP URL 취득 ──────────────────────────────────────────────
async def fetch_rtsp_url(cctv_id: str) -> str | None:
    """ITS OpenAPI 호출 → RTSP URL 반환. 실패 시 None."""
    params = {
        "apiKey": ITS_API_KEY,
        "type":   "its",
        "cctvType": "1",       # 1: 실시간, 2: 정지영상
        "minX": "126.0", "maxX": "129.0",
        "minY": "35.0",  "maxY": "38.0",
        "getType": "json",
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(ITS_BASE_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
            items = data.get("response", {}).get("data", [])
            for item in items:
                if item.get("cctvid") == cctv_id:
                    return item.get("cctvurl")   # RTSP URL
    except Exception as e:
        logger.warning("ITS API 호출 실패 [%s]: %s", cctv_id, e)
    return None


# ── 비디오 소스 열기 (Fallback 포함) ─────────────────────────────────
def open_video_source(rtsp_url: str | None) -> cv2.VideoCapture:
    """
    RTSP URL을 우선 시도하고, 실패하면 로컬 폴백 영상을 연다.
    """
    if rtsp_url:
        cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
        if cap.isOpened():
            logger.info("RTSP 스트림 연결 성공: %s", rtsp_url)
            return cap
        logger.warning("RTSP 연결 실패, 폴백으로 전환")

    fallback = str(Path(FALLBACK_VIDEO_PATH))
    cap = cv2.VideoCapture(fallback)
    if not cap.isOpened():
        raise RuntimeError(f"폴백 영상도 열 수 없음: {fallback}")
    logger.info("로컬 폴백 영상 사용: %s", fallback)
    return cap


# ── 탐지기 클래스 ─────────────────────────────────────────────────────
class VehicleDetector:
    """
    YOLOv8x 모델을 로드하고, 프레임마다 vehicle 탐지 결과를 반환한다.
    """

    CLASS_IDS = list(VEHICLE_CLASSES.keys())   # [2, 3, 5, 7]

    def __init__(self):
        self.model = YOLO(YOLO_MODEL)
        logger.info("YOLOv8x 모델 로드 완료")

    def detect(self, frame: np.ndarray) -> sv.Detections:
        """
        단일 프레임 추론.
        반환: supervision Detections (필터링된 vehicle 클래스만)
        """
        results = self.model(
            frame,
            conf=YOLO_CONF,
            iou=YOLO_IOU,
            classes=self.CLASS_IDS,
            verbose=False,
        )[0]

        detections = sv.Detections.from_ultralytics(results)
        return detections   # class_id, xyxy, confidence 포함


# ── 스트림 제너레이터 ─────────────────────────────────────────────────
class VideoStream:
    """
    비디오 캡처를 래핑해 auto-reconnect 로직을 제공한다.
    read_frame()이 None을 반환하면 호출 측에서 재연결을 시도한다.
    """

    RECONNECT_DELAY = 3.0   # 초

    def __init__(self, rtsp_url: str | None = None):
        self._rtsp_url = rtsp_url
        self._cap = open_video_source(rtsp_url)
        self._frame_id = 0

    @property
    def fps(self) -> float:
        return self._cap.get(cv2.CAP_PROP_FPS) or 30.0

    def read_frame(self) -> tuple[int, np.ndarray] | tuple[None, None]:
        """(frame_id, frame) 또는 (None, None) 반환."""
        ok, frame = self._cap.read()
        if not ok:
            return None, None
        self._frame_id += 1
        return self._frame_id, frame

    async def reconnect(self) -> None:
        """스트림 재연결 (RTSP → 폴백 순서)."""
        logger.info("스트림 재연결 시도…")
        self._cap.release()
        await asyncio.sleep(self.RECONNECT_DELAY)
        try:
            rtsp_url = await fetch_rtsp_url(ITS_CCTV_IDS[0]) if ITS_API_KEY != "YOUR_API_KEY_HERE" else None
        except Exception:
            rtsp_url = None
        self._cap = open_video_source(rtsp_url)

    def release(self) -> None:
        self._cap.release()
