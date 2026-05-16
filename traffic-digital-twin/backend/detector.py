"""
Video source handling plus YOLO detection + boxmot tracking.

Tracker tiers (TRACKER_TIER env var or config):
  cpu    → ByteTrack  (no ReID, fast, no GPU needed)
  low    → OcSort     (no ReID, better occlusion handling)
  medium → BotSort    (ReID, 6-8 GB VRAM recommended)
  high   → DeepOcSort (ReID, 8+ GB VRAM recommended)
  auto   → selected based on detected GPU VRAM

Inference backend priority:
  1. TensorRT (.engine)  — best performance on NVIDIA GPU
  2. ONNX Runtime (.onnx) — fallback when TensorRT unavailable
  3. PyTorch (.pt)        — universal fallback
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import importlib.util
import logging
from pathlib import Path
import shutil
import threading
from typing import Any

import cv2
import numpy as np
import supervision as sv
import torch
from ultralytics import YOLO

from config import (
    TRACKER_TIER,
    YOLO_AUTO_EXPORT_ENGINE,
    YOLO_CONF,
    YOLO_IMGSZ,
    YOLO_IOU,
    YOLO_MODEL,
    YOLO_MODEL_VARIANT,
    VEHICLE_CLASSES,
)
from roi_manager import roi_to_pixels

logger = logging.getLogger(__name__)

BACKEND_DIR = Path(__file__).resolve().parent
VARIANT_PRIORITY = ("x", "l", "m", "s", "n")

# ── Tracker tier config ────────────────────────────────────────────────────
_TRACKER_CONFIGS: dict[str, dict] = {
    "cpu":    {"name": "ByteTrack",  "cls": "ByteTrack",   "reid": None},
    "low":    {"name": "OcSort",     "cls": "OcSort",      "reid": None},
    "medium": {"name": "BotSort",    "cls": "BotSort",     "reid": "osnet_x0_25_msmt17.pt"},
    "high":   {"name": "DeepOcSort", "cls": "DeepOcSort",  "reid": "osnet_x1_0_msmt17.pt"},
}


# ── Model selection helpers ────────────────────────────────────────────────

@dataclass(frozen=True)
class ModelSelection:
    path: Path
    backend: str  # "tensorrt" | "onnx" | "pytorch"
    source: str


def _normalize_variant(value: str) -> str:
    value = (value or "").strip().lower()
    if value.startswith("yolov8") and len(value) >= 7:
        value = value[6]
    aliases = {"nano": "n", "small": "s", "medium": "m", "large": "l", "xlarge": "x"}
    return aliases.get(value, value[:1] if value else "")


def _can_use_tensorrt() -> bool:
    return torch.cuda.is_available() and importlib.util.find_spec("tensorrt") is not None


def _can_use_onnx() -> bool:
    return importlib.util.find_spec("onnxruntime") is not None


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
    variants.extend(v for v in VARIANT_PRIORITY if v not in variants)
    return [BACKEND_DIR / f"yolov8{v}" for v in variants]


def _move_to_backend(src: Path, dst: Path) -> Path:
    if src.resolve() == dst.resolve():
        return dst
    if dst.exists():
        return dst
    shutil.move(str(src), str(dst))
    return dst


def _export_model(weights_path: Path, fmt: str) -> Path | None:
    """YOLO 모델을 지정 포맷으로 export. 이미 존재하면 즉시 반환."""
    if fmt == "engine" and (not YOLO_AUTO_EXPORT_ENGINE or not _can_use_tensorrt()):
        return None
    out_path = weights_path.with_suffix(f".{fmt}")
    if out_path.exists():
        return out_path
    logger.info("%s export: %s → %s", fmt.upper(), weights_path.name, out_path.name)
    is_engine = fmt == "engine"
    try:
        m = YOLO(str(weights_path), task="detect")
        exported = m.export(
            format=fmt, imgsz=YOLO_IMGSZ,
            half=is_engine, device=(0 if is_engine else None), verbose=False,
        )
        if not exported:
            return out_path if out_path.exists() else None
        ep = Path(str(exported))
        if not ep.is_absolute():
            ep = (Path.cwd() / ep).resolve()
        if not ep.exists():
            return out_path if out_path.exists() else None
        return _move_to_backend(ep, out_path)
    except Exception as exc:
        logger.warning("%s export failed for %s: %s", fmt.upper(), weights_path.name, exc)
        return None


def resolve_model_selection() -> ModelSelection:
    """
    우선순위: TensorRT > ONNX Runtime > PyTorch
    각 포맷 파일이 없으면 자동 export 시도.
    """
    trt_ok = _can_use_tensorrt()
    onnx_ok = _can_use_onnx()
    pt_fallback: Path | None = None

    for stem in _candidate_stems():
        engine_path = stem.with_suffix(".engine")
        onnx_path   = stem.with_suffix(".onnx")
        pt_path     = stem.with_suffix(".pt")

        # 1. TensorRT
        if trt_ok and engine_path.exists():
            return ModelSelection(engine_path, "tensorrt", "existing-engine")

        if trt_ok and pt_path.exists():
            exported = _export_model(pt_path, "engine")
            if exported and exported.exists():
                return ModelSelection(exported, "tensorrt", "exported-engine")
            # TRT export 실패 → ONNX 시도
            if onnx_ok:
                exported_onnx = _export_model(pt_path, "onnx")
                if exported_onnx and exported_onnx.exists():
                    return ModelSelection(exported_onnx, "onnx", "exported-onnx")
            if pt_fallback is None:
                pt_fallback = pt_path
            continue

        # 2. ONNX Runtime (TRT 불가 환경)
        if onnx_ok and onnx_path.exists():
            return ModelSelection(onnx_path, "onnx", "existing-onnx")

        if onnx_ok and pt_path.exists() and not trt_ok:
            exported_onnx = _export_model(pt_path, "onnx")
            if exported_onnx and exported_onnx.exists():
                return ModelSelection(exported_onnx, "onnx", "exported-onnx")

        # 3. PyTorch fallback
        if pt_path.exists() and pt_fallback is None:
            pt_fallback = pt_path

    if pt_fallback is not None:
        return ModelSelection(pt_fallback, "pytorch", "weights")

    available = ", ".join(
        sorted(p.name for p in BACKEND_DIR.glob("yolov8*") if p.is_file())
    ) or "none"
    raise FileNotFoundError(f"No YOLO model found in backend. Candidates: {available}")


# ── Tracker helpers ────────────────────────────────────────────────────────

def _auto_tracker_tier() -> str:
    """GPU VRAM 기준 tracker tier 자동 선택."""
    if not torch.cuda.is_available():
        return "cpu"
    vram_gb = torch.cuda.get_device_properties(0).total_memory / (1024 ** 3)
    if vram_gb >= 10:
        return "high"
    if vram_gb >= 6:
        return "medium"
    if vram_gb >= 4:
        return "low"
    return "cpu"


def _build_tracker(tier: str, device: Any) -> Any:
    """boxmot tracker 인스턴스 생성. 실패 시 ByteTrack으로 폴백."""
    from boxmot import ByteTrack, OcSort, BotSort, DeepOcSort

    cfg = _TRACKER_CONFIGS.get(tier, _TRACKER_CONFIGS["cpu"])
    reid_name = cfg["reid"]

    try:
        if tier == "cpu":
            return ByteTrack()
        if tier == "low":
            return OcSort()
        # ReID 기반 tracker
        reid_path = Path(reid_name)
        cuda_dev = torch.device("cuda:0") if torch.cuda.is_available() else torch.device("cpu")
        half = torch.cuda.is_available()
        if tier == "medium":
            return BotSort(reid_weights=reid_path, device=cuda_dev, half=half)
        if tier == "high":
            return DeepOcSort(reid_weights=reid_path, device=cuda_dev, half=half)
    except Exception as exc:
        logger.warning("Tracker %s 초기화 실패 (%s) → ByteTrack 폴백", tier, exc)
        return ByteTrack()


def _sv_to_boxmot(dets: sv.Detections) -> np.ndarray:
    """sv.Detections → boxmot input array [x1,y1,x2,y2,conf,cls]"""
    if len(dets) == 0:
        return np.empty((0, 6), dtype=np.float32)
    n = len(dets)
    out = np.zeros((n, 6), dtype=np.float32)
    out[:, :4] = dets.xyxy
    out[:, 4] = dets.confidence if dets.confidence is not None else 1.0
    out[:, 5] = dets.class_id.astype(np.float32) if dets.class_id is not None else 0
    return out


def _boxmot_to_sv(tracks: np.ndarray) -> sv.Detections:
    """boxmot output [x1,y1,x2,y2,id,conf,cls,idx] → sv.Detections"""
    if tracks is None or tracks.shape[0] == 0:
        return sv.Detections.empty()
    return sv.Detections(
        xyxy=tracks[:, :4].astype(np.float32),
        tracker_id=tracks[:, 4].astype(int),
        confidence=tracks[:, 5].astype(np.float32),
        class_id=tracks[:, 6].astype(int),
    )


# ── Video source ───────────────────────────────────────────────────────────

def open_video_source(url: str) -> cv2.VideoCapture:
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    if cap.isOpened():
        logger.info("Stream connected: %s", url)
        return cap
    raise RuntimeError(f"Failed to open stream: {url}")


# ── VehicleDetector ────────────────────────────────────────────────────────

class VehicleDetector:
    CLASS_IDS = list(VEHICLE_CLASSES.keys())

    def __init__(self):
        self._device = "cuda" if torch.cuda.is_available() else "cpu"
        selection = resolve_model_selection()
        self._model_path = str(selection.path)
        self._backend = selection.backend
        self._inference_device = 0 if selection.backend == "tensorrt" else self._device

        self.model = YOLO(self._model_path, task="detect")
        if selection.backend not in ("tensorrt",):
            self.model.to(self._device)

        # 모델 락 (live_loop + ws/detect 공유)
        self._lock = threading.Lock()

        # ROI polygon (정규화 좌표 0~1). None이면 전체 프레임
        self._roi: list[list[float]] | None = None

        # boxmot tracker
        tier = TRACKER_TIER if TRACKER_TIER != "auto" else _auto_tracker_tier()
        self._tracker_tier = tier
        self._tracker_name = _TRACKER_CONFIGS.get(tier, _TRACKER_CONFIGS["cpu"])["name"]
        self._tracker = _build_tracker(tier, self._inference_device)

        logger.info(
            "YOLO: %s  backend=%s  device=%s | Tracker: %s (tier=%s)",
            selection.path.name, selection.backend, self._inference_device,
            self._tracker_name, tier,
        )

    # ── ROI ───────────────────────────────────────────────────────────

    def set_roi(self, polygon: list[list[float]] | None) -> None:
        self._roi = polygon
        logger.info("ROI %s", f"설정 ({len(polygon)}꼭짓점)" if polygon else "해제")

    def _apply_roi(self, dets: sv.Detections, frame: np.ndarray) -> sv.Detections:
        if self._roi is None or len(dets) == 0:
            return dets
        h, w = frame.shape[:2]
        polygon_px = roi_to_pixels(self._roi, w, h)
        try:
            zone = sv.PolygonZone(polygon=polygon_px)
            mask = zone.trigger(dets)
            return dets[mask]
        except Exception as exc:
            logger.warning("ROI 필터링 실패: %s", exc)
            return dets

    # ── Detection + Tracking ──────────────────────────────────────────

    def detect(self, frame: np.ndarray) -> sv.Detections:
        """Tracking 없이 detection만 수행."""
        with self._lock:
            results = self.model.predict(
                frame,
                imgsz=YOLO_IMGSZ, conf=YOLO_CONF, iou=YOLO_IOU,
                classes=self.CLASS_IDS, device=self._inference_device, verbose=False,
            )[0]
        return sv.Detections.from_ultralytics(results)

    def track(self, frame: np.ndarray) -> sv.Detections:
        """
        YOLO predict → ROI 필터 → boxmot tracker update → sv.Detections 반환.
        """
        # 1. YOLO 탐지 (tracking state 없음)
        with self._lock:
            results = self.model.predict(
                frame,
                imgsz=YOLO_IMGSZ, conf=YOLO_CONF, iou=YOLO_IOU,
                classes=self.CLASS_IDS, device=self._inference_device, verbose=False,
            )[0]
        dets = sv.Detections.from_ultralytics(results)

        # 2. ROI 필터 (tracking 전에 적용)
        dets = self._apply_roi(dets, frame)

        # 3. boxmot tracker update
        dets_np = _sv_to_boxmot(dets)
        try:
            tracks = self._tracker.update(dets_np, frame)
        except Exception as exc:
            logger.warning("Tracker update 실패: %s", exc)
            return sv.Detections.empty()

        return _boxmot_to_sv(tracks)

    def reset_tracker(self) -> None:
        try:
            self._tracker.reset()
            logger.info("Tracker reset: %s", self._tracker_name)
        except Exception as exc:
            logger.warning("Tracker reset 실패: %s", exc)

    @property
    def tracker_info(self) -> dict:
        return {
            "tracker": self._tracker_name,
            "tier": self._tracker_tier,
            "backend": self._backend,
        }


# ── VideoStream ────────────────────────────────────────────────────────────

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
        return self._cap.get(cv2.CAP_PROP_FPS) or 30.0 if self._cap else 30.0

    @property
    def url(self) -> str | None:
        return self._url

    def switch_to(self, url: str) -> None:
        if self._cap:
            self._cap.release()
            self._cap = None
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
            self._cap = None
        await asyncio.sleep(self.RECONNECT_DELAY)
        try:
            self._cap = open_video_source(self._url)
        except RuntimeError as exc:
            logger.warning("Reconnect failed: %s", exc)
            self._cap = None

    def release(self) -> None:
        if self._cap:
            self._cap.release()
            self._cap = None
