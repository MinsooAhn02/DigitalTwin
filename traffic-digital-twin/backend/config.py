"""
config.py — 전역 설정값 중앙 관리
  · ITS OpenAPI 인증키 / CCTV 채널 목록
  · Perspective Transform 용 픽셀-GPS 대응점
  · 탐지 임계값, 속도 제한, LOS 경계값
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── ITS 국가교통정보센터 API ──────────────────────────────────────────
ITS_API_KEY: str = os.getenv("ITS_API_KEY", "YOUR_API_KEY_HERE")
ITS_BASE_URL: str = "https://openapi.its.go.kr:9443/cctvInfo"

ITS_CCTV_IDS: list[str] = [
    "C010101",
    "C010201",
]

FALLBACK_VIDEO_PATH: str = "assets/test_traffic.mp4"

# ── YOLO 모델 설정 ────────────────────────────────────────────────────
# 사용 가능한 engine (TensorRT FP16, RTX 4070 Laptop, imgsz=640 기준):
#   yolov8x.engine  ~9.6ms (~37fps)  최고 정확도  ← 현재 선택
#   yolov8s.engine  ~3ms   (~50fps)  균형
# 전환: YOLO_MODEL=yolov8s.engine 환경변수로 서버 실행 시 변경 가능
YOLO_MODEL: str = os.path.join(os.path.dirname(__file__), os.getenv("YOLO_MODEL", "yolov8x.engine"))
YOLO_IMGSZ: int = 640
YOLO_CONF: float = 0.25
YOLO_IOU: float = 0.45

# detect-and-track: 이 프레임마다 1회 YOLO 추론, 나머지는 ByteTrack Kalman 예측
YOLO_DETECT_INTERVAL: int = 3

# 탐지 대상 COCO 클래스 ID → 표시명
VEHICLE_CLASSES: dict[int, str] = {
    2: "car",
    3: "motorcycle",
    5: "bus",
    7: "truck",
}

# ── ByteTrack 설정 ────────────────────────────────────────────────────
BYTE_TRACK_FPS: int = 30
BYTE_TRACK_BUFFER: int = 90    # 3초 유지 (YOLO 일시 실패 시 ID 보존)

# ── LineZone 통행량 카운팅 라인 (픽셀 좌표) ───────────────────────────
COUNT_LINE_START = (0, 360)
COUNT_LINE_END   = (1280, 360)

# ── Perspective Transform 랜드마크 ────────────────────────────────────
# 실제 프레임 해상도(640×360) 네 모서리를 GPS 격자에 매핑
# GPS는 update_gps_center()가 카메라 선택 시 동적으로 재보정
PIXEL_POINTS = [
    [  0,   0],   # 좌상
    [640,   0],   # 우상
    [640, 360],   # 우하
    [  0, 360],   # 좌하
]
GPS_POINTS = [
    [37.4632, 127.0382],  # 좌상
    [37.4632, 127.0390],  # 우상
    [37.4620, 127.0390],  # 우하
    [37.4620, 127.0382],  # 좌하
]

REAL_WORLD_WIDTH_M: float  = 20.0
REAL_WORLD_HEIGHT_M: float = 60.0

# ── 속도 / LOS 임계값 ─────────────────────────────────────────────────
SPEED_LIMIT_KPH: float = 60.0    # 도심 도로 기준
FPS: int = 30

LOS_THRESHOLDS: dict[str, int] = {
    "A": 3,
    "B": 6,
    "C": 9,
    "D": 12,
    "E": 15,
}

BOTTLENECK_DWELL_FRAMES: int = 60   # 2초 @ 30fps

# ── 속도 정확도 ──────────────────────────────────────────────────────────
SPEED_JITTER_THRESHOLD_M: float = 0.30   # 프레임 간 이동 < 이 값이면 정지로 간주
SPEED_SMOOTHING_ALPHA: float = 0.4       # EMA 계수 (0~1, 낮을수록 더 평활화)

# ── 주차 차량 자동 감지 ──────────────────────────────────────────────────
PARKED_FRAMES_THRESHOLD: int = 300         # 연속 정지 이 프레임 이상 = 주차 (30fps × 10초)
PARKED_POSITION_RADIUS_PX: float = 30.0   # 이 픽셀 반경 내 신규 탐지 → 즉시 주차 분류

# ── 카메라 베어링 보정 ────────────────────────────────────────────────────
CAMERA_BEARING_DEG: float = 0.0          # CCTV가 정북 기준 시계 방향으로 틀어진 각도(도)
