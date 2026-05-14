# 교통 디지털 트윈

실제 도로 CCTV 스트림을 AI로 탐지·추적하여 지도 위에 실시간 시각화하는 교통 디지털 트윈 시스템.

---

## 시스템 요구사항

| 항목 | 요구사항 |
|------|----------|
| OS | Windows 10/11 |
| Python | 3.11 이상 |
| Node.js | 18 이상 |
| GPU | NVIDIA (CUDA 12.4, TensorRT 8+) |
| Make | GnuWin32 Make |

> GPU가 없는 환경에서는 YOLO 탐지 기능이 동작하지 않습니다.

---

## 기술 스택

| 영역 | 기술 |
|------|------|
| 백엔드 | Python 3.11, FastAPI, Uvicorn |
| AI 탐지 | YOLOv8x / YOLOv8s (ultralytics), TensorRT FP16 |
| 차량 추적 | BoT-SORT (appearance ReID, ultralytics 내장) |
| 영상 처리 | OpenCV + FFmpeg (HLS 스트림) |
| 탐지 유틸 | supervision (LineZone 카운팅) |
| HTTP | httpx (비동기 ITS API 호출) |
| 프론트엔드 | React 18 + Vite |
| 지도 | deck.gl + react-map-gl + MapLibre GL |
| HLS 재생 | hls.js |
| 외부 API | ITS 국가교통정보센터 OpenAPI |

---

## 컴퓨터 성능별 추천 설정

| 환경 | 추천 모델 | 예상 추론 속도 | 실행 명령 |
|------|-----------|----------------|-----------|
| RTX 3070 이상 (고사양) | yolov8x + TensorRT FP16 | ~10ms | `make dev MODEL=x` |
| RTX 3060 / 2070 수준 (중간) | yolov8s + TensorRT FP16 | ~3ms | `make dev MODEL=s` |
| 구형 NVIDIA (GTX 1080 이하) | yolov8s + CUDA | ~30ms | `make dev MODEL=s` |
| CPU만 있는 환경 | 미지원 | ~500ms 이상 | — |

> `.engine` 파일은 반드시 **실행 GPU에서 직접 변환**해야 합니다. 다른 GPU에서 변환된 파일은 사용 불가.

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

> BoT-SORT의 선형 할당에 `lap` 패키지가 필요할 수 있습니다. 오류 발생 시: `pip install lap`

---

## 실행

프로젝트 루트(`DigitalTwin/`)에서:

```bash
make dev            # yolov8x (기본, 최고 정확도)
make dev MODEL=s    # yolov8s (경량, 저사양 환경)
```

브라우저에서 `http://localhost:5173` 접속

---

## 기타 명령어

| 명령어 | 설명 |
|--------|------|
| `make dev` | 백엔드 + 프론트엔드 동시 실행 |
| `make dev MODEL=s` | yolov8s 엔진으로 실행 |
| `make backend` | 백엔드만 실행 |
| `make frontend` | 프론트엔드만 실행 |
| `make kill` | 포트 8000 강제 해제 |

---

## 주요 기능

### 실시간 CCTV 탐지
- 지도에서 CCTV 아이콘 클릭 → HLS 영상 팝업 (hls.js)
- ITS API에서 현재 뷰포트 기준 CCTV 목록 자동 조회
- HLS 토큰 만료 시 백엔드 `/cctv-refresh` 엔드포인트로 URL 자동 갱신

### AI 탐지·추적 파이프라인

```
브라우저 <video>
    │ 캔버스 캡처 (640px JPEG, ~16ms 인터벌, 최대 2프레임 동시 전송)
    ▼
WebSocket /ws/detect
    │ YOLOv8 + BoT-SORT (TensorRT FP16, ~9.6ms)
    ▼
어노테이션 JPEG 반환 + JSON 브로드캐스트
    │
    ▼
지도 마커 / 사이드바 통계 실시간 업데이트
```

- **BoT-SORT**: appearance ReID 기반 → 차량이 잠시 사라져도 동일 ID 재할당, ID 끊김 최소화
- **탐지 대상**: car / motorcycle / bus / truck

### 교통 분석
- **속도**: Haversine 거리 + 실제 timestamp 기반 → EMA 평활화 (α=0.4)
- **주차 감지**: 연속 정지 10초(300프레임) → 통계 제외, 지도 회색 표시
  - 위치 기반 메모리: 30px 반경 내 재탐지 시 track_id 무관하게 즉시 주차 분류
- **LOS 등급**: 차량 수 기준 A–F 서비스 수준
- **경보**: 과속(60km/h 초과) + 병목(2초 이상 정지)

### 지도 시각화
- CCTV 마커, 차량 산점도, 차량 ID 레이블, 이동 궤적(TripsLayer)
- zoom ≥ 15 이상에서만 차량 표시
- 주차 차량 회색, 과속 차량 강조

---

## 아키텍처

```
브라우저 (React + deck.gl)
  ├── hls.js → CCTV 영상 재생 (NETWORK_ERROR 시 /cctv-refresh로 URL 자동 갱신)
  ├── canvas 캡처 → WS /ws/detect → BoT-SORT + YOLOv8 TensorRT → 어노테이션 반환
  └── WS /ws ← 차량 데이터 JSON 브로드캐스트 (지도 마커 + 사이드바 통계)

FastAPI 백엔드
  ├── GET  /cctvs          ITS API에서 뷰포트 내 CCTV 목록 조회
  ├── POST /switch-camera  카메라 전환 (BoT-SORT 리셋 + live_loop 스트림 전환)
  ├── GET  /cctv-refresh   HLS 토큰 만료 시 신선한 URL 반환
  ├── WS   /ws/detect      BoT-SORT + YOLO 추론, 어노테이션 JPEG 반환
  ├── WS   /ws             차량 데이터 실시간 브로드캐스트
  └── GET  /health         서버 상태 확인
```

---

## YOLO 모델 비교

| 모델 | 추론 속도 (TensorRT FP16) | 엔진 크기 | 비고 |
|------|---------------------------|-----------|------|
| yolov8x (기본) | ~9.6ms (~37fps) | 133.8MB | 최고 정확도 |
| yolov8s | ~3ms (~50fps) | 24.7MB | 저사양 환경 권장 |

> RTX 4070 Laptop (imgsz=640) 기준. 총 파이프라인 레이턴시는 ~24ms 내외.
