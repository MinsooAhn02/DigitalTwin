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
ITS_BASE_URL: str    = "https://openapi.its.go.kr:9443/cctvInfo"
ITS_TRAFFIC_URL: str = "https://openapi.its.go.kr:9443/trafficInfo"

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
YOLO_CONF: float = float(os.getenv("YOLO_CONF", "0.25"))
YOLO_IOU: float = float(os.getenv("YOLO_IOU", "0.45"))

YOLO_DETECT_INTERVAL: int = int(os.getenv("YOLO_DETECT_INTERVAL", "1"))

VEHICLE_CLASSES: dict[int, str] = {
    2: "car",
    3: "motorcycle",
    5: "bus",
    7: "truck",
}

# Tracking
BYTE_TRACK_FPS: int = int(os.getenv("BYTE_TRACK_FPS", "30"))
BYTE_TRACK_BUFFER: int = int(os.getenv("BYTE_TRACK_BUFFER", "30"))

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
ITS_POLL_INTERVAL: int = int(os.getenv("ITS_POLL_INTERVAL", "300"))
HLS_REFRESH_INTERVAL: int = int(os.getenv("HLS_REFRESH_INTERVAL", "1800"))

# ── History 저장 & 정체 클러스터링 ([B]/[C]) ────────────────────────────
# 단일 주기 샘플러가 bg snapshot + live 캐시를 누적 저장하는 주기 (초).
# detect 주기(8s)와 독립 — 더 큰 값으로 DB 증가 속도를 통제한다.
HISTORY_SAMPLE_S: int = int(os.getenv("HISTORY_SAMPLE_S", "30"))
# 보존 기간 — 이보다 오래된 행은 샘플러가 주기적으로 prune.
HISTORY_RETENTION_DAYS: int = int(os.getenv("HISTORY_RETENTION_DAYS", "14"))
# 카메라단위 정체 클러스터링 — 두 카메라를 같은 구간으로 묶는 최대 거리 (m).
CONGESTION_EPS_M: float = float(os.getenv("CONGESTION_EPS_M", "500"))

SPEED_JITTER_THRESHOLD_M: float = 0.5
# 물리적 상한 — 트랙 ID 스왑/호모그래피 폭주(수백 m 점프)만 거른다. 고속도로 차량이
# 보정 전(scale=1) 130~160으로 측정돼도 통과시켜야 OK 샘플이 쌓이고 ITS 보정이 시작된다.
# (과거 120은 정상 고속 차량까지 잘라 OK=0 → 속도 0 고착의 원인이었음)
MAX_REASONABLE_KPH: float = 180.0
# 이 미만의 측정 속도는 정지차 지터 노이즈로 간주해 0 처리 (정지차가 2km/h로 뜨는 문제)
SPEED_MIN_KPH: float = 5.0
GC_GRACE_FRAMES: int = 30

# 속도 출력 평활화 (0↔100 깜빡임 제거)
SPEED_EMA_ALPHA: float = 0.35      # 신규 샘플 반영 비율 (낮을수록 부드러움)
SPEED_STOP_SPAN_S: float = 1.0     # 변위<jitter가 이 시간 이상 지속돼야 '정지'로 0 감쇠
SPEED_SPIKE_FACTOR: float = 2.5    # raw > ema*factor+20 이면 이상치로 보고 EMA 미반영

# 슬라이딩 윈도우 속도 계산에 사용할 이력 프레임 수
SPEED_WINDOW_FRAMES: int = 18

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
