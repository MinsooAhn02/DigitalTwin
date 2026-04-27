# 교통 디지털 트윈

실시간 교통 CCTV 데이터를 3D 지도 위에 시각화하는 디지털 트윈 대시보드입니다.

---

## 빠른 시작

### 1. make 설치 (최초 1회)

PowerShell에서 실행:

```powershell
winget install GnuWin32.Make
```

설치 후 PATH 등록:

```powershell
$env:PATH += ";C:\Program Files (x86)\GnuWin32\bin"
[System.Environment]::SetEnvironmentVariable("PATH", $env:PATH + ";C:\Program Files (x86)\GnuWin32\bin", "User")
```

> 이미 설치되어 있으면 PATH 등록만 하면 됩니다.

---

### 2. 의존성 설치 (최초 1회)

백엔드:

```bash
cd traffic-digital-twin/backend
pip install -r requirements.txt
```

프론트엔드:

```bash
cd traffic-digital-twin/frontend
npm install
```

---

### 3. 실행

프로젝트 루트(`DigitalTwin/`)에서:

```bash
make dev
```

- 포트 8000 충돌을 자동으로 해제합니다
- 백엔드 서버 창과 프론트엔드 창이 각각 열립니다
- 브라우저에서 `http://localhost:5173` 접속

---

## 기타 명령어

| 명령어 | 설명 |
|---|---|
| `make dev` | 백엔드 + 프론트엔드 동시 실행 |
| `make backend` | 백엔드만 실행 |
| `make frontend` | 프론트엔드만 실행 |
| `make kill` | 포트 8000 강제 해제 |

---

## 주요 기능

- 실시간 차량 위치 및 궤적 시각화 (WebSocket)
- CCTV 아이콘 클릭 시 해당 카메라 위치로 이동
- zoom 15 이상 확대 시 차량 표시 (성능 최적화)
- 과속 / 꼬리물기 / 병목 경보
- 차종 분포 · 평균 속도 · 서비스 수준(LOS) 대시보드

---

## 설정

`traffic-digital-twin/backend/.env` 파일에서 모드를 변경할 수 있습니다:

```env
REPLAY_MODE=true      # true: JSON 재생 모드 / false: 실제 CCTV 라이브
ITS_API_KEY=your_key  # ITS 국가교통정보센터 API 키
```
