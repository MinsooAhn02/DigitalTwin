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
import math
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
    YOLO_DETECT_INTERVAL,
    YOLO_IMGSZ,
    YOLO_IOU,
    YOLO_MODEL,
    YOLO_MODEL_VARIANT,
    VEHICLE_CLASSES,
    BYTE_TRACK_BUFFER,
    BYTE_TRACK_FPS,
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
            # TRT export 실패 → PyTorch fallback (onnxruntime-gpu도 동일 CUDA 버전 필요)
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
    if vram_gb >= 3.5:  # 4 GB cards report ~3.9996 GB
        return "low"
    return "cpu"


def _build_tracker(tier: str, device: Any) -> Any:
    """boxmot tracker 인스턴스 생성. 실패 시 ByteTrack으로 폴백."""
    from boxmot import ByteTrack, OcSort, BotSort, DeepOcSort

    cfg = _TRACKER_CONFIGS.get(tier, _TRACKER_CONFIGS["cpu"])
    reid_name = cfg["reid"]

    try:
        if tier == "cpu":
            tracker = ByteTrack(
                track_thresh=YOLO_CONF,
                match_thresh=0.5,   # 관대한 IoU 매칭 → Kalman 예측 벗어난 차도 재매칭
                track_buffer=BYTE_TRACK_BUFFER,
                frame_rate=BYTE_TRACK_FPS,
            )
            # boxmot 12.x 버그: STrack.activate가 frame_id==1일 때만 is_activated=True로 설정함.
            # DETECT_INTERVAL > 1 환경에서 새 track이 영원히 반환되지 않는 문제를 패치.
            from boxmot.trackers.bytetrack.bytetrack import STrack as _STrack
            _orig_activate = _STrack.activate
            def _patched_activate(self, kf, fid, _orig=_orig_activate):
                _orig(self, kf, fid)
                self.is_activated = True
            _STrack.activate = _patched_activate
            return tracker
        if tier == "low":
            return OcSort(min_hits=1, max_age=BYTE_TRACK_BUFFER)
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


def _box_iou(a: np.ndarray, b: np.ndarray) -> float:
    ix1 = max(a[0], b[0]); iy1 = max(a[1], b[1])
    ix2 = min(a[2], b[2]); iy2 = min(a[3], b[3])
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    if inter == 0.0:
        return 0.0
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    return inter / (area_a + area_b - inter)


def _dedup_tracks(
    dets: sv.Detections,
    iou_thr: float = 0.3,
    dist_thr: float = 40.0,
) -> sv.Detections:
    """
    ByteTrack 중복 트랙 제거.
    Kalman 유령 트랙과 실제 트랙이 겹치지 않아도 중심거리가 가까우면 동일 차로 판단.
      - IoU > iou_thr  OR  중심 거리 < dist_thr(px)  → 높은 ID(최신) 제거
    """
    if len(dets) < 2 or dets.tracker_id is None:
        return dets
    boxes = dets.xyxy
    ids   = dets.tracker_id
    keep  = np.ones(len(dets), dtype=bool)
    cx = (boxes[:, 0] + boxes[:, 2]) / 2
    cy = (boxes[:, 1] + boxes[:, 3]) / 2
    for i in range(len(dets)):
        if not keep[i]:
            continue
        for j in range(i + 1, len(dets)):
            if not keep[j]:
                continue
            is_dup = (
                _box_iou(boxes[i], boxes[j]) > iou_thr
                or math.hypot(cx[i] - cx[j], cy[i] - cy[j]) < dist_thr
            )
            if is_dup:
                if ids[i] <= ids[j]:
                    keep[j] = False
                else:
                    keep[i] = False
                    break
    return dets if keep.all() else dets[keep]


# ── ID 안정화 (ByteTrack ReID 없음 보완) ──────────────────────────────────

class IDStabilizer:
    """
    ByteTrack이 잠깐 놓친 차량에 새 ID를 부여할 때 이전 ID로 복원.
    last known center 위치 기반 헝가리안 nearest-neighbor 매칭.
    """

    def __init__(self, max_lost_frames: int = 90, max_dist_px: float = 80.0):
        self._max_lost = max_lost_frames
        self._max_dist = max_dist_px
        self._prev_centers: dict[int, tuple[float, float]] = {}
        self._lost: dict[int, list] = {}   # stable_id → [cx, cy, age]
        self._remap: dict[int, int] = {}   # raw_id → stable_id
        self._display_map: dict[int, int] = {}  # stable_id → compact sequential display_id
        self._next_display: int = 0

    def update(
        self,
        dets: sv.Detections,
        frame_shape: tuple[int, int] | None = None,  # (h, w)
    ) -> sv.Detections:
        # 이전 프레임 활성 트랙을 매칭 루프 전에 lost로 이동
        # (루프 안에서 매칭되면 즉시 lost에서 제거)
        for sid, pos in self._prev_centers.items():
            self._lost.setdefault(sid, [pos[0], pos[1], 0])
        self._prev_centers.clear()

        # 프레임 경계 근처에서 사라진 트랙은 화면 이탈로 간주 → 즉시 제거
        # (새로 진입하는 차량에 이탈 차량의 ID가 재할당되는 것을 방지)
        if frame_shape is not None:
            h, w = frame_shape
            margin = 50
            to_remove = [
                sid for sid, entry in self._lost.items()
                if entry[2] == 0  # 이번 프레임에 처음 사라진 트랙만
                and (entry[0] < margin or entry[0] > w - margin
                     or entry[1] < margin or entry[1] > h - margin)
            ]
            for sid in to_remove:
                del self._lost[sid]
                self._remap = {r: s for r, s in self._remap.items() if s != sid}

        if len(dets) == 0 or dets.tracker_id is None:
            self._age_lost()
            return dets

        raw_ids = dets.tracker_id.tolist()
        centers = [
            ((float(dets.xyxy[i][0]) + float(dets.xyxy[i][2])) / 2,
             (float(dets.xyxy[i][1]) + float(dets.xyxy[i][3])) / 2)
            for i in range(len(dets))
        ]

        stable_ids: list[int] = []
        used_lost: set[int] = set()

        for raw_id, (cx, cy) in zip(raw_ids, centers):
            if raw_id in self._remap:
                stable = self._remap[raw_id]
                self._lost.pop(stable, None)  # 재등장 → lost에서 제거
            else:
                stable = self._find_lost(cx, cy, used_lost)
                if stable is not None:
                    used_lost.add(stable)
                    self._lost.pop(stable, None)
                    self._remap[raw_id] = stable
                else:
                    self._remap[raw_id] = raw_id
                    stable = raw_id
                    self._lost.pop(stable, None)
            stable_ids.append(stable)

        self._age_lost()
        self._prev_centers = dict(zip(stable_ids, centers))

        # 표시용 ID를 1부터 시작하는 순차 번호로 정규화
        # (ByteTrack 내부 카운터가 200+ 로 튀어도 사용자에게는 1,2,3... 으로 표시)
        display_ids: list[int] = []
        for sid in stable_ids:
            if sid not in self._display_map:
                self._next_display += 1
                self._display_map[sid] = self._next_display
            display_ids.append(self._display_map[sid])

        return sv.Detections(
            xyxy=dets.xyxy,
            tracker_id=np.array(display_ids, dtype=int),
            confidence=dets.confidence,
            class_id=dets.class_id,
        )

    def _find_lost(self, cx: float, cy: float, used: set[int]) -> int | None:
        best, best_dist = None, self._max_dist
        for sid, (lx, ly, _) in self._lost.items():
            if sid in used:
                continue
            d = math.hypot(cx - lx, cy - ly)
            if d < best_dist:
                best_dist, best = d, sid
        return best

    def _age_lost(self) -> None:
        for sid in list(self._lost):
            self._lost[sid][2] += 1
            if self._lost[sid][2] > self._max_lost:
                del self._lost[sid]
                self._remap = {r: s for r, s in self._remap.items() if s != sid}
                self._display_map.pop(sid, None)

    def reset(self) -> None:
        self._prev_centers.clear()
        self._lost.clear()
        self._remap.clear()
        self._display_map.clear()
        self._next_display = 0


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
        if selection.backend not in ("tensorrt", "onnx"):
            self.model.to(self._device)

        self._half = self._device == "cuda" and selection.backend not in ("tensorrt", "onnx")
        self._lock = threading.Lock()
        self._track_frame_count: int = 0
        self._last_dets_np: np.ndarray = np.empty((0, 6))
        self._last_tracks: sv.Detections = sv.Detections.empty()
        self._detect_interval = max(1, int(YOLO_DETECT_INTERVAL))

        # ROI polygon (정규화 좌표 0~1). None이면 전체 프레임
        self._roi: list[list[float]] | None = None

        # boxmot tracker
        tier = TRACKER_TIER if TRACKER_TIER != "auto" else _auto_tracker_tier()
        self._tracker_tier = tier
        self._tracker_name = _TRACKER_CONFIGS.get(tier, _TRACKER_CONFIGS["cpu"])["name"]
        self._tracker = _build_tracker(tier, self._inference_device)

        # CPU(ByteTrack) 전용 ID 안정화 후처리
        self._id_stabilizer = IDStabilizer(
            max_lost_frames=BYTE_TRACK_BUFFER,
            max_dist_px=80.0,
        )

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
                classes=self.CLASS_IDS, device=self._inference_device,
                half=self._half, verbose=False,
            )[0]
        return sv.Detections.from_ultralytics(results)

    def track(self, frame: np.ndarray) -> sv.Detections:
        """
        YOLO predict → ROI 필터 → boxmot tracker update → sv.Detections 반환.
        YOLO_DETECT_INTERVAL 프레임마다 한 번만 추론, 나머지는 tracker 예측으로 보간.
        """
        self._track_frame_count += 1
        should_detect = (self._track_frame_count - 1) % self._detect_interval == 0
        tracker_input = np.empty((0, 6), dtype=np.float32)

        # 1. YOLO_DETECT_INTERVAL 마다 YOLO 추론
        if should_detect:
            with self._lock:
                results = self.model.predict(
                    frame,
                    imgsz=YOLO_IMGSZ, conf=YOLO_CONF, iou=YOLO_IOU,
                    classes=self.CLASS_IDS, device=self._inference_device,
                    half=self._half, verbose=False,
                )[0]
            dets = sv.Detections.from_ultralytics(results)
            logger.debug("YOLO raw: %d dets, backend=%s", len(dets), self._backend)
            dets = self._apply_roi(dets, frame)
            logger.debug("After ROI: %d dets", len(dets))
            self._last_dets_np = _sv_to_boxmot(dets)
            tracker_input = self._last_dets_np
            if len(tracker_input) > 0:
                logger.debug("Tracker input[0]: %s", tracker_input[0])

        # 2. boxmot tracker update (항상 실행 — Kalman 예측 유지)
        try:
            tracks = self._tracker.update(tracker_input, frame)
            logger.debug(
                "Tracker output: shape=%s  tracks=%s",
                tracks.shape if tracks is not None else None,
                tracks[:, :5] if tracks is not None and tracks.ndim == 2 and len(tracks) > 0 else "[]",
            )
        except Exception as exc:
            logger.warning("Tracker update 실패: %s", exc)
            return self._last_tracks

        result = _boxmot_to_sv(tracks)

        # boxmot 12.x: 비탐지 프레임에서 (0,) 반환 시 마지막 결과 유지
        if len(result) == 0 and not should_detect:
            return self._last_tracks

        # ReID 없는 tier(cpu/low): 중복 트랙 제거 후 ID 복원
        if self._tracker_tier in ("cpu", "low"):
            result = _dedup_tracks(result)
            result = self._id_stabilizer.update(result, frame.shape[:2])

        self._last_tracks = result
        return result

    def reset_tracker(self) -> None:
        self._last_tracks = sv.Detections.empty()
        self._last_dets_np = np.empty((0, 6))
        self._track_frame_count = 0
        self._id_stabilizer.reset()
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
