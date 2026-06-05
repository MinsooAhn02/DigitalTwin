# report/img — figure images + how to finish the report

## 1) Drop the four screenshots here (exact filenames)

Until a file exists, the report compiles with a labelled placeholder box in its
place, so you can build anytime and add images later.

| Filename | Figure | Used in section |
|----------|--------|-----------------|
| `cctv_map.png`       | CCTV cameras on the map (status-coloured icons + FOV) | III. System Design (`fig:map`) |
| `yolo_detect.png`    | YOLO detection + tracking overlay                     | IV. Implementation – Detection (`fig:yolo`) |
| `auto_calib.png`     | Automatic lane-based calibration result               | IV. Implementation – Localization (`fig:calib`) |
| `congestion_map.png` | Congestion: CCTV icons recoloured by status + region  | IV. Implementation – Congestion (`fig:congestion`) |

- PNG recommended (JPG/PDF also fine — then change the extension in the `.tex`
  `\figorbox{...}` calls). Width ~1200 px; scaled to one column.
- Same filenames appear in both the base and contribution PDFs.

## 2) Get the Evaluation numbers (automatic — no manual logging)

The numbers are produced by the running app; you do **not** type them in.

1. `cd traffic-digital-twin && make dev`
2. In the browser, click a CCTV on the map and **watch it on the Live tab for
   1–2 minutes** (the Live tab is what records per-stage latency; the YOLO tab
   records tracking/speed/detection only).
3. The server auto-writes these files to `traffic-digital-twin/backend/` and
   refreshes them every 30 s:
   - `eval_summary.json`  ← main file; also contains a ready-to-paste Markdown
     table under the `"markdown"` field
   - `eval_latency.csv`, `eval_tracking.csv`, `eval_speed.csv`, `eval_detections.csv`
   - Instant snapshot any time: open `http://localhost:8000/eval/report`
   - Start a fresh run: `POST http://localhost:8000/eval/reset`

## 3) Fill the tables in the report

Copy the values from `eval_summary.json` (or the CSVs) into:
- **Table I** (latency / throughput) ← `eval_latency.csv` + `throughput_fps`
- **Table II** (tracking + speed distribution) ← `eval_tracking.csv` / `eval_speed.csv`
- **Table III** (measured vs. ITS speed) ← the live broadcast fields
  `our_avg_kph` / `its_speed_kph` / `speed_error_pct` and `backend/speed_scale.json`
- The "Setup" line in Section V (model / backend / tracker / GPU / camera / N frames)

## 4) Rebuild the PDFs

```
cd traffic-digital-twin
make report          # builds report_base.pdf and report_contribution.pdf
```
(or upload the `report/` folder to Overleaf and compile each `.tex`).

> Checklist: ① put 4 images here → ② `make dev`, watch Live tab 1–2 min →
> ③ read `backend/eval_summary.json` → ④ type those numbers into the tables →
> ⑤ `make report`.

---

## 5) 스크린샷 촬영 상세 가이드

### 공통 준비
- `make dev` 실행 후 `http://localhost:5173` 열기
- 브라우저 줌 100%, 창 충분히 크게 (1600px 이상 권장)
- 스크린샷: **Win+Shift+S** → 영역 캡처 → PNG로 저장

---

### `cctv_map.png` — 지도 전체 뷰
**목표**: 상태별 색상 CCTV 아이콘 + 선택 카메라의 FOV 폴리곤 + 차량 마커

1. 지도에서 교통량이 보이는 카메라 클릭 (고속도로 구간 추천)
2. 사이드바에 속도 수치가 표시될 때까지 대기 (캘리브레이션 완료 신호)
3. 우측 패널에서 Background monitoring에 추가 카메라 2–3개 등록
   - 색상 다양성: 정상(초록), 바쁨(노랑), 혼잡(빨강)이 섞이면 좋음
4. 지도 줌: FOV 폴리곤 + 주변 아이콘 여러 개가 한 화면에 들어오게 조정
5. 차량 마커(점)가 도로 위에 보이는 순간 캡처
6. 캡처 범위: **지도 영역 전체** (사이드바 제외해도 무방)

---

### `yolo_detect.png` — YOLO 검출 오버레이
**목표**: 바운딩 박스 + 차종 레이블 + 트랙 ID가 동시에 보이는 프레임

1. CctvPlayer(우측 하단 플로팅 패널)에서 **YOLO 탭** 클릭
2. annotated MJPEG 스트림이 시작될 때까지 2–3초 대기
3. 차량 3대 이상 + 박스·클래스(car/truck/bus)·숫자 ID가 보이는 순간 캡처
4. 캡처 범위: CctvPlayer 비디오 영역 (플레이어 UI 포함해도 무방)

---

### `auto_calib.png` — 자동 캘리브레이션 결과
**목표**: FOV 폴리곤이 실제 도로 위에 올라가 있는 모습

1. 캘리브레이션이 되지 않은 카메라 선택
   (또는 기존 카메라에서 `DELETE /calibration` 호출해 초기화)
2. Live 탭으로 이동 → 카메라 전환 후 약 5–10초 대기 (자동 캘리브 5회 시도)
3. 사이드바 "Auto Calibration Estimate" 카드에 값이 나타나면 완료
4. 지도에서 **FOV 폴리곤이 도로와 겹쳐 보이는 순간** 캡처
   - 보조: `http://localhost:8000/video-stream` 을 브라우저에서 열면
     차선 검출 결과가 오버레이된 원본 프레임 확인 가능
5. 캡처 범위: 지도 전체 또는 FOV + 도로가 잘 보이는 영역

---

### `congestion_map.png` — 혼잡 클러스터 오버레이
**목표**: 상태별 색 아이콘 + 혼잡 구역 음영 폴리곤

1. Background monitoring에 카메라 4–6개 등록
2. 차량 7대 이상인 카메라가 포함되면 congested(빨강) 상태 발생
3. **30초 대기** (history_sampler_loop 주기) → 지도에 혼잡 폴리곤 등장
4. 폴리곤 색상(minor=노랑, medium=주황, severe=빨강)이 보이는 순간 캡처
5. 캡처 범위: 혼잡 폴리곤 + 주변 아이콘 여러 개가 한 화면에 들어오는 지도 영역

---

### 공통 주의사항
| 항목 | 기준 |
|---|---|
| 해상도 | 최소 1200 px 너비 (PNG 권장) |
| 개인정보 | 차량 번호판이 선명하면 흐리게 처리 |
| 파일명 | 정확히 위 표의 파일명 사용 (대소문자 일치) |
| 저장 위치 | 이 폴더 (`report/img/`) |
| 포맷 변경 시 | `.tex` 내 `\figorbox{파일명}` 확장자도 함께 수정 |
