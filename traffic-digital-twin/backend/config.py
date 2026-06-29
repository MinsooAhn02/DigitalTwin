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
ITS_API_KEY: str = os.getenv("ITS_API_KEY", "YOUR_API_KEY_HERE")  # 국도
EX_API_KEY:  str = os.getenv("EX_API_KEY",  "YOUR_API_KEY_HERE")  # 고속도로
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
# 모델 패밀리 (yolov8 | yolo26). 프로파일(model_setup가 기록) 우선, 없으면 기본 yolo26.
YOLO_MODEL_FAMILY: str = os.getenv(
    "YOLO_MODEL_FAMILY",
    str(RUNTIME_PROFILE.get("family", "yolo26")),
).strip().lower()
YOLO_AUTO_EXPORT_ENGINE: bool = os.getenv(
    "YOLO_AUTO_EXPORT_ENGINE",
    "true",
).lower() == "true"
YOLO_IMGSZ: int = int(os.getenv("YOLO_IMGSZ", "640"))
YOLO_CONF: float = float(os.getenv("YOLO_CONF", "0.30"))
YOLO_IOU: float = float(os.getenv("YOLO_IOU", "0.45"))  # YOLOv8-era NMS threshold; ignored by YOLO26 (NMS-free)

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
# ITS 구간속도로 speed_scale을 자동 보정할지 여부.
# False(기본): ITS는 표시 전용 — speed_scale=1.0 고정, 호모그래피/캘리브 정확도로만 측정.
# True: 기존 동작 유지 (ITS 신호가 scale을 갱신).
ITS_DRIVES_SCALE: bool = os.getenv("ITS_DRIVES_SCALE", "false").lower() == "true"
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
# 카메라 snap GPS에서 이 거리(m) 초과 차량의 속도는 통계(avg/ITS 보정)에서 제외.
# bbox 1px 오차가 원거리에서 수 m로 증폭되므로 표시는 유지하되 샘플로는 쓰지 않는다.
SPEED_TRUST_MAX_DEPTH_M: float = float(os.getenv("SPEED_TRUST_MAX_DEPTH_M", "100"))
GC_GRACE_FRAMES: int = 30

# 속도 출력 평활화 (0↔100 깜빡임 제거)
SPEED_EMA_ALPHA: float = 0.35      # 신규 샘플 반영 비율 (낮을수록 부드러움)
SPEED_STOP_SPAN_S: float = 1.0     # 변위<jitter가 이 시간 이상 지속돼야 '정지'로 0 감쇠
SPEED_SPIKE_FACTOR: float = 2.5    # raw > ema*factor+20 이면 이상치로 보고 EMA 미반영

# 슬라이딩 윈도우 속도 계산 — 초 단위로 정의 후 backend_fps로 프레임수 유도.
# 모델/프로파일이 FPS를 바꿔도 측정 '시간'이 물리적으로 일관됨 (Phase 12).
SPEED_WINDOW_S: float = float(os.getenv("SPEED_WINDOW_S", "0.7"))
SPEED_WINDOW_FRAMES: int = max(6, round(SPEED_WINDOW_S * FPS))

PARKED_FRAMES_THRESHOLD: int = 300
PARKED_POSITION_RADIUS_PX: float = 30.0

CAMERA_BEARING_DEG: float = 0.0

# ── Phase 12: Road-model 포즈 캘리브 & 속도 outlier ──────────────────────────
# solve_pose 채택 임계 — reprojection RMS(px)가 이 미만이어야 포즈 캘리브 채택.
POSE_RESIDUAL_MAX_PX: float = float(os.getenv("POSE_RESIDUAL_MAX_PX", "8.0"))
# Focal free 조건: 점선 주기(종방향) 관측이 충분할 때 focal을 5번째 최적화 변수로 해방.
# MIN_OBS: 최소 관측 수 / MIN_ROW_FRAC: 관측이 프레임 높이의 이 비율 이상 span해야 함.
FOCAL_FREE_MIN_OBS: int   = int(os.getenv("FOCAL_FREE_MIN_OBS",   "3"))
FOCAL_FREE_MIN_ROW_FRAC: float = float(os.getenv("FOCAL_FREE_MIN_ROW_FRAC", "0.20"))
# vehicle scale 모델 최소 관측수 — 이제 포즈의 *보조*라 낮춤(한계2 C).
#   SCALE_MIN_OBS: 일반,  SCALE_MIN_OBS_SPARSE: 교통량 적을 때 자동 하향.
SCALE_MIN_OBS: int = int(os.getenv("SCALE_MIN_OBS", "12"))
SCALE_MIN_OBS_SPARSE: int = int(os.getenv("SCALE_MIN_OBS_SPARSE", "8"))
# 교통량 '적음' 판정 — 카메라 전환 후 이 프레임수가 지나도 일반 임계 미달이면 SPARSE 적용.
SCALE_SPARSE_AFTER_FRAMES: int = int(os.getenv("SCALE_SPARSE_AFTER_FRAMES", "600"))
# 차량간 속도 outlier 제거 MAD 계수 (한계3 A) — |x-median| > K*MAD 면 이상치.
SPEED_OUTLIER_MAD_K: float = float(os.getenv("SPEED_OUTLIER_MAD_K", "3.0"))

# ── Warm-up → commit → lock 캘리브레이션 ─────────────────────────────────────
# 카메라 전환 후 clean-plate 생성을 위한 관측 누적 기간(초). 이 시간 안에 data-driven
# commit 조건이 충족되면 즉시 lock; 미충족 시 timeout에서 fixed-focal fallback으로 lock.
WARMUP_MAX_S: float = float(os.getenv("WARMUP_MAX_S", "90.0"))
# clean-plate commit 체크 주기 (프레임 수).
WARMUP_EVAL_EVERY: int = int(os.getenv("WARMUP_EVAL_EVERY", "30"))
# clean-plate 스택 최대 프레임 수 (1/2해상도 그레이스케일 ROI, 메모리 cap).
CLEANPLATE_MAX_FRAMES: int = int(os.getenv("CLEANPLATE_MAX_FRAMES", "60"))
# clean-plate 샘플링 간격(초) — 너무 자주 샘플하면 스택이 같은 차량 위치로 포화됨.
CLEANPLATE_SAMPLE_S: float = float(os.getenv("CLEANPLATE_SAMPLE_S", "0.5"))
# early-commit 게이트: clean-plate에서 이 수 이상의 dash_obs가 있어야 focal 해방.
DASH_MIN_OBS: int = int(os.getenv("DASH_MIN_OBS", "3"))
# early-commit 게이트: clean-plate에서 이 수 이상의 lane_w_obs가 있어야 측면 앵커 신뢰.
LANE_MIN_OBS: int = int(os.getenv("LANE_MIN_OBS", "2"))

# ── Movement-based direction classification (Task 1) ──────────────────────
# Along-axis EMA threshold (m) below which direction is kept unchanged.
DIR_DEADZONE_M: float = float(os.getenv("DIR_DEADZONE_M", "0.10"))
DIR_EMA_ALPHA: float = 0.4   # smoothing for along-velocity signal

# ── Bearing auto-refinement from vehicle flow (Task 3) ────────────────────
# Minimum reliable movement samples before refining bearing.
BEARING_REFINE_MIN_SAMPLES: int = int(os.getenv("BEARING_REFINE_MIN_SAMPLES", "30"))
# How aggressively to update bearing (lower = more inertia).
BEARING_REFINE_EMA_ALPHA: float = float(os.getenv("BEARING_REFINE_EMA_ALPHA", "0.15"))
# Check and potentially refine bearing every N frames.
BEARING_REFINE_INTERVAL_FRAMES: int = int(os.getenv("BEARING_REFINE_INTERVAL_FRAMES", "30"))
# Minimum angular change (deg) before re-broadcasting updated heading.
BEARING_BROADCAST_MIN_DEG: float = float(os.getenv("BEARING_BROADCAST_MIN_DEG", "1.5"))

# ── Road-shape learning from vehicle GPS traces ───────────────────────────────
# Minimum GPS positions before attempting road_pts refinement.
ROAD_PTS_REFINE_MIN_SAMPLES: int = int(os.getenv("ROAD_PTS_REFINE_MIN_SAMPLES", "50"))
# Number of along-axis bins for polyline estimation.
ROAD_PTS_REFINE_NBINS: int = int(os.getenv("ROAD_PTS_REFINE_NBINS", "10"))

# ── Phase 1: node 위치 평활 ──────────────────────────────────────────────
POS_EMA_ALPHA: float  = 0.4    # 새 위치 반영 비율 (높을수록 반응성, 낮을수록 평활)
POS_JUMP_RESET_M: float = 8.0  # 이 거리 초과 점프면 EMA 리셋 (occlusion 재등장 대응)

# ── Phase 3: ROI GPS ring 수평선 clamp ──────────────────────────────────
FAR_CAP_M: float = 250.0       # ROI 투영 시 최대 전방거리 clamp (m) — road corridor fwd_m과 일치

# ── Phase 4: polygon 안정화 ──────────────────────────────────────────────
FOV_EMA_MIN_SAMPLES: int = 60  # 자동 추정값 반영 전 최소 누적 프레임
FOV_EMA_ALPHA: float = 0.05    # polygon 파라미터 EMA alpha (낮을수록 느리게 변경)

# ── Phase 5: 차선 분리 ───────────────────────────────────────────────────
LANE_OFFSET_M: float = 1.75    # 방향별 도로 중심선 수직 offset (half-lane ≈ 1.75m)

# ── 한국 도로 노면표시 규격 (경찰청 교통노면표시 설치·관리 매뉴얼) ────────────
# road_rank별 점선 주기(paint+gap), 없으면 "default" 사용.
# 고속도로/도시고속(101/102): paint 8m + gap 12m = 주기 20m
# 일반도로(103+):             paint 3m + gap  5m = 주기  8m
MARK_PERIOD_M: dict[str, float] = {
    "101": 20.0, "102": 20.0,
    "default": 8.0,
}
MARK_PAINT_M: dict[str, float] = {
    "101": 8.0, "102": 8.0,
    "default": 3.0,
}
MARK_GAP_M: dict[str, float] = {
    "101": 12.0, "102": 12.0,
    "default": 5.0,
}
MARK_WIDTH_M: float = 0.15      # 공칭 선폭 (0.10~0.20m)
MARK_PERIOD_TOL: float = 0.30   # 허용 오차 ±30%

# ── Tracker ────────────────────────────────────────────────────────────────
# TRACKER_TIER: "auto" | "cpu" | "low" | "medium" | "high"
#   auto   → GPU VRAM 크기 기준 자동 선택
#   cpu    → ByteTrack  (ReID 없음, VRAM 불필요)
#   low    → OcSort     (ReID 없음, 가림 강함)  ← 교통 카운팅/속도 측정 용도에 최적
#   medium → BotSort    (ReID 있음, 6~8 GB VRAM)
#   high   → DeepOcSort (ReID 있음, 8 GB+ VRAM)
# 프레임 밖 재진입 ReID가 불필요한 고정 CCTV 교통 분석 용도에는 "low"가 적합.
# ReID가 필요하면 환경변수로 오버라이드: TRACKER_TIER=medium
TRACKER_TIER: str = os.getenv("TRACKER_TIER", "low").strip().lower()
