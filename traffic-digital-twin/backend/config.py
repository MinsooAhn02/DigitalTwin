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

# ── ITS 국가교통정보센터 API ──────────────────────────────────────────
ITS_API_KEY: str = os.getenv("ITS_API_KEY", "YOUR_API_KEY_HERE")
ITS_BASE_URL: str = "https://openapi.its.go.kr:9443/cctvInfo"

ITS_CCTV_IDS: list[str] = [
    "C010101",
    "C010201",
]

FALLBACK_VIDEO_PATH: str = "assets/test_traffic.mp4"

# ── YOLO 모델 설정 ────────────────────────────────────────────────────
# GPU(RTX 4070 Laptop) 기준 속도 참고 (imgsz=640):
#   yolov8n  ~5ms  (~200fps)   정확도 낮음
#   yolov8s  ~8ms  (~120fps)   균형
#   yolov8m  ~15ms (~65fps)    정확도 좋음
#   yolov8x  ~33ms (~30fps)    최고 정확도  ← 현재 선택
YOLO_MODEL: str = os.path.join(os.path.dirname(__file__), os.getenv("YOLO_MODEL", "yolov8s.engine"))
YOLO_IMGSZ: int = 640
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
