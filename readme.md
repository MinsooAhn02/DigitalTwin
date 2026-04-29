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
- 백엔드 패키지 설치
- TensorRT FP16 엔진 변환 (`yolov8s.engine`, 약 7분 소요)
- 프론트엔드 패키지 설치

---

## 실행

프로젝트 루트(`DigitalTwin/`)에서:

```bash
make dev            # yolov8s (기본, ~3ms/frame)
make dev MODEL=x    # yolov8x (고정밀, ~9.6ms/frame)
```

백엔드 서버와 프론트엔드 창이 각각 열립니다.
브라우저에서 `http://localhost:5173` 접속

---

## 기타 명령어

| 명령어 | 설명 |
|---|---|
| `make dev` | 백엔드 + 프론트엔드 동시 실행 |
| `make dev MODEL=x` | yolov8x 엔진으로 실행 |
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
  ├── canvas 캡처 → WebSocket(/ws/detect) → YOLOv8s TensorRT → 어노테이션 반환
  └── WebSocket(/ws) ← 차량 데이터 브로드캐스트

FastAPI 백엔드
  ├── /cctvs         ITS API에서 뷰포트 내 CCTV 목록 조회
  ├── /switch-camera 카메라 전환 및 트래커 리셋
  ├── /ws/detect     YOLO 추론 (TensorRT FP16, ~3ms)
  └── /ws            차량 데이터 실시간 브로드캐스트
```

---

## YOLO 모델 비교

| 모델 | 추론속도 | 엔진 크기 | 용도 |
|---|---|---|---|
| yolov8s (기본) | ~3ms | 24.7MB | 일반 사용 권장 |
| yolov8x | ~9.6ms | 133.8MB | 최고 정확도 필요 시 |

> 두 엔진 모두 TensorRT FP16, RTX 4070 Laptop 기준
