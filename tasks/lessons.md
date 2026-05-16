# 개발 교훈 (Lessons Learned)

## 2026-05-16

### L1. ITS API 단건 응답은 dict (list가 아님)
- **현상**: CCTV 1개만 반환될 때 `data`가 `dict`로 옴 — `for item in data` 시 key 이름("coordx" 등)을 순회
- **수정**: `[raw] if isinstance(raw, dict) else raw` 분기 처리 (3곳: `/cctvs`, `/cctv-refresh`, `hls_refresh_loop`)
- **교훈**: 외부 API는 반환 타입이 수량에 따라 바뀔 수 있음. 항상 방어적으로 처리.

### L2. boxmot 트래커 인스턴스 공유 금지
- **현상**: `live_loop`와 `ws/detect`가 동일 `VehicleDetector.tracker` 인스턴스를 동시에 업데이트 → 서로 다른 프레임 시퀀스가 섞여 트래킹 완전 붕괴
- **수정**: `_detect_clients > 0` 시 `live_loop`는 `detector.track()` 스킵. `ws/detect` 종료 시 `reset_tracker()` 호출.
- **교훈**: 상태를 유지하는 컴포넌트(트래커, 칼만필터 등)는 파이프라인당 독립 인스턴스를 써야 함.

### L3. Auto-ROI가 탐지 성능을 해친다
- **현상**: Canny edge detection 기반 자동 ROI가 건물·교량 외곽을 도로로 오인 → 이상한 좌표로 ROI 저장 → 차량 대부분 필터링
- **수정**: 카메라 전환 시 auto-estimate 및 자동 저장 제거. 수동으로 저장된 ROI만 적용.
- **교훈**: 자동 추정 기능은 탐지 파이프라인에 직접 연결하지 말고, UI에서 사용자가 확인 후 적용하게 해야 함.

### L4. Pydantic 모델 필드명은 프론트엔드와 반드시 일치
- **현상**: `CalibBody.camera_key`를 기대했지만 프론트엔드가 `cctvurl`을 전송 → 422 Unprocessable Entity
- **수정**: 백엔드에서 `cctvurl`을 받아 `camera_key`를 내부에서 계산
- **교훈**: API 설계 시 "프론트엔드가 자연스럽게 갖고 있는 값"을 받는 것이 맞음. 서버 내부 ID 체계를 클라이언트에 노출하지 않는다.

### L5. CalibrationMode canvas 레이아웃은 RoiEditor 패턴을 따라야 함
- **현상**: flex column으로 배너+캔버스 나눴을 때 `canvas.offsetHeight` ≠ video container height → 좌표 매핑 오차
- **수정**: canvas를 `position:absolute; inset:0`으로 전체 컨테이너 커버, 배너는 absolute overlay
- **교훈**: video letterbox 좌표 계산은 컨테이너 전체 기준으로 해야 objectFit:contain 매핑이 정확함.

### L6. useCallback dependency array 빠뜨리면 stale closure 발생
- **현상**: `handleCctvClick`에서 `calMode` 읽었지만 `[]`으로 선언 → 보정 중에도 카메라 전환 가능
- **수정**: `[calMode]` 추가
- **교훈**: state를 읽는 callback은 반드시 해당 state를 dependency에 포함.

## 2026-05-17

### L7. useEffect cleanup에서 블록 스코프 변수에 접근 불가 → React 흰 화면
- **현상**: `if (Hls.isSupported()) { const loadTimeout = setTimeout(...) }` 안에서 선언 후, cleanup 함수 `return () => { clearTimeout(loadTimeout) }` 실행 시 `ReferenceError` → React 오류 경계가 없으면 전체 앱 흰 화면
- **수정**: `let loadTimeout = null`을 `if` 블록 밖 useEffect 스코프에 선언, 블록 안에서 할당
- **교훈**: useEffect cleanup에서 접근할 변수는 반드시 cleanup과 같은 스코프(또는 상위)에 선언해야 함. `const`를 조건 블록 안에 쓰면 cleanup에서 참조 불가.

### L8. HLS destroy 후 video 요소에 이전 프레임이 남음 → 뿌연 화면
- **현상**: `hls.destroy()` 호출해도 `<video>` DOM 요소가 마지막으로 디코딩한 프레임을 유지함. 로딩 오버레이가 반투명(0.75)이면 이전 카메라 화면이 비쳐 보임
- **수정**: `hls.destroy()` 직후 `video.src = ""; video.load()` → 프레임 클리어. 로딩 오버레이 `background: "rgba(0,0,0,1)"` (완전 불투명)
- **교훈**: HLS 인스턴스와 video DOM 요소는 별개. destroy는 HLS 내부 상태만 정리하고 video 버퍼는 직접 비워야 함.

### L9. speed 0→폭발 반복의 3가지 근본 원인
- **현상**: 차량 추적 중 speed_kph가 0→100→0→90 식으로 매 프레임 날뜀
- **원인 3가지**:
  1. `_gc()` 즉각 삭제: 1프레임 미감지 시 `_prev[tid]` 삭제 → 재감지 시 이전 위치 없어서 속도 0 시작 → 다음 프레임 큰 dt로 폭발
  2. bbox 노이즈가 `SPEED_JITTER_THRESHOLD_M` 경계를 넘나들며 0⟷raw_speed 반복
  3. `SPEED_SMOOTHING_ALPHA=0.4` 너무 반응적 → 노이즈 측정값이 EMA에 40% 반영
- **수정**: (1) 5프레임 grace period `_lost_frames` 도입 (2) `raw_kph > 180` 이상치 스킵 (3) alpha 0.4→0.15
- **교훈**: 속도 노이즈는 단일 원인이 아님. 트래킹 연속성(GC) + 이상치 제거 + EMA smoothing 세 층을 모두 잡아야 함.

### L10. Tkinter BooleanVar trace_add는 forward reference로 동작
- **현상**: `_on_cuda_toggle`이 `fps_labels`를 참조하는데 `fps_labels`가 그 아래 줄에서 정의됨 → 실행 시 이미 채워진 후 호출되므로 문제 없음
- **교훈**: Python 클로저는 호출 시점에 변수를 조회(late binding). trace callback은 UI 렌더링 완료 후에만 실행되므로 forward reference가 안전함. 단, 모듈 레벨에서 즉시 실행되는 코드라면 순서를 지켜야 함.
