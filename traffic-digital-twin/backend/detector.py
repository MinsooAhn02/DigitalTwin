"""
Video source handling plus YOLO/BoT-SORT inference.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import importlib.util
import logging
from pathlib import Path
import shutil
import threading

import cv2
import numpy as np
import supervision as sv
import torch
from ultralytics import YOLO

from config import (
    YOLO_AUTO_EXPORT_ENGINE,
    YOLO_CONF,
    YOLO_IMGSZ,
    YOLO_IOU,
    YOLO_MODEL,
    YOLO_MODEL_VARIANT,
    VEHICLE_CLASSES,
)

logger = logging.getLogger(__name__)

BACKEND_DIR = Path(__file__).resolve().parent
VARIANT_PRIORITY = ("x", "l", "m", "s", "n")


@dataclass(frozen=True)
class ModelSelection:
    path: Path
    backend: str
    source: str


def _normalize_variant(value: str) -> str:
    value = (value or "").strip().lower()
    if value.startswith("yolov8") and len(value) >= 7:
        value = value[6]

    aliases = {
        "nano": "n",
        "small": "s",
        "medium": "m",
        "large": "l",
        "xlarge": "x",
    }
    return aliases.get(value, value[:1] if value else "")


def _can_use_tensorrt() -> bool:
    return torch.cuda.is_available() and importlib.util.find_spec("tensorrt") is not None


def _resolve_requested_model() -> Path | None:
    if not YOLO_MODEL:
        return None

    requested = Path(YOLO_MODEL)
    if not requested.is_absolute():
        requested = BACKEND_DIR / requested
    return requested


def _candidate_stems() -> list[Path]:
    requested = _resolve_requested_model()
    if requested is not None:
        return [requested.with_suffix("")] if requested.suffix else [requested]

    preferred = _normalize_variant(YOLO_MODEL_VARIANT)
    variants = [preferred] if preferred else []
    variants.extend(variant for variant in VARIANT_PRIORITY if variant not in variants)
    return [BACKEND_DIR / f"yolov8{variant}" for variant in variants]


def _move_engine_to_backend(exported_path: Path, engine_path: Path) -> Path:
    if exported_path.resolve() == engine_path.resolve():
        return engine_path
    if engine_path.exists():
        return engine_path
    shutil.move(str(exported_path), str(engine_path))
    return engine_path


def _export_engine(weights_path: Path) -> Path | None:
    if not YOLO_AUTO_EXPORT_ENGINE or not _can_use_tensorrt():
        return None

    engine_path = weights_path.with_suffix(".engine")
    if engine_path.exists():
        return engine_path

    logger.info("TensorRT engine export: %s -> %s", weights_path.name, engine_path.name)
    try:
        export_model = YOLO(str(weights_path), task="detect")
        exported = export_model.export(
            format="engine",
            half=True,
            device=0,
            imgsz=YOLO_IMGSZ,
            verbose=False,
        )
        if not exported:
            return engine_path if engine_path.exists() else None

        exported_path = Path(str(exported))
        if not exported_path.is_absolute():
            exported_path = (Path.cwd() / exported_path).resolve()

        if not exported_path.exists():
            return engine_path if engine_path.exists() else None

        return _move_engine_to_backend(exported_path, engine_path)
    except Exception as exc:
        logger.warning("TensorRT engine export failed for %s: %s", weights_path.name, exc)
        return None


def resolve_model_selection() -> ModelSelection:
    trt_available = _can_use_tensorrt()
    pt_fallback: Path | None = None

    for stem in _candidate_stems():
        engine_path = stem.with_suffix(".engine")
        pt_path = stem.with_suffix(".pt")

        if trt_available and engine_path.exists():
            return ModelSelection(engine_path, "tensorrt", "existing-engine")

        if trt_available and pt_path.exists():
            exported_engine = _export_engine(pt_path)
            if exported_engine is not None and exported_engine.exists():
                return ModelSelection(exported_engine, "tensorrt", "exported-engine")
            if pt_fallback is None:
                pt_fallback = pt_path
            continue

        if pt_path.exists() and pt_fallback is None:
            pt_fallback = pt_path

    if pt_fallback is not None:
        return ModelSelection(pt_fallback, "pytorch", "weights")

    available = ", ".join(
        sorted(path.name for path in BACKEND_DIR.glob("yolov8*") if path.is_file())
    ) or "none"
    raise FileNotFoundError(
        "No YOLO model was found in backend. "
        f"Current candidates: {available}"
    )


def open_video_source(url: str) -> cv2.VideoCapture:
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    if cap.isOpened():
        logger.info("Stream connected: %s", url)
        return cap
    raise RuntimeError(f"Failed to open stream: {url}")


class VehicleDetector:
    CLASS_IDS = list(VEHICLE_CLASSES.keys())

    def __init__(self):
        self._device = "cuda" if torch.cuda.is_available() else "cpu"
        selection = resolve_model_selection()
        self._model_path = str(selection.path)
        self._backend = selection.backend
        self._inference_device = 0 if selection.backend == "tensorrt" else self._device
        self.model = YOLO(self._model_path, task="detect")
        if selection.backend != "tensorrt":
            self.model.to(self._device)

        # Serialize access because live_loop and ws/detect can share one model.
        self._lock = threading.Lock()
        logger.info(
            "YOLO model ready: %s backend=%s source=%s device=%s",
            selection.path.name,
            selection.backend,
            selection.source,
            self._inference_device,
        )

    def detect(self, frame: np.ndarray) -> sv.Detections:
        with self._lock:
            results = self.model(
                frame,
                imgsz=YOLO_IMGSZ,
                conf=YOLO_CONF,
                iou=YOLO_IOU,
                classes=self.CLASS_IDS,
                device=self._inference_device,
                verbose=False,
            )[0]
        return sv.Detections.from_ultralytics(results)

    def track(self, frame: np.ndarray) -> sv.Detections:
        with self._lock:
            results = self.model.track(
                frame,
                imgsz=YOLO_IMGSZ,
                conf=YOLO_CONF,
                iou=YOLO_IOU,
                classes=self.CLASS_IDS,
                device=self._inference_device,
                tracker="botsort.yaml",
                persist=True,
                verbose=False,
            )[0]
        return sv.Detections.from_ultralytics(results)

    def reset_tracker(self) -> None:
        try:
            if (
                hasattr(self.model, "predictor")
                and self.model.predictor is not None
                and hasattr(self.model.predictor, "trackers")
                and self.model.predictor.trackers
            ):
                self.model.predictor.trackers[0].reset()
                logger.info("BoT-SORT tracker reset")
        except Exception as exc:
            logger.warning("Tracker reset failed: %s", exc)


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
        if self._cap:
            self._cap.release()
        self._url = url
        self._cap = open_video_source(url)
        self._frame_id = 0
        logger.info("Camera switched: %s", url)

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
        logger.info("Reconnecting stream: %s", self._url)
        if self._cap:
            self._cap.release()
        await asyncio.sleep(self.RECONNECT_DELAY)
        try:
            self._cap = open_video_source(self._url)
        except RuntimeError as exc:
            logger.warning("Reconnect failed: %s", exc)
            self._cap = None

    def release(self) -> None:
        if self._cap:
            self._cap.release()
