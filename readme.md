# 교통 디지털 트윈

실시간 CCTV 영상에서 차량을 탐지하고 3D 지도 위에 시각화하는 교통 디지털 트윈 대시보드입니다.

---

## 시스템 요구사항

| 항목 | 요구사항 |
|---|---|
| OS | Windows 10/11 |
| Python | 3.13 이상 |
| Node.js | 18 이상 |
| GPU | NVIDIA (CUDA 12.4, TensorRT 10) |
| Make | GnuWin32 Make |

> GPU가 없는 환경에서는 YOLO 탐지 기능이 동작하지 않습니다.

---

## 컴퓨터 성능별 추천 설정

| 환경 | 추천 모델 | 예상 추론 속도 | 실행 명령어 |
|---|---|---|---|
| NVIDIA RTX 3070 이상 (고사양) | yolov8x + TensorRT FP16 | ~10ms | `make dev MODEL=x` |
| NVIDIA RTX 3060 / 2070 수준 (중간) | yolov8s + TensorRT FP16 | ~3ms | `make dev MODEL=s` |
| 구형 NVIDIA GPU (GTX 1080 이하) | yolov8s + CUDA (TensorRT 미지원 가능) | ~30ms | `make dev MODEL=s` |
| CPU만 있는 환경 | 미지원 | ~500ms 이상 | — |

> 두 엔진 모두 해당 GPU에서 직접 변환(`make dev` 최초 실행 시 자동 처리)해야 합니다.
> 다른 GPU에서 변환된 `.engine` 파일은 사용할 수 없습니다.

---

## 최초 설치 (처음 한 번만)

### 1. Make 설치

```powershell
winget install GnuWin32.Make
$env:PATH += ";C:\Program Files (x86)\GnuWin32\bin"
[System.Environment]::SetEnvironmentVariable("PATH", $env:PATH + ";C:\Program Files (x86)\GnuWin32\bin", "User")
```

### 2. API 키 설정

`traffic-digital-twin/backend/.env` 파일 생성:

```env
ITS_API_KEY=발급받은_ITS_API_키
```

> ITS OpenAPI 키 발급: [https://openapi.its.go.kr](https://openapi.its.go.kr) → 회원가입 → cctvInfo API 신청

### 3. 환경 자동 설치

```bash
cd traffic-digital-twin
make dev
```

최초 실행 시 자동으로 처리됩니다:
- Python 가상환경 (`.venv`) 생성
- CUDA 12.4 PyTorch + TensorRT 설치
- 백엔드 패키지 설치 (`pip install -r requirements.txt`)
- TensorRT FP16 엔진 변환 (약 10분 소요)
- 프론트엔드 패키지 설치 (`npm install`)

**직접 설치할 경우 (`make dev` 대신):**

```bash
# 백엔드
cd traffic-digital-twin/backend
pip install -r requirements.txt
# PyTorch CUDA 및 TensorRT는 Makefile 참고하여 별도 설치 필요

# 프론트엔드
cd traffic-digital-twin/frontend
npm install
```

---

## 실행

프로젝트 루트(`DigitalTwin/`)에서:

```bash
make dev            # yolov8x (기본, 최고 정확도)
make dev MODEL=s    # yolov8s (경량, 저사양 환경)
```

백엔드 서버와 프론트엔드 창이 각각 열립니다.
브라우저에서 `http://localhost:5173` 접속

---

## 기타 명령어

| 명령어 | 설명 |
|---|---|
| `make dev` | 백엔드 + 프론트엔드 동시 실행 |
| `make dev MODEL=s` | yolov8s 엔진으로 실행 |
| `make backend` | 백엔드만 실행 |
| `make frontend` | 프론트엔드만 실행 |
| `make kill` | 포트 8000 강제 해제 |

---

## 주요 기능

- **실시간 CCTV 탐지** — ITS HLS 스트림에서 YOLOv8 + ByteTrack으로 차량 탐지 및 추적
- **3D 지도 시각화** — 탐지된 차량 위치·궤적을 실시간으로 지도에 표시
- **CCTV 패널** — 지도에서 카메라 아이콘 클릭 시 실시간 영상 + YOLO 탐지 화면 팝업
- **통계 대시보드** — 평균 속도, 서비스 수준(LOS), 차종 분포, 진입/진출 카운터
- **이상 경보** — 과속 / 꼬리물기 / 병목 실시간 알림

---

## 아키텍처

```
브라우저 (React + deck.gl)
  ├── HLS.js → CCTV 영상 재생
  ├── canvas 캡처 → WebSocket(/ws/detect) → YOLOv8 TensorRT → 어노테이션 반환
  └── WebSocket(/ws) ← 차량 데이터 브로드캐스트

FastAPI 백엔드
  ├── /cctvs         ITS API에서 뷰포트 내 CCTV 목록 조회
  ├── /switch-camera 카메라 전환 및 트래커 리셋
  ├── /ws/detect     YOLO 추론 (TensorRT FP16)
  └── /ws            차량 데이터 실시간 브로드캐스트
```

---

## YOLO 모델 비교

| 모델 | 추론속도 (TensorRT FP16) | 엔진 크기 | 비고 |
|---|---|---|---|
| yolov8x (기본) | ~10ms | 133.8MB | 최고 정확도, 현재 선택 |
| yolov8s | ~3ms | 24.7MB | 저사양 환경 권장 |

> RTX 4070 Laptop 기준 측정값. 실제 파이프라인(WebSocket 포함) 레이턴시는 약 50ms 내외.
