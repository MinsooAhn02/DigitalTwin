"""
detector.py — 영상 소스 관리 + YOLOv8x 탐지
  1. HLS URL로 OpenCV(FFmpeg 백엔드) 스트림 열기
  2. switch_to()로 카메라 전환
  3. YOLOv8x로 프레임 추론 → Supervision Detections 반환
"""

from __future__ import annotations
import asyncio
import logging

import cv2
import numpy as np
from ultralytics import YOLO
import supervision as sv

from config import (
    YOLO_MODEL,
    YOLO_CONF,
    YOLO_IOU,
    VEHICLE_CLASSES,
)

logger = logging.getLogger(__name__)


# ── 비디오 소스 열기 ──────────────────────────────────────────────────
def open_video_source(url: str) -> cv2.VideoCapture:
    """HLS / RTSP URL을 FFmpeg 백엔드로 연다."""
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    if cap.isOpened():
        logger.info("스트림 연결 성공: %s", url)
        return cap
    raise RuntimeError(f"스트림 열기 실패: {url}")


# ── 탐지기 클래스 ─────────────────────────────────────────────────────
class VehicleDetector:
    CLASS_IDS = list(VEHICLE_CLASSES.keys())

    def __init__(self):
        self.model = YOLO(YOLO_MODEL)
        logger.info("YOLOv8x 모델 로드 완료")

    def detect(self, frame: np.ndarray) -> sv.Detections:
        results = self.model(
            frame,
            conf=YOLO_CONF,
            iou=YOLO_IOU,
            classes=self.CLASS_IDS,
            verbose=False,
        )[0]
        return sv.Detections.from_ultralytics(results)


# ── 스트림 클래스 ─────────────────────────────────────────────────────
class VideoStream:
    RECONNECT_DELAY = 3.0

    def __init__(self):
        self._url: str | None = None
        self._cap: cv2.VideoCapture | None = None
        self._frame_id = 0

    @property
    def is_open(self) -> bool:
        return self._cap is not None and self._cap.isOpened()

    @property
    def fps(self) -> float:
        if self._cap:
            return self._cap.get(cv2.CAP_PROP_FPS) or 30.0
        return 30.0

    def switch_to(self, url: str) -> None:
        """새 HLS/RTSP URL로 카메라 전환."""
        if self._cap:
            self._cap.release()
        self._url = url
        self._cap = open_video_source(url)
        self._frame_id = 0
        logger.info("카메라 전환 완료: %s", url)

    def read_frame(self) -> tuple[int, np.ndarray] | tuple[None, None]:
        if self._cap is None:
            return None, None
        ok, frame = self._cap.read()
        if not ok:
            return None, None
        self._frame_id += 1
        return self._frame_id, frame

    async def reconnect(self) -> None:
        if not self._url:
            await asyncio.sleep(1.0)
            return
        logger.info("스트림 재연결 시도: %s", self._url)
        if self._cap:
            self._cap.release()
        await asyncio.sleep(self.RECONNECT_DELAY)
        try:
            self._cap = open_video_source(self._url)
        except RuntimeError as e:
            logger.warning("재연결 실패: %s", e)
            self._cap = None

    def release(self) -> None:
        if self._cap:
            self._cap.release()
