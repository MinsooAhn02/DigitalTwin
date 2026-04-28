"""
config.py — 전역 설정값 중앙 관리
  · ITS OpenAPI 인증키 / CCTV 채널 목록
  · Perspective Transform 용 픽셀-GPS 대응점
  · 탐지 임계값, 속도 제한, LOS 경계값
  · Replay 모드 (real_world_track_data.json 기반 시뮬레이션)
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── Replay 모드 ───────────────────────────────────────────────────────
# REPLAY_MODE=true  → JSON 데이터를 프레임별로 재생 (YOLO/CCTV 불필요)
# REPLAY_MODE=false → ITS RTSP 라이브 스트림 사용
REPLAY_MODE: bool = os.getenv("REPLAY_MODE", "true").lower() == "true"
REPLAY_JSON_PATH: str = os.getenv(
    "REPLAY_JSON_PATH",
    r"C:\Users\dksal\OneDrive\바탕 화면\26 Spring\cse 327\tt\real_world_track_data.json",
)
REPLAY_FPS: int = int(os.getenv("REPLAY_FPS", "20"))   # 재생 속도 (fps)

# ── ITS 국가교통정보센터 API ──────────────────────────────────────────
ITS_API_KEY: str = os.getenv("ITS_API_KEY", "YOUR_API_KEY_HERE")
ITS_BASE_URL: str = "https://openapi.its.go.kr:9443/cctvInfo"

ITS_CCTV_IDS: list[str] = [
    "C010101",
    "C010201",
]

FALLBACK_VIDEO_PATH: str = "assets/test_traffic.mp4"

# ── YOLO 모델 설정 ────────────────────────────────────────────────────
# 모델별 CPU 속도 참고 (imgsz=320 기준):
#   yolov8n  ~50ms  (~20fps)   정확도 낮음
#   yolov8s  ~100ms (~10fps)   균형
#   yolov8m  ~200ms (~5fps)    정확도 좋음
#   yolov8x  ~500ms (~2fps)    최고 정확도
YOLO_MODEL: str = "yolov8n.pt"
YOLO_IMGSZ: int = 320    # 추론 해상도 (640→320으로 줄이면 ~4배 빠름, 정확도 소폭 하락)
YOLO_CONF: float = 0.25
YOLO_IOU: float = 0.45

# 탐지 대상 COCO 클래스 ID → 표시명
# 실제 데이터: class_id 2=car, 7=truck 확인됨
VEHICLE_CLASSES: dict[int, str] = {
    2: "car",
    3: "motorcycle",
    5: "bus",
    7: "truck",
}

# ── ByteTrack 설정 ────────────────────────────────────────────────────
BYTE_TRACK_FPS: int = 30
BYTE_TRACK_BUFFER: int = 30

# ── LineZone 통행량 카운팅 라인 (픽셀 좌표) ───────────────────────────
COUNT_LINE_START = (0, 360)
COUNT_LINE_END   = (1280, 360)

# ── Perspective Transform 랜드마크 ────────────────────────────────────
# 실제 데이터 GPS 범위: lat ~37.462, lon ~127.038 (서울 강남/분당 인근 도로)
PIXEL_POINTS = [
    [  50,   80],
    [3800,   80],
    [3800, 1800],
    [  50, 1800],
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

TAILGATING_THRESHOLD_M: float = 10.0
BOTTLENECK_DWELL_FRAMES: int = 60   # 2초 @ 30fps
