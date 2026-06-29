# Todo — Image-only metric calibration & speed accuracy
> Plan 원본: `C:\Users\dksal\.claude\plans\toasty-wishing-backus.md`
> 현재 플랜: `C:\Users\dksal\.claude\plans\user-answered-claude-s-replicated-scone.md`

---

## 🟢 진행 현황 (2026-06-30 기준)

| Step | 내용 | 상태 | 검증 |
|------|------|------|------|
| 1 | Depth-invariance 진단 메트릭 | ✅ 구현 완료 | ✅ 라이브 확인: 달래내1 `qualifying_tracks=9, median_cv=0.046` |
| 2 | ITS_DRIVES_SCALE=False | ✅ 구현 완료 | ✅ 코드 확인: `analytics.py:911` return None |
| 3 | 차선 표시 감지 (`lane_markings.py`) | ✅ 구현 완료 | ✅ 자체 테스트 PASS (lane_w_obs=2) |
| 4 | Multi-anchor solver + free focal | ✅ 구현 완료 | ✅ ALL PASS (long_err=0.7%, Δf=1.4%) |
| UI | startup·토글·이름·아이콘·클러스터 | ✅ 구현 완료 | - |
| **W** | **Warm-up → commit → lock 재설계** | 🔵 진행 중 | |
| &nbsp;&nbsp;W1 | clean-plate 누적기 + 단일이미지 detect/solve 헬퍼 | ⬜ | |
| &nbsp;&nbsp;W2 | warm-up 상태머신 + lock + refit 동결 + persistence | ⬜ | |
| &nbsp;&nbsp;W3 | 프론트엔드: 보정중 배지 + 재보정 버튼 + 실측 표시 | ⬜ | |
| 6 | Digital-twin 실측값 표시 (W3에 통합) | → W3 | |
| 7 | 문서 (CODE_LOGIC.md 갱신) | ⬜ 미시작 | W 완료 후 |

### 라이브 검증 결과 (경부선 상적교, 2026-06-30)
```
qualifying_tracks=32, median_cv=0.124
anchor_residual_px.n=0  ← dash_obs=0: 단일프레임 autocorr 미통과 → free-focal 미작동
speed_scale_snapshot={}  ← ITS 제거 성공
median speed=145.8 km/h  🚨 과추정 (제한속도 100~110 km/h)
```

### 근본 원인 & 재설계 요약
- **단일 프레임** 캘리브레이션: 5번 시도 중 첫 residual<8px 프레임이 lock → 노이즈/차량에 매우 취약
- **dash_obs=0** 불변: 단일 노이즈 프레임에서 점선 자기상관 peak<0.15 → focal 고정 → 종방향 scale 틀림
- **해결**: 카메라 ON → warm-up(관측 누적) → `np.median` clean-plate → 한 번 solve → lock
  - clean-plate = 정적 차선 페인트 복원, 차량/노이즈 제거 → `dash_obs>0` → free-focal 작동
  - lock 후 vehicle-scale refit도 동결; 수동 재보정 버튼으로만 재시작
- **상세**: `C:\Users\dksal\.claude\plans\user-answered-claude-s-replicated-scone.md`

### 설계 원칙 확인
- plan의 `calibrate_from_its` docstring `alpha=0.99` → 실제 코드는 `0.15/0.05/0.01` 적응형
- 단, `ITS_DRIVES_SCALE=False`이면 `analytics.py:911`에서 `return None` 즉시 반환 → alpha 무관
- `_is_calibrated`는 4-point 수동 캘리브 전용 → `locked`는 별도 플래그로 추가
- "ITS 없이 CCTV 이미지만으로" 원칙 = Step 2 + Step 4(free-focal) + Step W로 완성

---

## Full Plan (최신 — 2026-06-30 재설계)

> 이전 plan: `toasty-wishing-backus.md` (Step 1–4 완료 후 종료)
> 현재 plan: `user-answered-claude-s-replicated-scone.md`

### 배경 & 근본 원인

Goal: **ITS/GPS 없이 CCTV 이미지만으로** 속도·거리 정확도 확보.  
Step 1–4 완료 후 경부선 라이브 검증 결과 **median speed 145.8 km/h (제한 100~110 km/h)**  
→ root cause 확인:

| 문제 | 원인 코드 | 결과 |
|---|---|---|
| 단일 프레임 캘리브 | `main.py:2095` 5-attempt loop (1프레임 = 1시도) | 노이즈/차량에 취약 |
| `dash_obs=0` 불변 | `lane_markings._autocorr_period` peak<0.15 (1프레임 노이즈) | free-focal 미작동 |
| focal 고정 `1.2·h` | `transform.py:751` | 종방향 scale 틀림 → 속도 과추정 |
| vehicle-scale 무한 refit | `main.py:2268` 10프레임마다 재피팅, lock 없음 | 드리프트 지속 |

**해결책:** warm-up(관측 누적) → clean-plate(차량 제거 중앙값 프레임) → 한 번 solve → lock

### 설계 결정 (확정)

| 항목 | 결정 |
|---|---|
| commit 트리거 | data-driven (dash_obs 충분) + timeout(`WARMUP_MAX_S`) fallback |
| warm-up UI | 잠정값(fixed-focal) 표시 + `보정 중 (N s)` 배지; commit 시 정확값으로 교체 |
| lock 정책 | 한 번 lock → 재보정은 수동 버튼(`POST /recalibrate`)으로만 |
| vehicle-scale | lock 시 함께 동결 (commit 시점에 최종 1회 fit) |

---

### Part A — 완료된 기반 (Step 1–4)

**A.1 진단 메트릭 (Step 1 ✅)**
- `metrics.LiveMetrics`: `depth_invariance` (단일 트랙 CV), `anchor_residual_px`
- `GET /eval/report` → live 측정 가능

**A.2 ITS 의존 제거 (Step 2 ✅)**
- `ITS_DRIVES_SCALE=False` (`config.py`) → `analytics.py:911` return None
- `speed_scale` 항상 1.0 유지

**A.3 차선 감지 모듈 (Step 3 ✅)**
- `lane_markings.py`: `detect_lane_markings()` → `lane_width_obs`, `dash_period_obs`
- 한국 도로 표준 상수: 고속도로 period=20m, 일반 8m (`config.py`)
- 현재 단일 프레임에서 `dash_obs=0` → clean-plate로 해결 예정

**A.4 Multi-anchor solver + free focal (Step 4 ✅)**
- `camera_pose.solve_pose()`: `lane_w_obs`, `dash_obs` 앵커 지원
- `FOCAL_FREE_MIN_OBS=3`, `FOCAL_FREE_MIN_ROW_FRAC=0.20` gate
- `dash_obs≥3` + row span 충분 시 focal 5번째 변수로 해방
- self-test ALL PASS: `long_err=0.7%, Δf=1.4%`

**A.5 코드 핵심 위치 (리팩터 시 재사용)**
- `transform.py:485` `accumulate_scale_obs` / `501` `fit_scale_model`
- `transform.py:733` `auto_calibrate_from_frame` (detect+solve 분리 예정)
- `transform.py:76` `_is_calibrated` = 4-point 수동 전용 (`locked`와 별개)
- `camera_pose.py:324` `solve_pose` / `407` fixed-focal fallback (내장)
- `main.py:2095` 5-attempt loop → warm-up 상태머신으로 교체
- `main.py:2268` vehicle-scale refit → `not _transformer.locked` 게이트 추가
- `main.py:140/153` `_load/_save_camera_pose` / `112/126` `_load/_save_vehicle_calib`
- `App.jsx:701` `CollapsibleCard` (auto-calib 카드 확장)

---

### Part B — 현재 구현 대상: Warm-up → commit → lock

#### B.1 핵심 아이디어: temporal clean-plate

```
warm-up 동안 N프레임 샘플 (≈1~2/s, 최대 60장)
→ np.median(stack, axis=0) → vehicle-free clean-plate
→ detect_lane_markings(clean_plate) → dash_obs > 0 (정적 차선만 남음)
→ solve_pose(free-focal) → 정확한 focal 복원
→ lock
```

단일 프레임 autocorr가 실패하는 이유: 차량/압축 노이즈가 점선 신호를 묻음.  
clean-plate는 차량(transient)을 제거하고 차선 페인트(static)만 남김 → peak>0.15 통과.

#### B.2 카메라 라이프사이클

```
switch_camera
  └─ 저장 pose 있음? ──yes──→ apply prior + locked=True (warm-up 스킵)
  └─ 없음 ──────────────────→ WARMUP 진입
        └─ 매 프레임: clean-plate 스택 누적 + scale_obs 누적
        └─ WARMUP_EVAL_EVERY 프레임마다: commit check
        └─ 조건 통과 OR timeout → COMMIT
              └─ clean-plate → detect → solve_pose
              └─ quality pass → 적용 + persist + locked=True
              └─ quality fail → fixed-focal fallback + locked=True
        └─ 이후 LOCKED: refit 없음, 재시도 없음
        └─ 수동 재보정: /recalibrate → locked=False + 저장 pose 삭제 → WARMUP
```

#### B.3 commit 게이트 (data-driven + timeout)

**Early commit** (모두 충족 시):
- `dash_obs ≥ DASH_MIN_OBS` (=3) AND row span ≥ `FOCAL_FREE_MIN_ROW_FRAC`
- `solve_pose` residual < `POSE_RESIDUAL_MAX_PX`

**Timeout commit** (`WARMUP_MAX_S` 초 경과):
- 앵커 부족 → fixed focal fallback (현재 동작과 동일) + locked
- 기존 fallback ladder 유지 (prior pose → GPS grid)

#### B.4 설계 주의사항 (코드 더블체크 결과)

- `locked` = **새 플래그** (`_is_calibrated`는 4-point 수동 전용, 재사용 금지)
- warm-up은 `not _is_calibrated` 조건 하에서만 (`main.py:2095` guard 유지)
- `solve_pose` 내부에 fixed-focal fallback 이미 있음 (`camera_pose.py:407`) → timeout 경로 자연스럽게 처리됨

---

### Part C — 구현 순서 (각각 독립 커밋)

**W1 — clean-plate 누적기 + 단일이미지 헬퍼** (backend only)
- `transform.py`: `auto_calibrate_from_frame`의 detect+solve 꼬리를 `_calibrate_from_image(img, …)` 헬퍼로 분리
- `LiveTransformer`에 warm-up 상태 추가: `_warmup_stack: list[np.ndarray]`, `_warmup_t0`, `_warmup_locked`
- `feed_warmup_frame(frame)`: 1/2 해상도 ROI 그레이스케일 → ring buffer (maxlen=`CLEANPLATE_MAX_FRAMES`)
- `commit_calibration(…)`: `np.median(stack)` → `_calibrate_from_image` → 성공/timeout 분기
- `config.py`: `WARMUP_MAX_S=90`, `WARMUP_EVAL_EVERY=30`, `CLEANPLATE_MAX_FRAMES=60`, `DASH_MIN_OBS=3`
- 검증: `python camera_pose.py` ALL PASS 유지 + unit test로 합성 점선 clean-plate → `dash_obs>0` 확인

**W2 — warm-up 상태머신 + lock + 저장** (`main.py`)
- `_auto_calib_attempts` loop (`2095-2138`) → warm-up 상태머신으로 교체
- vehicle-scale refit (`2268`) → `not _transformer.locked` gate 추가
- commit 시: `fit_scale_model` + `_save_vehicle_calib` + `_save_camera_pose`
- WS broadcast: warm-up 중 `{type:"calibrating", elapsed_s:N}` 매 30프레임 전송
- `POST /recalibrate` endpoint: `_transformer.recalibrate()` + 저장 pose 삭제
- 검증: `make dev` → `보정 중 (N s)` 로그 확인, commit 후 lock, 재보정 버튼 API 작동

**W3 — 프론트엔드** (`App.jsx`, `useWebSocket.js`, `i18n/index.jsx`)
- `calibrating` WS 메시지 → `보정 중 (N s)` 배지 (속도 숫자 위에 반투명 오버레이, 기존 provisional 값은 유지)
- `auto_calibrated` 수신 시 배지 제거
- **재보정 버튼** → `POST /recalibrate` → warm-up 재시작
- `CollapsibleCard` (Auto Calibration) 확장: `cam_h_m`, `pitch_deg`, `focal_px`, `residual_px`, `quality_score`
- i18n 키: `calib.warming`, `calib.recalibrate`, `calib.quality`

**W4 — docs** (`CODE_LOGIC.md`)
- warm-up/lock 아키텍처, clean-plate 설명
- 기존 오류 수정: `calibrate_from_its` docstring alpha=0.99 (실제 0.15/0.05/0.01); eval CSV 경로

---

### Part D — 검증 체크리스트

1. **Self-test**: `python camera_pose.py` → ALL PASS (focal-recovery 유지)
2. **Unit**: clean-plate에서 `dash_obs>0` + `focal_px ≠ h*1.2` 확인
3. **Live (경부선)**: `make dev` → 배지 표시 → commit 후 `/eval/report` → `anchor_residual_px.n>0`, **median speed ↓ 100~110 km/h**
4. **Lock**: commit 후 pose/scale 변화 없음, `/recalibrate` 후 warm-up 재시작
5. **Regression**: 점선 미감지 카메라 → timeout + fixed-focal lock, 크래시 없음

---

### Reuse 목록

| 재사용 항목 | 위치 |
|---|---|
| `solve_pose` focal-free path | `camera_pose.py:324` |
| `_load/_save_camera_pose` | `main.py:140,153` |
| `_load/_save_vehicle_calib` | `main.py:112,126` |
| `metrics.LiveMetrics` depth-invariance | `metrics.py` |
| `_apply_homography_corners` | `transform.py` |
| `CollapsibleCard` | `App.jsx:799` |
| Hough/Canny/VP body | `auto_calibrate_from_frame` (헬퍼로 추출) |
