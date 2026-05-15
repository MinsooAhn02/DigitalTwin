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
