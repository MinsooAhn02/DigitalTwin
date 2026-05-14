"""
detector.py — 영상 소스 관리 + YOLOv8x 탐지 / BoT-SORT 추적
  · detect() : 단순 YOLO 추론 (tracker_id 없음)
  · track()  : BoT-SORT appearance ReID 추적 (tracker_id 포함, persist=True)
  · reset_tracker() : 카메라 전환 시 BoT-SORT 내부 상태 초기화
"""

from __future__ import annotations
import asyncio
import logging
import threading

import cv2
import numpy as np
from ultralytics import YOLO
import supervision as sv
import torch

from config import (
    YOLO_MODEL,
    YOLO_IMGSZ,
    YOLO_CONF,
    YOLO_IOU,
    VEHICLE_CLASSES,
)

logger = logging.getLogger(__name__)


def open_video_source(url: str) -> cv2.VideoCapture:
    """HLS / RTSP URL을 FFmpeg 백엔드로 연다."""
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    if cap.isOpened():
        logger.info("스트림 연결 성공: %s", url)
        return cap
    raise RuntimeError(f"스트림 열기 실패: {url}")


class VehicleDetector:
    CLASS_IDS = list(VEHICLE_CLASSES.keys())

    def __init__(self):
        self._device = "cuda" if torch.cuda.is_available() else "cpu"
        is_engine = str(YOLO_MODEL).endswith(".engine")
        self.model = YOLO(YOLO_MODEL, task="detect")
        if not is_engine:
            self.model.to(self._device)
        # live_loop + ws/detect 가 동시에 model 을 쓰지 않도록 직렬화
        self._lock = threading.Lock()
        logger.info("YOLO 모델 로드 완료: %s  device=%s", YOLO_MODEL, self._device)

    def detect(self, frame: np.ndarray) -> sv.Detections:
        """단순 추론 — tracker_id 없음."""
        with self._lock:
            results = self.model(
                frame,
                imgsz=YOLO_IMGSZ,
                conf=YOLO_CONF,
                iou=YOLO_IOU,
                classes=self.CLASS_IDS,
                device=self._device,
                verbose=False,
            )[0]
        return sv.Detections.from_ultralytics(results)

    def track(self, frame: np.ndarray) -> sv.Detections:
        """
        BoT-SORT appearance ReID 추적.
        persist=True → 프레임 간 tracker 상태 유지, ID 끊김 대폭 감소.
        """
        with self._lock:
            results = self.model.track(
                frame,
                imgsz=YOLO_IMGSZ,
                conf=YOLO_CONF,
                iou=YOLO_IOU,
                classes=self.CLASS_IDS,
                device=self._device,
                tracker="botsort.yaml",
                persist=True,
                verbose=False,
            )[0]
        return sv.Detections.from_ultralytics(results)

    def reset_tracker(self) -> None:
        """카메라 전환 시 BoT-SORT 내부 상태 초기화."""
        try:
            if (
                hasattr(self.model, "predictor")
                and self.model.predictor is not None
                and hasattr(self.model.predictor, "trackers")
                and self.model.predictor.trackers
            ):
                self.model.predictor.trackers[0].reset()
                logger.info("BoT-SORT tracker 리셋 완료")
        except Exception as e:
            logger.warning("tracker reset 실패: %s", e)


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

    @property
    def url(self) -> str | None:
        return self._url

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
