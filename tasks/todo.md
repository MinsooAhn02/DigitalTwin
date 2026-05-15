# Digital Twin 개선 TODO

## Phase 1 — 버그 수정 ✅ 완료
- [x] 1-1. backend/main.py — camera_ready WS 신호 + 큐 드레인 + 스트림 비동기 전환
- [x] 1-2. backend/main.py — switch_to를 asyncio.to_thread로 비동기 호출
- [x] 1-3. frontend/hooks/useWebSocket.js — camera_ready 메시지 타입 분기
- [x] 1-4. frontend/src/App.jsx — switching 상태를 WS 신호로 제어, setTimeout 제거
- [x] 1-5. frontend/src/App.jsx AlertPanel — React.memo + useMemo 최적화

## Phase 2 — ROI 마스킹 ✅ 완료
- [x] 2-1. backend/roi_manager.py — 자동추정(OpenCV edge detection) + 저장/로드 (정규화 좌표)
- [x] 2-2. backend/main.py — /roi GET/POST/DELETE + 카메라 전환 시 자동 ROI 추정
- [x] 2-3. backend/detector.py — set_roi() + _apply_roi(sv.PolygonZone 필터링)
- [x] 2-4. frontend/src/components/RoiEditor.jsx — canvas 오버레이, 클릭/더블클릭 polygon
- [x] 2-5. frontend/src/components/CctvPlayer.jsx — ROI 편집 탭 추가

## Phase 3 — Tracker & 백엔드 업그레이드 ✅ 완료
- [x] 3-1. pip install boxmot(--no-deps) + scikit-learn + filterpy etc + cachetools
- [x] 3-2. backend/config.py — TRACKER_TIER env var (auto/cpu/low/medium/high)
- [x] 3-3. backend/detector.py — boxmot 연동: predict→ROI→tracker.update→sv.Detections
         BotSort(medium) ReID 자동 다운로드 확인 (osnet_x0_25_msmt17.pt, RTX 4070)
- [x] 3-4. backend/detector.py — ONNX export + resolve_model_selection() 멀티 백엔드
- [x] 3-5. backend/model_setup.py — write_profile에 tracker_tier/inference_backend 저장
- [x] 3-6. backend/main.py — /runtime-config tracker 정보 + ITS API TTLCache(5분)

## Phase 4 — UI 개선 ✅ 완료
- [x] 4-1. frontend/MapView.jsx — 📷 TextLayer로 카메라 아이콘 교체 (선택 시 cyan 강조)
- [x] 4-2. frontend/MapView.jsx — PolygonLayer로 시야 범위 삼각형 표시 (70° FOV, 90m)
         투명 ScatterplotLayer로 클릭 히트영역 유지
- [x] 4-3. backend/main.py — /cctvs 응답에 heading(기본 0°), fov_deg(70) 필드 추가
- [x] 4-4. frontend/App.jsx — Legend에 시야범위 항목 추가

## 버그 수정 (테스트 중 발견)
- [x] ITS API 단건 응답 dict 처리 (3곳: /cctvs, /cctv-refresh, hls_refresh_loop)
- [x] boxmot 트래커 동시성 버그: ws/detect 활성 시 live_loop track() 스킵
- [x] ws/detect 종료 시 boxmot reset_tracker() 호출 (live_loop 재개 준비)
- [x] Auto-ROI 자동 적용 제거 (카메라 전환 시) — 수동 저장 ROI만 적용
- [x] roi_config.json auto-generated 항목 정리
- [x] CalibBody.camera_key → cctvurl 수신 후 서버에서 camera_key 계산
- [x] handleCctvClick calMode dependency array 추가
- [x] 보정 탭 비디오 display:none 수정 (cal 탭도 영상 표시)
- [x] CalibrationMode canvas 레이아웃 RoiEditor 패턴으로 재작성
- [x] tasks/lessons.md 생성

## Phase 5 — 방향 보정 ✅ 완료
- [x] 5-1. frontend/CalibrationMode.jsx — canvas 오버레이, 픽셀 클릭 → 지도 GPS 클릭 4쌍 수집
- [x] 5-2. frontend/CctvPlayer.jsx — 🔧 보정 탭 추가 (pendingGps/onNeedGps/onCancelGps props)
- [x] 5-3. frontend/App.jsx — calMode 상태머신 + handleMapClick + 보정 안내 배너
- [x] 5-4. frontend/MapView.jsx — calibrationMode prop, 보정 중 CCTV 클릭 비활성화, cursor:crosshair
- [x] 5-5. backend/main.py — /calibration GET/POST/DELETE 엔드포인트
- [x] 5-6. backend/transform.py — update_from_calibration() homography 재계산 + calibration_data.json 저장
