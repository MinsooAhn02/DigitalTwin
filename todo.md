# Todo — Image-only metric calibration & speed accuracy
> Plan 원본: `C:\Users\dksal\.claude\plans\toasty-wishing-backus.md`

---

## 🟢 진행 현황 (2026-06-30 기준)

| Step | 내용 | 상태 | 검증 |
|------|------|------|------|
| 1 | Depth-invariance 진단 메트릭 | ✅ 구현 완료 | ⚠️ 라이브 확인 필요 (아래 참조) |
| 2 | ITS_DRIVES_SCALE=False | ✅ 구현 완료 | ✅ 코드 확인: `analytics.py:911` return None |
| 3 | 차선 표시 감지 (`lane_markings.py`) | ✅ 구현 완료 | ✅ 자체 테스트 PASS (lane_w_obs=2) |
| 4 | Multi-anchor solver + free focal | ✅ 구현 완료 | ✅ ALL PASS (long_err=0.7%, Δf=1.4%) |
| UI | startup·토글·이름·아이콘·클러스터 | ✅ 구현 완료 | - |
| **5** | **Auto-cali 견고성 + 품질 점수** | ⬜ 미시작 | Step 1 라이브 확인 후 시작 |
| **6** | **Digital-twin 실측값 표시** | ⬜ 미시작 | |
| 7 | 문서 (CODE_LOGIC.md 갱신) | ⬜ 미시작 | |

### Step 1 라이브 확인 방법 (사용자 직접)
```
make dev → 카메라 선택 → 1~2분 대기
GET http://localhost:8000/eval/report
확인: depth_invariance.qualifying_tracks > 0
```

### 설계 원칙 확인
- plan의 `calibrate_from_its` docstring `alpha=0.99` → 실제 코드는 `0.15/0.05/0.01` 적응형
- 단, `ITS_DRIVES_SCALE=False`이면 `analytics.py:911`에서 `return None` 즉시 반환 → alpha 무관
- "ITS 없이 CCTV 이미지만으로" 원칙 = Step 2 + Step 4(free-focal)로 구현됨

---

## Plan: Image-only metric calibration & speed accuracy (no ITS/GPS)

## Context

Goal: improve **speed-measurement and calibration accuracy** using only *image-observable*
physics — in-image road geometry (road edges **and lane markings**), NodeLink lane data, camera
position, and vehicle apparent-size-vs-depth. **No ITS segment speed, no GPS** (user: ITS is too
coarse to be a reference; the whole point is a system that works without those). Then refresh
`CODE_LOGIC.md`. Accuracy work first, docs last.

The user's four explicit requirements:
1. Verify the current vehicle-width code **line-by-line** (done below) before changing it.
2. Measure scale from **lane-line / road markings too**, not only vehicle width.
3. Scrutinize the **auto-calibration principle** — if auto-cali is wrong, every measurement is wrong.
4. As a digital twin, pull **real-world data and display it on screen** properly.

---

## Part A — Verified current behavior (read line-by-line)

### A.1 Vehicle apparent-size scale (`transform.py`)
- `accumulate_scale_obs(v, bbox_w_px, class_name, frame_h, frame_w)` (`transform.py:485`):
  looks up `VEHICLE_WIDTHS_M` (`car 1.8, truck 2.5, bus 2.5`), rejects `bbox_w_px < 20`,
  appends `(v, real_w/bbox_w_px)` to `_scale_obs` (deque maxlen 200). So each sample is
  **(bbox-bottom row, lateral meters-per-pixel at that row)**. Fed from `main.py:2223` using the
  bbox bottom-centre.
- `fit_scale_model(min_obs)` (`transform.py:501`): least-squares `1/scale = B·v + C`
  (inverse-mpp linear in row — the correct pinhole form for a ground plane). Validity gate:
  `B>0` and `vp_y = −C/B ∈ (0, 0.7·h)`. Refit every 10 frames (`main.py:2232`), persisted to
  `vehicle_calib.json` with drift warning.
- `_scale_correction_at(v)` (`transform.py:531`): `fitted_scale = 1/(Bv+C)` vs the homography's
  horizontal scale at row v; returns the **ratio, hard-clamped `[0.6, 1.8]`**.
- `speed_correction_at(v_px, frame_h)` (`transform.py:579`): velocity-domain wrapper (resolution
  rescale + per-row cache), wired to `analytics.depth_corr_fn` (`main.py:290`).
- Applied in `_speed` (`analytics.py:398, 451`): `scaled = raw · min(corr·speed_scale, 3.0)`.

**Concrete weaknesses found:**
- **W1 (the big one): vehicle width is a *lateral* anchor; speed needs the *longitudinal*
  (depth) scale.** Converting one to the other needs camera pitch/focal. Today the longitudinal
  scale comes from the homography (built with **fixed focal `1.2·h`**), and vehicle data is only a
  clamped `[0.6,1.8]` nudge — the rich absolute-scale signal is discarded. So the depth scale is
  never truly measured; ITS `speed_scale` patches it. This is the root cause.
- **W2: bbox-width bias.** bbox width equals real width only head-on; under yaw/perspective the
  bbox also captures vehicle length → mpp overestimated on oblique cameras.
- **W3: crude widths.** `bus == truck == 2.5`, no motorcycle; single `car 1.8`.

### A.2 Auto-calibration chain (`transform.auto_calibrate_from_frame`, called `main.py:2065`)
Canny → `HoughLinesP` → keep diagonals (<60° from vertical) → sample road edges at 5 rows, take
**15th/85th percentile x** as left/right edge → lstsq `x=a·y+b` per edge → **VP** = line
intersection (clamped `vp_y ≤ 0.55h`) → direction decision (curvature-match, else VP+map-bearing,
else `vp_x>0.55w`) → **`camera_pose.solve_pose`** (focal fixed; accept if `residual<8px`) →
`pose_to_corners` → homographies. Fallbacks: heuristic trapezoid → saved prior pose → GPS grid.
5 attempts scheduled per switch (`main.py:1988`).

**Weaknesses found (these poison everything downstream if wrong):**
- **W4: fixed focal `fy=h·1.2`** also seeds `pitch0/yaw0` and the heuristic — systematic bias for
  any camera whose true FoV ≠ 45°, which is most of them (user: every CCTV angle/zoom differs).
- **W5: percentile edges are not validated.** 15th/85th picks the outermost diagonals; guardrails,
  shadows, or multi-lane markings can be latched as "road edges". No parallel-in-world check.
- **W6: single quality gate.** Only `residual_px < 8` decides acceptance; a degenerate fit with low
  residual still passes. No independent cross-check (e.g. does a known-length ground feature
  reproject correctly?).
- **W7: `road_width = lanes × 2 × lane_w` ("always ×2")** is the *only* metric anchor for the
  solver. If it's wrong (one-way roads, wrong lane count), the recovered pose scale is wrong.
- **W8: direction decision** is multi-branch; a wrong flip sends vehicles backward and corrupts
  direction + speed sign handling.

### A.3 Display / digital-twin data path
`camera_ready` (`main.py:1997`) sends road_name, lanes, max_spd, bearing, snap, **nominal**
road_width, road_pts, roi_gps_ring. `auto_calibrated` (`main.py:2093`) adds heading + FOV
(`cam_h_m, pitch_deg, yaw_deg, focal_px, near_m, far_m, road_width_m, residual_px`). Frontend
(`MapView.jsx`) draws the FOV polygon (`computeRoadCorridorPolygon`/`computeCalibPolygon`) and
vehicle markers from broadcast lat/lon. **Gap:** the screen shows *nominal* NodeLink values and an
FOV outline, but not the **measured** geometry or any **calibration-quality** signal — so a wrong
auto-cali looks identical to a good one.

---

## Part B — Improvement design: multi-anchor metric calibration

Use **three independent, ground-plane, multi-depth anchors** so depth scale is *measured*, not
patched, and anchors cross-check each other:

1. **Vehicle width (lateral)** — existing `_scale_obs`; keep as fallback/cross-check (W2/W3 caveats).
2. **Lane width (lateral)** — detected lane-marking spacing vs NodeLink lane width (~3.0–3.5 m).
   On-ground, fixed geometry → cleaner than bbox width; cross-checks the "always ×2" road width (W7).
3. **Dashed lane-marking longitudinal period (LONGITUDINAL)** — the missing piece. Korean dashed
   lane markings have standardized paint/gap lengths; consecutive dashes at known image rows give a
   **direct depth anchor** → makes focal/pitch/H fully observable → correct *speed* scale with no ITS.
   If dashes aren't reliably detectable, the system degrades to lateral-only + fixed focal — no
   regression.

### B.1 Verified Korean standards (web-searched — go into `config.py`)
Selected per NodeLink `road_rank` (already available on switch):
- **Dashed centre/lane markings** (경찰청 교통노면표시 설치·관리 매뉴얼):
  - Expressway / motor-vehicle-only (rank 101/102): **paint 8 m, gap 12 m → period 20 m**.
  - Urban / general road: **paint 3 m, gap 5 m → period 8 m**.
  - Line width (W): **0.10–0.20 m** (use 0.15 m nominal).
- **Lane width** (도로의 구조·시설 기준에 관한 규칙): national road standard **3.5 m**; design
  speed 80 → **3.25 m**, 60 → **3.0 m** — matches NodeLink `lane_w` mapping in `nodelink.py`, so
  the lane-width anchor and NodeLink agree by construction and cross-check the "×2" road width.
- These are **soft priors** with a tolerance band, not hard equalities — the solver fits to the
  *detected* marking period; the standard seeds the expected value and rejects detections that are
  implausibly far from it (robustness, not over-constraint).
Constants: `MARK_PERIOD_M`, `MARK_PAINT_M`, `MARK_GAP_M` (per rank), `MARK_WIDTH_M`,
`MARK_PERIOD_TOL`. Sources cited in the commit/CODE_LOGIC.

**Core change:** extend `camera_pose.solve_pose` to take optional anchor observations
`[(row_v, mpp_lateral)]` and `[(row_v_a, row_v_b, real_long_m)]`, add reprojection residuals for
them (reuse `_project`/`_backproject`/`_road_to_world`), and **free `focal` as a 5th variable**
only when anchors are sufficient and span enough depth (`FOCAL_FREE_*` gates). The recovered focal
flows through `pose_to_corners` → `H_meter` unchanged downstream. With focal correct, the depth
correction `κ` → ~1.0 and the homography itself carries the right longitudinal scale.

**Demote ITS to display-only:** gate `analytics.calibrate_from_its` behind `ITS_DRIVES_SCALE`
(**default False**); `speed_scale` stays 1.0; keep `_inject_its_speed` comparison fields purely
informational. Optional ITS-free residual: a conservative same-track depth-invariance nudge.

---

## Part C — Auto-cali robustness (so the foundation is trustworthy)

- **Add lane-marking detection** alongside edge detection to (a) disambiguate true road edges from
  guardrails/shadows (W5) and (b) supply the lane-width + dashed-period anchors (Part B).
- **Add a calibration quality score** beyond `residual_px` (W6): independent-anchor reprojection
  error + same-track speed depth-invariance (a correctly-scaled camera measures one car's speed
  ~constant across depth). Reject/flag low-quality solves; require plausible `H` and `pitch` ranges.
- **Cross-check road width** (W7): if detected lane width disagrees with NodeLink ×2 beyond a
  tolerance, prefer the measured width and log it.
- Keep the existing fallback ladder; the new anchors strengthen the *primary* path, not replace
  the graceful-degradation chain (no regression when detection is weak — night/rain/dense traffic).

---

## Part D — Digital-twin real-data display

- Add **measured-vs-nominal** fields to the `auto_calibrated`/`camera_ready` payloads:
  recovered `cam_h_m / pitch_deg / focal_px`, **measured road width**, **calibration quality score**,
  and the **depth-invariance metric**. Render them in the existing *Auto Calibration Estimate* card
  (`App.jsx` `CollapsibleCard`) so a bad calibration is visible, not hidden.
- Optional **anchor overlay** on the calibration/YOLO view (`CctvPlayer.jsx`): draw the detected
  road edges, lane lines, and dashes the solver used, so the geometry is visually verifiable.
- Ensure corrected scale flows to vehicle GPS so markers sit on real lanes (already via `H_gps`;
  verify after the focal change).

---

## Steps (each is one self-contained, independently pushable commit)

Workflow: I implement **one step at a time and stop** so you can review, commit, and push it
before the next. Each step is ordered so the tree stays working (no half-broken states).

- **Step 1 — Diagnostics (no behavior change).** ✅ 완료. Add the self-consistency metrics so we can
  measure improvement without ground truth: single-track **depth-invariance** (CV of one track's
  pre-scale speed across `bbox_bottom_y` bins) + **anchor reprojection residual**.
  *Files:* `metrics.py` (`LiveMetrics` accumulators + `report()` fields, CSV), hook in
  `main.py:_live_process` / `analytics._speed` to emit per-track raw-speed+row. Safe to push.
  ⚠️ 라이브 검증 필요: `GET /eval/report` → `qualifying_tracks > 0`

- **Step 2 — Stop ITS driving speed (isolated, reversible).** ✅ 완료. Add `ITS_DRIVES_SCALE` (default
  **False**) in `config.py`; gate `analytics.calibrate_from_its` and `main.py:_update_its_speed`
  save so `speed_scale` stays 1.0; keep `_inject_its_speed` fields as display-only. Push.

- **Step 3 — Lane-marking detection module.** ✅ 완료. New `lane_markings.py` (or function in
  `transform.py`) detecting lane lines → lateral lane-width obs + longitudinal dashed-period obs;
  verified constants (B.1) in `config.py`, selected by NodeLink `road_rank`. Wire detections into
  diagnostics/logging only (no solver change yet) so it's observable and safe to push.
  자체 테스트 PASS: `lane_w_obs=2, dash_obs=0` (합성 프레임 정상)

- **Step 4 — Multi-anchor solver + free focal (the core accuracy change).** ✅ 완료. `camera_pose.py`:
  extend `solve_pose`/`_residuals`/`_initial_opt` to take vehicle-width + lane-width + dash-period
  anchors and free `focal` when anchors span enough depth (`FOCAL_FREE_MIN_OBS`,
  `FOCAL_FREE_MIN_ROW_FRAC`); extend `_self_test` with a longitudinal-anchor trial that recovers
  focal. `transform.auto_calibrate_from_frame` feeds the anchors. Push.
  자체 테스트 ALL PASS: `focal-recovery long_err=0.7%, Δf=1.4%`

- **Step 5 — Auto-cali robustness + quality score.** ⬜ 미시작. Edge/marking disambiguation, plausibility
  bounds on `H`/`pitch`, and a calibration **quality score** (anchor reprojection + depth-invariance)
  beyond `residual_px`; cross-check measured vs NodeLink road width. Push.

- **Step 6 — Digital-twin display.** ⬜ 미시작. Add measured `cam_h_m/pitch_deg/focal_px`, measured road
  width, and quality score to `camera_ready`/`auto_calibrated`; render in the *Auto Calibration
  Estimate* card (`App.jsx`); optional anchor overlay in `CctvPlayer.jsx`. Push.

- **Step 7 — Docs.** ⬜ 미시작. Refresh `CODE_LOGIC.md` (multi-anchor calibration, lane-marking module, ITS
  demotion, diagnostics, display fields) and **fix existing drift**: `calibrate_from_its` docstring
  says `alpha=0.99` (real: adaptive `0.15/0.05/0.01`, 단 ITS_DRIVES_SCALE=False라 실행 안 됨);
  §8 says eval → `backend/eval_*.csv` but code writes `backend/logs/` (`metrics.LOGS_DIR`);
  undocumented `min(corr·speed_scale, 3.0)` cap and `_corr_y_ema` (0.7/0.3) y-smoothing in `_speed`. Push.

## Reuse (don't reinvent)
- `camera_pose._project/_backproject/_road_to_world/_boundary_curve` for anchor reprojection.
- `transform._scale_obs`, `VEHICLE_WIDTHS_M`, `_apply_homography_corners` (shared cali tail).
- `metrics.LiveMetrics` + `stats()`; `camera_pose._self_test` synthetic round-trip.
- Existing Hough/edge pipeline in `auto_calibrate_from_frame` (extend, don't replace).
- `App.jsx` `CollapsibleCard` for the new measured-data card.

## Verification (no ground truth)
1. **Geometry self-test:** `python camera_pose.py` — ✅ ALL PASS (focal-recovery trial PASS)
2. **Live self-consistency:** `make dev`, watch 3–4 cameras of differing angle/zoom; via
   `GET /eval/report` confirm depth-invariance CV ↓ and anchor reprojection residual ↓ vs a
   baseline capture (before/after). ⚠️ 미확인
3. **Visual:** anchor overlay matches real lanes; vehicle markers sit on the road; FOV follows the
   curve; measured road width ≈ visual reality.
4. **Regression guard:** cameras with weak detection keep fixed focal + fallback ladder unchanged.
5. **ITS-independence:** with `ITS_DRIVES_SCALE=False`, `speed_scale` stays 1.0; speeds plausible
   against the YOLO overlay (the user's stated eval method). ✅ 코드 확인 완료
