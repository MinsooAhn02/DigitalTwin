"""
Central backend configuration.
"""

import os
import json
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BACKEND_DIR = Path(__file__).resolve().parent
YOLO_CHOICE_FILE = BACKEND_DIR / ".yolo_model"
RUNTIME_PROFILE_FILE = BACKEND_DIR / ".runtime_profile.json"


def _read_yolo_choice() -> str:
    if not YOLO_CHOICE_FILE.exists():
        return ""
    return YOLO_CHOICE_FILE.read_text(encoding="utf-8").strip()


def _read_runtime_profile() -> dict:
    if not RUNTIME_PROFILE_FILE.exists():
        return {}
    try:
        return json.loads(RUNTIME_PROFILE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


RUNTIME_PROFILE = _read_runtime_profile()

# ITS OpenAPI
ITS_API_KEY: str = os.getenv("ITS_API_KEY", "YOUR_API_KEY_HERE")
ITS_BASE_URL: str = "https://openapi.its.go.kr:9443/cctvInfo"

ITS_CCTV_IDS: list[str] = [
    "C010101",
    "C010201",
]

FALLBACK_VIDEO_PATH: str = "assets/test_traffic.mp4"

# YOLO runtime selection
# - YOLO_MODEL: explicit filename/path override
# - YOLO_MODEL_VARIANT: x/s/n style hint
# - YOLO_AUTO_EXPORT_ENGINE: export .pt -> .engine on first run when TensorRT is usable
YOLO_MODEL: str = os.getenv("YOLO_MODEL", "").strip() or _read_yolo_choice()
YOLO_MODEL_VARIANT: str = os.getenv(
    "YOLO_MODEL_VARIANT",
    os.getenv("MODEL", ""),
).strip().lower()
YOLO_AUTO_EXPORT_ENGINE: bool = os.getenv(
    "YOLO_AUTO_EXPORT_ENGINE",
    "true",
).lower() == "true"
YOLO_IMGSZ: int = int(os.getenv("YOLO_IMGSZ", "640"))
YOLO_CONF: float = 0.25
YOLO_IOU: float = 0.45

YOLO_DETECT_INTERVAL: int = 3

VEHICLE_CLASSES: dict[int, str] = {
    2: "car",
    3: "motorcycle",
    5: "bus",
    7: "truck",
}

# Tracking
BYTE_TRACK_FPS: int = 30
BYTE_TRACK_BUFFER: int = 90

# LineZone
COUNT_LINE_START = (0, 360)
COUNT_LINE_END = (1280, 360)

# Perspective transform
PIXEL_POINTS = [
    [0, 0],
    [640, 0],
    [640, 360],
    [0, 360],
]
GPS_POINTS = [
    [37.4632, 127.0382],
    [37.4632, 127.0390],
    [37.4620, 127.0390],
    [37.4620, 127.0382],
]

REAL_WORLD_WIDTH_M: float = 20.0
REAL_WORLD_HEIGHT_M: float = 60.0

RUNTIME_PROFILE_NAME: str = str(RUNTIME_PROFILE.get("profile", "quality"))
CAPTURE_INTERVAL_MS: int = int(RUNTIME_PROFILE.get("capture_interval_ms", 33))
CAPTURE_WIDTH: int = int(RUNTIME_PROFILE.get("capture_width", 640))
CAPTURE_QUALITY: float = float(RUNTIME_PROFILE.get("capture_quality", 0.92))
MAX_IN_FLIGHT: int = int(RUNTIME_PROFILE.get("max_in_flight", 2))
JPEG_QUALITY: int = int(RUNTIME_PROFILE.get("jpeg_quality", 85))

# Traffic analytics
SPEED_LIMIT_KPH: float = float(os.getenv("SPEED_LIMIT_KPH", "120"))
FPS: int = int(RUNTIME_PROFILE.get("backend_fps", 30))

LOS_THRESHOLDS: dict[str, int] = {
    "A": 3,
    "B": 6,
    "C": 9,
    "D": 12,
    "E": 15,
}

BOTTLENECK_DWELL_FRAMES: int = int(os.getenv("BOTTLENECK_DWELL_FRAMES", "150"))

SPEED_JITTER_THRESHOLD_M: float = 0.20
SPEED_SMOOTHING_ALPHA: float = 0.15
MAX_REASONABLE_KPH: float = 180.0
GC_GRACE_FRAMES: int = 5

PARKED_FRAMES_THRESHOLD: int = 300
PARKED_POSITION_RADIUS_PX: float = 30.0

CAMERA_BEARING_DEG: float = 0.0

# ── Tracker ────────────────────────────────────────────────────────────────
# TRACKER_TIER: "auto" | "cpu" | "low" | "medium" | "high"
#   auto   → GPU VRAM 크기 기준 자동 선택
#   cpu    → ByteTrack  (ReID 없음, VRAM 불필요)
#   low    → OcSort     (ReID 없음, 가림 강함)
#   medium → BotSort    (ReID 있음, 6~8 GB VRAM)
#   high   → DeepOcSort (ReID 있음, 8 GB+ VRAM)
TRACKER_TIER: str = os.getenv("TRACKER_TIER", "auto").strip().lower()
