# Traffic Digital Twin — Code Logic Reference

> Single source of truth for the report. Every mechanism below is documented from the
> actual source (`traffic-digital-twin/backend/*.py`, `traffic-digital-twin/frontend/src/**`).
> File/symbol references use the form `file.py:symbol`. Constants and formulas are the real
> ones used in code (`config.py` unless noted).

---

## 1. System Overview

The system turns a live Korean ITS CCTV feed into an interactive, on-map **digital twin** of road
traffic. For a camera the user clicks, it:

1. streams the camera's HLS video,
2. detects vehicles (**YOLO26**, NMS-free end-to-end; YOLOv8 available as fallback) and tracks them across frames (BoxMOT multi-object tracker),
3. converts each vehicle's pixel position to a GPS coordinate via a homography that is either
   manually calibrated, **solved from a road-model camera pose** (lane edges + NodeLink road
   width/bearing → camera height/pitch/yaw via least squares), or approximated from the road network,
4. computes per-vehicle speed, travel direction (In/Out), and flags (speeding, bottleneck, parked),
5. aggregates frame-level metrics (vehicle count, average speed, Level-of-Service grade), and
6. broadcasts everything over a WebSocket to a React/deck.gl front-end that draws vehicles, trails,
   and a field-of-view (FOV) polygon on a vector map.

On top of the single live camera, the backend also runs **multi-camera background monitoring**,
**camera-level congestion clustering**, and a **SQLite time-series history** with charting and CSV
export. A self-calibrating loop compares measured speed against the ITS official segment speed and
learns a per-camera correction factor (`speed_scale`).

---

## 2. Architecture & Data Flow

Two inference paths can produce `FrameAnalytics`. They **share one `VehicleDetector`** and therefore
must never call `track()` concurrently — a counter (`_detect_clients` in `main.py`) coordinates them.

### 2.1 Browser path — `WS /ws/detect`
```
<video> (hls.js, /hls-proxy)
   └─ canvas capture: JPEG ≤640px, every ~33 ms, maxInFlight=2  (CctvPlayer.jsx)
        └─ WS /ws/detect  ──JPEG bytes──▶  main.py:_yolo_detect_annotate
                 ├─ cv2.imdecode → detector.track(frame)        (YOLO + BoxMOT)
                 ├─ VehicleTracker.update → LineZone in/out
                 ├─ _build_vehicles → batch homography → VehicleState[]
                 ├─ analytics.update → FrameAnalytics
                 ├─ _broadcast(...) to all /ws clients   ◀── sidebar + map update
                 └─ annotated JPEG ──▶ back to that browser  (boxes overlay)
```

### 2.2 Server path — `live_loop` (server-side HLS)
`main.py:live_loop` opens the HLS stream directly with OpenCV/FFmpeg and runs the same
detect→track→transform→analytics→broadcast pipeline through `_live_process`. It is **gated** twice:

- If `_detect_clients > 0` (a browser YOLO tab is active) it only *drains* frames and skips `track()`,
  so the two paths never corrupt the shared tracker state.
- If not `(_current_cam and _live_viewer_active and _clients)` it sleeps — no GPU work when nobody is
  watching. The front-end reports visibility via `POST /viewer-active`.

It also feeds the two MJPEG endpoints (`/video-stream`, `/video-stream-yolo`) and the auto-calibration
attempts on camera switch.

### 2.3 Background loops (started in `main.py:lifespan`)
| Loop | Period | Job |
|------|--------|-----|
| `live_loop` | per-frame (target `1/FPS`) | server-side detect + broadcast, camera-switch handling, auto-calibration |
| `hls_refresh_loop` | `HLS_REFRESH_INTERVAL` = 30 min | refresh HLS URL before token expiry (`_refresh_hls_url_from_its`) |
| `_its_speed_poll_loop` | `ITS_POLL_INTERVAL` = 5 min | fetch ITS segment speed → `analytics.calibrate_from_its` → save `speed_scale` |
| `history_sampler_loop` | `HISTORY_SAMPLE_S` = 30 s | snapshot bg+live cameras to SQLite, recompute congestion clusters, hourly prune |

---

## 3. Technology Stack

| Layer | Technology |
|-------|------------|
| Web server | FastAPI + Uvicorn (async REST + WebSocket) |
| Detection | ultralytics **YOLO26** (default, NMS-free end-to-end) / YOLOv8 (legacy fallback); classes 2/3/5/7 = car/motorcycle/bus/truck; `YOLO_CONF=0.30`, `YOLO_IOU=0.45` (ignored by YOLO26) |
| Inference backend | **TensorRT FP16 `.engine` → ONNX Runtime → PyTorch** (auto-selected, `detector.py:resolve_model_selection`) |
| Calibration | Road-model camera-pose solver — `scipy.optimize.least_squares` → planar homography (`camera_pose.py`) |
| Tracking | **BoxMOT**: ByteTrack / OcSort / BotSort / DeepOcSort (tier-selected) |
| Detection utils | `supervision` (Detections, LineZone, Box/Label annotators) |
| Video | OpenCV + FFmpeg (HLS), numpy homography |
| HTTP / cache | httpx (async ITS calls), cachetools (5-min CCTV cache) |
| Storage | sqlite3 (history WAL DB, NodeLink R*tree DB) |
| Frontend | React 18 + Vite |
| Map | deck.gl (Scatterplot/Text/Polygon/Icon) + react-map-gl + MapLibre GL |
| Basemaps | Carto Dark Matter / Positron, Esri World Imagery (satellite) |
| Video playback | hls.js |
| Charts | Recharts |
| External data | ITS OpenAPI `cctvInfo` + `trafficInfo` (국도 `ITS_API_KEY` / 고속도로 `EX_API_KEY` 분리, `asyncio.gather` 병렬 조회); MOCT NodeLink shapefile → SQLite |

---

## 4. Backend Modules

### 4.1 `detector.py` — detection + tracking

**Model selection (`resolve_model_selection`).** Walks candidate **family×variant** stems
(`{yolo26,yolov8}{x,l,m,s,n}`, family from `YOLO_MODEL_FAMILY` — default `yolo26` — or the configured
`YOLO_MODEL`) and picks, in priority order: an existing `.engine` (TensorRT, if CUDA + tensorrt
present), else export `.pt→.engine` on the fly (`YOLO_AUTO_EXPORT_ENGINE`), else `.onnx` (ONNX
Runtime), else `.pt` (PyTorch). FP16 (`half=True`) is used only for the PyTorch path on CUDA; TensorRT
engines are already FP16. **YOLO26 is end-to-end (NMS-free)** so the engine graph is simpler/faster, but
it still goes through the *same* `.pt→.engine` export and falls back to `.pt` if the engine build fails.
`model_setup.py`'s picker groups models by a **family tab** (YOLO26 default / YOLOv8), shows that
family's n/s/m/x variants, and writes the chosen stem (e.g. `yolo26m.engine`) to `.yolo_model` plus the
family into `.runtime_profile.json`.

**Tracker tiers.** `TRACKER_TIER` (`auto` by default). `_auto_tracker_tier` picks by VRAM:
≥10 GB → `high`, ≥6 GB → `medium`, ≥3.5 GB → `low`, else `cpu`.

| Tier | Tracker | ReID weights | Notes |
|------|---------|--------------|-------|
| cpu | ByteTrack | none | fastest, no GPU; patched (see below) |
| low | OcSort | none | `min_hits=1, max_age=BYTE_TRACK_BUFFER`; strong occlusion handling |
| medium | BotSort | `osnet_x0_25_msmt17.pt` | appearance ReID; `cmc_method="sof"` (optical flow) to avoid ECC failures on low-texture night frames |
| high | DeepOcSort | `osnet_x1_0_msmt17.pt` | appearance ReID, 8 GB+ |

**Lock architecture.**
Two locks guard `VehicleDetector` state:

| Lock | Scope | Notes |
|------|-------|-------|
| `_lock` | `model.predict()` only | GPU inference serialization; held only inside `track()` |
| `_state_lock` | all mutable tracker state (`_last_tracks`, `_last_dets_np`, `_track_frame_count`, `_yolo_miss_streak`, `_tracker.update()`, `_id_stabilizer`) | Wraps the **entire** `track()` body and the **entire** `reset_tracker()` body — prevents data race between the thread-pool thread running `track()` and the thread-pool thread running `reset_tracker()` |

Nesting is always `_state_lock` (outer) → `_lock` (inner, for `model.predict()` only). `detect()` uses only `_lock` and never `_state_lock`, so there is no circular dependency and no deadlock risk.

All `reset_tracker()` call sites in async functions use `await asyncio.to_thread(det.reset_tracker)` so that waiting on `_state_lock` happens on a thread-pool thread and never blocks the event loop (which could stall for ~100–300 ms while a GPU inference holds `_state_lock`).

**`track(frame)` pipeline.**
1. `should_detect = (frame_count-1) % detect_interval == 0`. `_detect_interval` is forced to **1** on
   TensorRT/ONNX (inference is cheap); only CPU honours `YOLO_DETECT_INTERVAL`.
2. On detect frames: YOLO `predict` → `sv.Detections` → ROI mask (`_apply_roi` via `sv.PolygonZone`).
3. **Empty-detection grace:** if YOLO returns nothing and the empty streak ≤ `_YOLO_MISS_GRACE` (**3**, raised from 2 for moving-vehicle occlusion tolerance),
   it returns the *last* tracks and skips `tracker.update`. Calling `update(empty)` would make
   ByteTrack/OcSort mark all tracks LOST and re-issue new raw IDs on the next real detection →
   IDStabilizer mismatch → duplicate IDs. Preserving tracker state lets the same raw ID re-match.
4. On non-detect frames: re-feed the **last** detection array (never empty) so IoU matching keeps IDs
   and the Kalman filter advances.
5. For `cpu`/`low` tiers (no ReID): `_dedup_tracks` then `IDStabilizer.update`.

**`_dedup_tracks`.** Removes duplicate tracks where IoU > 0.3 **or** centre distance < 40 px, keeping
the lower (older) ID. Handles ByteTrack emitting two IDs for one car.

**`IDStabilizer`** (only for cpu/low, which lack appearance ReID). Restores a vehicle's previous ID
after a brief miss using **velocity-predicted position nearest-neighbour matching** (≤ **80 px** from
predicted centre; class default is 120 px, but the `VehicleDetector` instantiates with `max_dist_px=80.0`). Each active track's pixel velocity `(vx, vy)` is tracked per frame; when a track
goes lost its velocity is saved in `_lost` as `[cx, cy, age, vx, vy]`. `_find_lost` predicts
`(cx + vx·age, cy + vy·age)` so fast-moving vehicles (30+ px/frame) remain matchable across 3–4 lost
frames. Two-pass design:
- **Pass 1:** raw IDs already in `_remap` reclaim their stable ID first (prevents a new track from
  stealing it via `_find_lost`).
- **Pass 2:** unmatched tracks match against `_lost`; on match it **purges all stale `_remap`
  entries** pointing at that stable ID (otherwise dozens of old raw→stable mappings accumulate and the
  tracker reusing those raw IDs collapses them into one display row).
- Tracks that disappear within 50 px of a frame edge are evicted immediately (they left the scene, so
  their ID must not be handed to a newly entering vehicle).
- Display IDs are renumbered 1,2,3… (`_display_map`) so the UI never shows ByteTrack's 200+ counters.
- `match_thresh` for ByteTrack: **0.35** (lowered from 0.5) — fast vehicles have IoU 0.3–0.4 between
  frames at 30 fps, which previously caused premature track splits.

**ByteTrack patch.** boxmot 12.x sets `STrack.is_activated` only when `frame_id == 1`; with
`DETECT_INTERVAL > 1` new tracks were never returned. `_patched_activate` forces `is_activated = True`.

**`VideoStream`.** OpenCV `CAP_FFMPEG`. Sets `CAP_PROP_BUFFERSIZE=1` to keep the read buffer minimal.
`open_video_source` validates connectivity beyond `isOpened()`: `isOpened()` only confirms the m3u8
manifest opened — if the HLS segment is 404 or token-expired, `read()` silently fails every frame.
After opening, the function loops `read()` for up to 3 seconds; if no frame arrives it releases the
capture and raises `RuntimeError("Stream opened but no frames decoded")`
(`detector.py:open_video_source`). It exposes `pos_msec` (stream PTS) for an accurate speed time axis
(§4.10). `reconnect()` runs `open_video_source` via `await asyncio.to_thread(open_video_source, url)`
so the 3-second validation block does not stall the event loop
(`detector.py:VideoStream.reconnect`).

### 4.2 `tracker.py` — In/Out counting

`VehicleTracker` lazily builds a horizontal `sv.LineZone` at `y = h/2` on the first frame. Each
`update` returns the detections plus cumulative `in_count`/`out_count` **and** the sets of
`track_id`s that crossed in/out *this frame*. Those crossed-ID sets drive per-vehicle direction in
analytics (a vehicle stays "In" or "Out" once it crosses).

### 4.3 `transform.py` — pixel → GPS / metre

Maintains two homographies: `_H_gps` (pixel→lat/lon) and `_H_meter` (pixel→local ENU metres, used for
speed). `update_from_calibration` rebuilds both from the 4 GPS corners projected to a local ENU frame
so distances are internally consistent.

**Manual 4-point calibration (`update_from_calibration`).** `cv2.findHomography` on 4 (pixel, GPS)
pairs. Sets `is_calibrated = True` — the only path that does. Speed accuracy depends on this.

**Curved 2-stage GPS mapping (`_pixel_to_gps_curved`).** When a road centreline is set
(`set_road_corridor(road_pts, snap_along_m)`) and the camera is *not* manually calibrated, pixels map
along the real road curve instead of a flat plane:
1. pixel → `(x_m, y_m)` via `_H_meter`.
2. ENU displacement from the snap point → decompose into `d_along` (road direction) and `d_lateral`.
3. `target_arc = snap_along_m + curve_dir_sign · d_along`; interpolate the centreline (`_road_interp`)
   to get the on-road GPS at that arc length.
4. Re-apply `d_lateral` perpendicular to the **local** road bearing at that arc.

`curve_dir_sign` (and `_curvature_flip_candidate`) decide whether the camera looks in the F→T or T→F
direction by matching the **image** road-curve sign (`_image_curve_sign`, from the detected lane
centres) against the **map** curve sign for each candidate direction.

**Warm-up / commit-once / lock calibration pipeline (replaces single-frame 5-attempt loop).** When a
camera has no saved pose, `LiveTransformer` enters a **warm-up** accumulation phase instead of
attempting to calibrate from one noisy frame.

Lifecycle:
1. **Camera switch** — if `camera_pose.json` has a saved pose for `camera_key`, `load_pose_params` is
   called immediately and `_locked = True` is set; no warm-up. Otherwise `start_warmup(...)` is called
   and `_warmup_active = True`.
2. **Warm-up** — each processed frame calls `feed_warmup_frame(frame)`. The method samples frames at
   `CLEANPLATE_SAMPLE_S = 0.5 s` intervals, stores a **½-resolution grayscale ROI** in `_warmup_stack`
   (cap `CLEANPLATE_MAX_FRAMES = 60`). Every `WARMUP_EVAL_EVERY = 30` frames it checks if data quality
   is sufficient for an early commit. If `WARMUP_MAX_S = 90 s` elapses with no early commit, a timeout
   commit is forced. During warm-up, `main.py` broadcasts `{type: "calibrating", elapsed_s}` every
   `WARMUP_EVAL_EVERY` frames so the frontend shows a `보정 중 (Ns)` badge.
3. **Clean plate.** `_build_clean_plate()` computes `np.median(stack, axis=0).astype(np.uint8)` over the
   collected frames. Because vehicles and compression artefacts are transient, the median removes them and
   reveals **static lane paint** — producing a vehicle-free image where dashed-line autocorrelation
   reliably clears the `peak_val ≥ 0.15` gate (`lane_markings.py`), giving `dash_obs ≥ DASH_MIN_OBS = 3`
   and enabling the focal-free solve (see `camera_pose.py`). This is the root fix for
   `dash_obs = 0` + median speed overestimation on single-frame calibration.
4. **Commit** — `commit_calibration(frame_shape)` upscales the clean plate back to full resolution, calls
   `auto_calibrate_from_frame` on it (the same solve as before), and on success sets `_locked = True`.
   The solved pose is saved to `camera_pose.json`; the final `fit_scale_model` is run once and saved.
   `main.py` broadcasts `auto_calibrated` (with `warmup_elapsed_s`, `focal_px`, `residual_px`).
5. **Locked** — `_transformer.locked` is `True`. Vehicle-scale refit (`fit_scale_model`) is **gated** by
   `not _transformer.locked`, so the scale never drifts after commit. Pose also never re-solves.
6. **Re-calibrate** — `POST /recalibrate` calls `_atomic_delete_json(CAMERA_POSE_PATH, cam_key)`,
   `_transformer.recalibrate()` (clears `_locked`, `_warmup_active`, `_warmup_stack`), then
   `start_warmup(...)`. The frontend's Re-calibrate button triggers this endpoint.

**`_locked` vs `_is_calibrated`.** `_is_calibrated` is set only by the 4-point **manual** calibration
(`update_from_calibration`). `_locked` is the warm-up system's flag. The warm-up runs only when
`not _is_calibrated`, so manual calibration always takes priority and is never overwritten by warm-up.

**`auto_calibrate_from_frame`.** Estimates a homography from lane geometry. Steps:
1. Gray → Gaussian blur → Canny; keep only the lower 55 % (road ROI).
2. `HoughLinesP`; keep diagonals (< 60° from vertical).
3. Sample road edges at 5 vertical levels; per level take the 15th/85th percentile x as left/right.
4. Least-squares fit `x = a·y + b` for each edge.
5. **Vanishing point** = intersection of the two fitted lines (with sane bounds; parallel-line
   fallback uses the median of pairwise Hough intersections).
6. **Direction decision:** curvature match if available, else compare the VP horizontal angle
   `φ = atan2(vp_x − w/2, fy)` against the camera→snap bearing (flip 180° if the reverse candidate is
   closer), else fall back to `vp_x > 0.55·w`. Skipped entirely when `fix_direction=True`.
7. **Road-model pose solver (primary, `camera_pose.solve_pose`).** Feeds multi-level lane edges + VP +
   dashed-line observations (`dash_obs`, `dash_period_px` from `lane_markings.detect_lane_markings`) +
   NodeLink `road_width_m` to the pose solver. On success with `residual_px < POSE_RESIDUAL_MAX_PX`
   (8 px) it builds the 4 image↔GPS corners → `_apply_homography_corners` and returns
   (`method="pose"`, carrying `cam_h_m/pitch_deg/yaw_deg/focal_px/near_m/far_m/residual_px`).
8. **Heuristic trapezoid fallback** (only when the pose solve fails). The legacy estimate: pitch
   `= atan2(h/2 − vp_y, fy)`, `fy = h·1.2`; `d_near = road_width_m·fy/road_px_w`;
   `cam_h = d_near·tan(pitch+vfov/2)` (3–40 m); trapezoid `src_pts` → GPS corners → homographies.

Either path leaves `is_calibrated` **False**. `_apply_homography_corners` builds `_H_gps`/`_H_meter`.

**Road-model camera-pose solver (`camera_pose.py`).** A pinhole-camera pose fit with optional
focal-length recovery.
- **Model.** World frame is nadir-aligned ENU at the snap point (camera at `(0,0,H)`, ground `Z=0`).
  Camera rotation is pitch-only; the road carries `yaw` and lateral offset `x0`. `Pose` dataclass:
  `H_m, pitch_deg, yaw_deg, focal_px, x0_m`.
- **Fixed-focal solve (`solve_pose`, default path).** `scipy.optimize.least_squares` (soft-L1)
  minimises, over 5 sampled rows, the reprojection residual of the projected left/right road boundaries
  (`±road_width_m/2`) against detected lane edges, plus a vanishing-point residual. Focal fixed at
  `FOCAL_RATIO·h`. `θ = (H, pitch, yaw, x0)`.
- **Focal-free solve (when `dash_obs ≥ FOCAL_FREE_MIN_DASH_OBS`).** If dashed-line observations from
  `lane_markings.detect_lane_markings` are available and pass the `FOCAL_FREE_MIN_ROW_FRAC` gate, focal
  is added as a 5th free parameter. The dashed-line period (in pixels) anchors the **longitudinal**
  scale that road-width alone cannot observe — fixing the root cause of speed overestimation. On the
  clean plate this gate reliably fires; on a single live frame it rarely does.
- **Output (`pose_to_corners`).** Projects 4 road-frame corners to image and GPS; shared tail builds
  both homographies.
- **Persistence & cold-start.** `get_pose_params`/`load_pose_params` serialise the 5-field `Pose` to
  `camera_pose.json` per `camera_key`. `apply_prior_pose` uses the saved pose when edges are too weak.
  `rough_pose_from_vehicles` is a last-resort cold-start. A `__main__` synthetic self-test validates.
- **Vehicle scale model gated on lock.** `fit_scale_model` (linear `1/scale = B·v + C` from bbox
  widths, persisted to `vehicle_calib.json`) runs adaptively during warm-up but is **frozen on lock**
  (`not _transformer.locked` guard in `main.py`). Minimum-obs threshold is adaptive
  (`SCALE_MIN_OBS` 12 → `SCALE_MIN_OBS_SPARSE` 8 after `SCALE_SPARSE_AFTER_FRAMES`).

### 4.3b `lane_markings.py` — dashed lane marking detection

`detect_lane_markings(frame, roi_top_frac, road_rank)` extracts dashed-line observations used by the
focal-free pose solver.

1. Convert to grayscale, apply Gaussian blur, crop to the road ROI (`roi_top_frac..h`).
2. For each of `N_STRIPS = 5` horizontal strips: compute a column-intensity profile; subtract a 1-D
   Gaussian baseline; run 1-D autocorrelation; find the dominant peak at lags `LAG_MIN..LAG_MAX`; if
   `peak_val ≥ 0.15`, record the period (`period_px`) and the strip's vertical centre pixel.
3. Fit a linear model `period_px = A·y_px + B` (period grows with depth) to passing strips; the slope
   encodes the dashed-line perspective foreshortening and is what `solve_pose` uses.
4. Returns `LaneMarkingResult(dash_obs, dash_period_px, dash_period_slope, lane_w_obs, ...)`; `dash_obs`
   is the number of strips that cleared the autocorrelation gate. On a **clean plate** (median of 30–60
   frames) this reliably returns `dash_obs ≥ 3`; on a single compressed highway frame it typically
   returns 0 (motivating the warm-up accumulator in §4.3).

**Fallback grid (`update_gps_center`).** With no calibration at all, builds a trapezoidal homography
(top edge 25–75 % of width, near 15 m / far 80 m / half-width 25 m) rotated to the road bearing. Also
sets `is_calibrated = False`.

### 4.4 `analytics.py` — metrics engine

`VehicleState` (per vehicle): `track_id, class_name, bbox_xyxy, center_px, lat, lon, x_m, y_m,
direction, speed_kph, is_speeding, speed_reliable, dwell_frames, is_bottleneck, is_parked, lane_id`.
`speed_reliable` (default `True`) is cleared when the vehicle's GPS distance from the camera snap point exceeds `SPEED_TRUST_MAX_DEPTH_M`. `lane_id` defaults to -1; reserved for future lane assignment.
`FrameAnalytics` (per frame): `frame_id, timestamp_ms, vehicles[], vehicle_count, avg_speed_kph,
los_grade, in_count, out_count, class_counts`.

**Speed pipeline (`_speed`, the most-tuned logic).** Per track:
1. **Duplicate skip** — identical metre coordinates (non-detect frames) are not appended.
2. **Physics jump guard** — `corr = depth_corr_fn(bbox_bottom_y, frame_h)` is evaluated first (1.0 if no scale model yet); then `raw_max_mps = (MAX_REASONABLE_KPH/3.6) / max(speed_scale · corr, 0.1)` (accounts for both accumulated scale and depth correction so the guard threshold is consistent with the final scaled speed), then two-tier check:
   - `step_m > raw_max_mps·dt·3.0` → **teleport reset**: window cleared entirely (ID-switch artifact; the
     regression slope would otherwise be corrupted by a position jump across IDs).
   - `step_m > raw_max_mps·dt·1.5` → sample dropped, window kept (transient detection noise; an earlier
     version cleared the whole window and produced 0 speed ~47 % of the time).
   - `dt > 2 s` clears the window (track re-appeared).
3. **OLS regression** — over a sliding window of `SPEED_WINDOW_FRAMES` `(x_m, y_m, t)` samples,
   fit velocity by least squares: `kph = hypot(vx, vy)·3.6`. Window displacement
   `< SPEED_JITTER_THRESHOLD_M = 0.5 m` ⇒ speed 0 (jitter). The window is now defined in **seconds**
   (`SPEED_WINDOW_FRAMES = round(SPEED_WINDOW_S·FPS)`, `SPEED_WINDOW_S = 0.7 s`) so the measurement
   *time* stays constant when a different model/profile changes the FPS.
4. **Per-track EMA** (`_speed_ema`, α = `SPEED_EMA_ALPHA` = 0.35). Spike reject: if a confirmed EMA
   (> 5) and `scaled > ema·2.5 + 20`, ignore the sample. The EMA is **never seeded at 0** (a 0 seed
   makes spike-reject block all real speeds → stuck at 0). Stop decay: when stopped, decay ×0.6 and
   floor to 0 below `SPEED_MIN_KPH = 5`.
5. **Depth correction + reliability flag** — `κ = corr` (computed at step 2's start) is applied: `scaled = raw * κ * speed_scale`. `depth_corr_fn` is wired from `main.py` at singleton init (`analytics.depth_corr_fn = _transformer.speed_correction_at`); `frame_h` is updated per-frame (`analytics.frame_h = fh`). The function wraps `_scale_correction_at` (linear fit of inverse apparent-width vs vertical pixel row; clamped [0.3, 3.0]; cached by rounded pixel row). Applied in velocity domain (not position domain) to avoid the frame-to-frame position-jump bug that disabled the previous position-domain attempt.
   `v.speed_reliable` is also computed at step start (`analytics._speed()`): GPS equirect distance from the camera snap point — `hypot((v.lat - cam_lat) * 110574, (v.lon - cam_lon) * 111320 * cos(cam_lat_rad))` > `SPEED_TRUST_MAX_DEPTH_M = 100 m` → `speed_reliable = False`. The speed is still computed and displayed but the vehicle is excluded from `avg_speed_kph`, the `_speed_samples` ITS calibration input, and `is_speeding` is forced False. `FrameAnalytics.avg_speed_kph` is computed exclusively from `reliable_vehicles` (GPS ≤ 100 m from snap).
6. **Scale + flag** — `speed_kph = round(raw · κ · speed_scale, 1)`; `is_speeding = speed_reliable and speed_kph > limit * 1.10` (10 % tolerance).
   `MAX_REASONABLE_KPH = 180` rejects only ID-swap/homography blow-ups (so legit highway speed passes
   and feeds the ITS calibration).

**Cross-vehicle outlier rejection (`_reject_speed_outliers`, used by `_avg_speed`).** The frame average is computed only over `speed_reliable` vehicles (GPS distance ≤ 100 m from camera); within that set, samples with `|x − median| > SPEED_OUTLIER_MAD_K·1.4826·MAD` (K = 3) are dropped (needs ≥ 3 vehicles; otherwise kept). A single ID-swap/homography spike or far-field noise vehicle no longer pulls the frame average — which matters because that average also feeds the `speed_scale` statistics, so ITS-less roads get self-consistency checking without an external reference.

`MAX_REASONABLE_KPH = 180` and `speed_limit_kph` come from the NodeLink `max_spd` on camera switch
(else `SPEED_LIMIT_KPH = 120`).

**GC grace.** A track is kept for `GC_GRACE_FRAMES = 30` missing frames before its history is dropped,
preserving continuity across brief misses.

**Road-axis projection (`_project_to_road_axis`).** Projects each vehicle's GPS onto the road-bearing
axis about a fixed reference (the camera snap point), removing lateral jitter so markers sit on the
road centreline.

**Dwell / bottleneck / parked.** `dwell_frames` counts consecutive zero-speed frames →
`is_bottleneck` at `BOTTLENECK_DWELL_FRAMES = 150` (~5 s) → `is_parked` at
`PARKED_FRAMES_THRESHOLD = 300` (~10 s). Parked pixel positions are remembered in a `deque(maxlen=200)`;
any vehicle within `PARKED_POSITION_RADIUS_PX = 30 px` of a known parked spot is flagged parked
regardless of track_id (survives ID cycling). Parked vehicles are excluded from counts/LOS/average.

**LOS grade (`_los`).** `LOS_THRESHOLDS = {A≤3, B≤6, C≤9, D≤12, E≤15}`, else F, on the active
(non-parked) vehicle count.

**ITS self-calibration (`calibrate_from_its`).** Compares a 10-min rolling average of measured speed (from `speed_reliable` vehicles only) against the ITS segment speed:
- needs ≥ 50 samples in the 600 s window; skip if average < 3 kph.
- **Volatility guard:** coefficient of variation > 0.4 ⇒ skip (traffic in transition).
- `target = old_scale · ITS / our_avg`, **clamped to [0.3, 5.0]** (a clamp hit logs a warning —
  surfaces a badly-off homography instead of failing silently).
- **Adaptive learning rate** with per-update ±10% step clamp:
  - Camera whose `speed_scale` was **restored from `speed_scale.json`** (`its_scale_restored=True`): α fixed at 0.01 (preserve accumulated calibration).
  - **New camera** (no saved factor): α = 0.15 on the 1st–2nd update, 0.05 on the 3rd–4th, then 0.01 — fast initial convergence, dampened steady-state.
  - Step clamp: `new_scale = clamp(blended, old * 0.9, old * 1.1)` — a single corrupt ITS sample cannot shift the scale more than 10% in one step.
  - `_its_calib_runs` counter increments per update, reset to 0 on `analytics.reset()` (camera switch).
  The 10-min window is double the ITS 5-min aggregation so the ITS window is always contained regardless of poll phase.

**Direction classification (`_assign_directions`, `_project_to_road_axis`).** Per vehicle, a signed
along-axis delta (EMA over frames, deadzone `DIR_DEADZONE_M = 0.10 m`) determines direction:
`delta > deadzone` → **Out** (moving in bearing direction), `delta < -deadzone` → **In** (approaching
camera, against bearing). EMA coefficient `DIR_EMA_ALPHA = 0.4`. Falls back to the last LineZone crossing
when `road_bearing_deg` is not set.

**Road-shape learning.** Two mechanisms refine the road geometry from observed vehicle GPS:
- `_accumulate_gps_trace`: collects GPS positions of all non-parked vehicles (up to 1 000 entries) for road centreline refinement.
- `refine_road_pts`: bins accumulated GPS traces along the bearing axis into `ROAD_PTS_REFINE_NBINS = 10`
  bins, averages each bin, and returns a refined road polyline plus a new `snap_along_m`. Requires
  ≥ `ROAD_PTS_REFINE_MIN_SAMPLES = 50` points. **Note**: as of Phase 2 this refinement is explicitly
  disabled in `live_loop` (`new_road = None`) to preserve the NodeLink centreline shape.

**Bearing auto-refinement (`refine_bearing`).** Accumulates per-frame vehicle flow vectors (x_m, y_m
deltas) using double-angle statistics (`_flow_sin2`, `_flow_cos2`) to estimate the road axis free of
180° ambiguity. After ≥ `BEARING_REFINE_MIN_SAMPLES = 30` samples, the estimated axis is blended into
`road_bearing_deg` with `BEARING_REFINE_EMA_ALPHA = 0.15`. Called from `live_loop` every
`BEARING_REFINE_INTERVAL_FRAMES = 30` frames; a broadcast is sent only when the change exceeds
`BEARING_BROADCAST_MIN_DEG = 1.5°`.

**Speed debug logging.** `set_speed_debug(on)` / `speed_debug_status()` / `_spd_debug()` form a
per-frame diagnostic subsystem (off by default). Enable by creating `backend/speed_debug.on` or setting
`SPEED_DEBUG=1`; disable by deleting the file. Output goes to `backend/speed_debug.log`. Each track
emits one line per frame (throttled to 0.2 s) showing decision code, dt, step, span, raw/scaled speed.

### 4.5 `nodelink.py` — national road network

Queries the MOCT NodeLink SQLite DB (R*tree spatial index) built once by
`scripts/build_nodelink_db.py`.

- `get_links_near` — bbox query, ranks links by perpendicular distance to the F→T segment.
- `_best_link` — prefers a link whose `road_name` matches the CCTV-name hint
  (`_road_name_matches`, digit-aware so "국도 1호선" == "국도1호선"); otherwise re-ranks links within a
  distance tolerance by road rank (101 = motorway first) then length (longer first), avoiding short
  low-rank intersection connectors.
- `_snap_to_polyline` — perpendicular projection of the camera onto the road polyline → snap point,
  local tangent bearing, segment index.
- `_road_corridor_pts` — extracts ± 150 m of centreline around the snap, returning `road_pts` (F→T)
  and `snap_along_m`.
- `_extend_pts_with_adjacent` — links end at intersections, so near a boundary the corridor would be
  too short and the FOV polygon nearly square. This stitches one adjacent link at each end (matched by
  shared node + same road name + bearing ± 60°).
- **Bidirectional centre fix** — NodeLink stores each direction as a separate one-way link, so the
  snap lands on one carriageway's centre. `_find_reverse_link` finds the opposite carriageway (same
  name, bearing ≈ +180° ± 60°, using the link's overall F→T bearing — local segment bearing differs
  40–60° on curves), `_snap_for_link` snaps it, and if the two snaps are 2–40 m apart their midpoint
  becomes the true road centre; `road_pts` is shifted by the same lateral delta.
- **Road width** — `lanes × 2 × lane_w` with `lane_w` = 3.5 m (rank 101/102/103), 3.25 m (104/105),
  else 3.0 m. The "× 2" (always assume bidirectional) is a deliberate approximation.

`get_road_snap` returns `snap_lat/lon, bearing_deg, road_name, lanes, max_spd, road_rank,
road_width_m, is_oneway, cam_dist_m, road_pts, snap_along_m`.

### 4.6 `congestion.py` — camera-level clustering
Background cameras have only a vehicle *count* and a status, not per-vehicle GPS, so congestion is
clustered at the **camera** level. `_cluster_points` is a greedy DBSCAN (haversine distance, `eps`
= `CONGESTION_EPS_M` = 500 m, `min_samples` = 1, BFS connected components) over busy/congested
cameras. Polygon: Andrew monotone-chain convex hull for ≥ 3 cameras, else a 120 m circle. Severity
(`_severity`): **severe** if ≥ 2 congested or total > 6·members; **medium** if any congested/busy;
else **minor**.

### 4.7 `history.py` — SQLite time-series
WAL-mode SQLite, single connection + lock (called via `asyncio.to_thread`). One `snapshots` table
(`ts, cam_key, name, name_ko, lat, lon, source ['bg'|'live'], vehicle_count, class_counts JSON,
status, avg_speed_kph`) with an `(cam_key, ts)` index. `record_many` batches one sampler tick.
`series` buckets by `CAST(ts/bucket)·bucket` returning per-bucket average + peak vehicle count and
average speed. `peak` returns the max-count timestamp. `export_rows` feeds CSV. `prune` deletes rows
older than `retention_cutoff(HISTORY_RETENTION_DAYS = 14)`.

### 4.8 `roi_manager.py`, `config.py`, `utils.py`
- `roi_manager` — ROI polygons stored as **normalized** [0,1] coordinates (resolution-independent),
  keyed by `camera_key = md5(url)[:12]`; `roi_to_pixels` converts for `sv.PolygonZone`.
  `save_roi()` wraps the entire read-modify-write under a module-level `_write_lock` (TOCTOU prevention).
- `config.py` — all constants. Key groups:
  - YOLO: `YOLO_MODEL_FAMILY`, `YOLO_CONF=0.30`, `YOLO_IOU=0.45`, `YOLO_DETECT_INTERVAL`
  - Tracker: `TRACKER_TIER`, `BYTE_TRACK_FPS=30`, `BYTE_TRACK_BUFFER=30`
  - Speed: `SPEED_WINDOW_S=0.7s` → `SPEED_WINDOW_FRAMES`, `SPEED_EMA_ALPHA=0.35`, `SPEED_SPIKE_FACTOR=2.5`, `SPEED_STOP_SPAN_S=1.0` (imported in analytics but stop-decay uses fixed 0.6 multiplier), `SPEED_MIN_KPH=5`, `MAX_REASONABLE_KPH=180`, `SPEED_JITTER_THRESHOLD_M=0.5`, `SPEED_OUTLIER_MAD_K=3.0`, `SPEED_TRUST_MAX_DEPTH_M=100` (GPS distance cutoff; vehicles beyond this are `speed_reliable=False`)
  - Pose/scale: `POSE_RESIDUAL_MAX_PX=8.0`, `SCALE_MIN_OBS=12`, `SCALE_MIN_OBS_SPARSE=8`, `SCALE_SPARSE_AFTER_FRAMES=600`
  - Warm-up / clean-plate: `WARMUP_MAX_S=90.0`, `WARMUP_EVAL_EVERY=30`, `CLEANPLATE_MAX_FRAMES=60`, `CLEANPLATE_SAMPLE_S=0.5`, `DASH_MIN_OBS=3`, `LANE_MIN_OBS=2`
  - Focal-free solve: `FOCAL_FREE_MIN_DASH_OBS` (min strips for free-focal), `FOCAL_FREE_MIN_ROW_FRAC` (min depth span)
  - Direction: `DIR_DEADZONE_M=0.10`, `DIR_EMA_ALPHA=0.4`
  - Bearing refinement: `BEARING_REFINE_MIN_SAMPLES=30`, `BEARING_REFINE_EMA_ALPHA=0.15`, `BEARING_REFINE_INTERVAL_FRAMES=30`, `BEARING_BROADCAST_MIN_DEG=1.5`
  - Road-shape learning: `ROAD_PTS_REFINE_MIN_SAMPLES=50`, `ROAD_PTS_REFINE_NBINS=10`
  - Position smoothing: `POS_EMA_ALPHA=0.4`, `POS_JUMP_RESET_M=8.0`
  - FOV polygon: `FAR_CAP_M=120.0` (ROI projection max), `FOV_EMA_MIN_SAMPLES=60`, `FOV_EMA_ALPHA=0.05`
  - Lane offset: `LANE_OFFSET_M=1.75` (In/Out perpendicular separation)
  - LOS: `LOS_THRESHOLDS {A≤3, B≤6, C≤9, D≤12, E≤15}`
  - Dwell: `BOTTLENECK_DWELL_FRAMES=150`, `PARKED_FRAMES_THRESHOLD=300`, `PARKED_POSITION_RADIUS_PX=30`
  - History: `HISTORY_SAMPLE_S=30`, `HISTORY_RETENTION_DAYS=14`, `CONGESTION_EPS_M=500`
  - Loops: `ITS_POLL_INTERVAL=300s`, `HLS_REFRESH_INTERVAL=1800s`
  Runtime profile (`.runtime_profile.json`, with `family`) overrides capture/FPS/JPEG.
- `utils.py` — `haversine_m` geodesic distance.

### 4.9 `main.py` — server, endpoints, orchestration

**Concurrency primitives in `main.py`.**

| Object | Type | Purpose |
|--------|------|---------|
| `_json_file_lock` | `threading.Lock` | Serializes all JSON config read-modify-write operations across the three helpers below, preventing TOCTOU across concurrent async handlers |
| `_frame_count_lock` | `threading.Lock` | Makes `_frame_count += 1` atomic in `_yolo_detect_annotate` and `_live_process` (both run in thread-pool threads) |
| `_atomic_update_json(path, key, value)` | helper | Acquires `_json_file_lock`, reads, updates one key, writes back atomically — used by `_save_vehicle_calib`, `_save_speed_scale`, `save_calibration` endpoint |
| `_atomic_delete_json(path, key)` | helper | Same lock, removes one key — used by `delete_calibration` and `delete_roi` endpoints |

`_save_camera_pose` performs its own read-modify-write directly under `_json_file_lock` (not via the helpers, since it merges multiple sub-keys).

`_set_viewer_active` is `async def` (converted from `def`) so it can `await asyncio.to_thread(det.reset_tracker)` without blocking the event loop. `stop_camera` and the `viewer_active` endpoint both call it with `await`.

**REST endpoints.**
| Method · path | Purpose |
|---|---|
| `GET /cctvs` | ITS CCTV list for the viewport bbox (5-min `TTLCache`); `_fetch_its_cctvs` issues parallel (`asyncio.gather`) queries for national roads (`ITS_API_KEY`, `type="its"`) and expressways (`EX_API_KEY`, `type="ex"`), merges results, adds EN names + dedup numbering |
| `POST /switch-camera` | switch the live camera (see below) |
| `GET /cctv-refresh` | fresh HLS URL after token expiry (browser) |
| `GET /hls-proxy` | CORS proxy that rewrites m3u8 segment URLs and streams .ts |
| `GET /video-stream`, `/video-stream-yolo` | MJPEG of raw / annotated live frames |
| `GET /nodelink/nodes` | nearby road nodes for calibration GPS snapping |
| `GET/POST/DELETE /roi`, `/calibration` | ROI and 4-point calibration CRUD |
| `POST /background/add`, `/background/remove/{key}`, `GET /background/status` | multi-camera monitoring |
| `GET /history/cameras`, `/history/series`, `/history/peak`, `/history/export.csv` | history analytics |
| `POST /viewer-active` | report tab visibility (pauses live GPU work) |
| `POST /recalibrate` | delete saved pose for current camera, clear `_locked`, restart warm-up |
| `POST /stop-camera`, `GET /health`, `/runtime-config`, `/speed-debug/{state}` | control/diagnostics |

**WebSockets.** `/ws` (broadcast sink) and `/ws/detect` (browser JPEG → annotate → analytics).
Messages on `/ws`:
| Type | Trigger |
|------|---------|
| `camera_ready` | camera switch complete (road_name, bearing, snap, road_width, road_pts, roi_gps_ring, calibrated) |
| `calibrating` | warm-up in progress (`elapsed_s`, `stack_frames`); sent every `WARMUP_EVAL_EVERY` frames |
| `auto_calibrated` | warm-up commit succeeded or bearing changed (heading, near_m, far_m, road_width_m, focal_px, residual_px, warmup_elapsed_s, roi_gps_ring) |
| `camera_error` | stream open failed |
| `background_status` | background camera status change |
| `congestion_clusters` | cluster recompute after history tick |
| `roi_updated` | ROI polygon changed (roi_gps_ring) |
| (default) | `FrameAnalytics` JSON per frame |

**`switch_camera`.** Bumps `_cam_version` (so `/ws/detect` resets its tracker), resets analytics,
restores the saved per-camera `speed_scale` (`_load_speed_scale` returns `(scale, found)`;
`found=True` sets `analytics.its_scale_restored=True` to keep α=0.01 for already-converged cameras),
the vehicle scale model (`vehicle_calib.json`) **and the road-model pose prior** (`_load_camera_pose`
→ `load_pose_params`, which seeds the next solve), resets
the BoxMOT tracker, kicks an async ITS speed fetch, queries NodeLink (`get_road_info` +
`get_road_snap`), sets `speed_limit_kph` and the effective bearing (priority **name_bearing ?? snap
bearing ?? link bearing**), stores `_current_cam`, and queues the stream switch for `live_loop`.

**`live_loop` camera-switch block.** Switches the OpenCV stream, sets the road corridor
(`set_road_corridor`), restores saved ROI, manual calibration, and the scale model. If a saved pose
exists (`_load_camera_pose`), it is applied via `load_pose_params` and `_locked = True` is set
immediately — no warm-up. Otherwise `start_warmup(...)` is called. Computes road width, broadcasts
`camera_ready`.

**`live_loop` warm-up state machine.** While `_warmup_active` and `not _is_calibrated`:
- Each frame calls `_transformer.feed_warmup_frame(frame)` which samples frames into `_warmup_stack`.
- Every `WARMUP_EVAL_EVERY` frames `main.py` broadcasts `{type: "calibrating", elapsed_s}`.
- When `feed_warmup_frame` signals ready (quality gate or timeout), `commit_calibration(frame.shape)`
  is called: builds the median clean plate, runs `auto_calibrate_from_frame`, saves the pose and final
  scale fit, sets `_locked = True`, and broadcasts `auto_calibrated`.
- Vehicle-scale refit (`fit_scale_model`) is gated by `not _transformer.locked` so it stops after commit.

**`live_loop` bearing auto-refinement.** Every `BEARING_REFINE_INTERVAL_FRAMES = 30` frames, calls
`analytics.refine_bearing()`. If the refined bearing differs from the last broadcast by ≥
`BEARING_BROADCAST_MIN_DEG = 1.5°`, an `auto_calibrated` message is sent with the new heading. Road-pts
refinement (`refine_road_pts`) is intentionally **not applied** (Phase 2 decision: `new_road = None`) to
preserve the NodeLink centreline shape over the bearing-binned polyline approximation.

**Camera-pose / scale persistence.** `camera_pose.json` and `vehicle_calib.json` are keyed by
`camera_key`; the per-frame scale refit (`_live_process`) uses the adaptive `min_obs` and `_save_*`
writes update them so each session improves on the last. `_scale_switch_frame` records the switch frame
for the light-traffic (`SCALE_SPARSE_AFTER_FRAMES`) threshold drop.

**Speed time axis (`_speed_timestamp_ms`).** Builds a monotonic ms clock preferring the stream PTS
(`pos_msec`) delta and falling back to wall-clock when PTS is 0/non-monotonic. The old `frame_id/fps`
synthetic time under-counted `dt` during HLS drops → over-estimated speed → clipped to 0; the browser
path uses wall-clock directly for the same reason.

**`_build_vehicles`.** Uses the **bbox bottom-centre** (ground-contact point) for the homography
(not the geometric centre), culls Kalman ghost tracks outside the frame, and batches all
pixel→GPS/metre transforms into single `cv2.perspectiveTransform` calls.

**`_inject_its_speed`.** Adds `speed_scale`, `our_avg_kph` (10-min rolling average, needs ≥ 5 samples),
`its_speed_kph`, and `speed_error_pct` to every broadcast (ITS fields omitted when `_its_speed_kph` is
`None`, i.e., no ITS poll has succeeded for the current camera).

**Name parsing.** `_ROAD_NAME_RE` matches both `[국도 1호선]` (bracket) and plain `국도1호선`;
`_NAME_BEARING` maps Korean direction words to degrees; 상행/하행 derive from the road bearing.
`_en_only_name`/`_korname_to_en` build English aliases (National/Provincial Route N, Expressway,
IC/JC/TG/SA, section number, NB↑/SB↓/Both↕).

**`BackgroundMonitor`.** Each camera is an independent `asyncio.Task` polling every `POLL_S = 8 s`
with `detector.detect()` (no tracker, so no contention; detect is lock-serialized). Status thresholds
(`THRESH_BUSY = 6`, `THRESH_CONGESTED = 14`): `normal` (≤6), `busy` (7–14), `congested` (≥15).
Emits `background_status` only when (status, count) changes.

**`history_sampler_loop`.** Every 30 s collects bg + live snapshots, batches the INSERT, recomputes
clusters and broadcasts `congestion_clusters` only when the signature changes, and prunes hourly.

### 4.10 Why the speed time axis matters
Speed = distance / time. Pixel→metre distance is from the homography; **time must come from the frame
content**, not the loop. HLS buffering/drops make naive `frame_id/fps` wrong. PTS-first
(`_speed_timestamp_ms`) plus the OLS window plus EMA smoothing plus the ITS scale together form a
four-layer defence against speed error (§7). Two further mechanisms reduce systematic bias: a
depth-varying correction factor κ (`speed_correction_at`) compensates depth-dependent homography
projection error in the velocity domain, and the far-field reliability cutoff
(`SPEED_TRUST_MAX_DEPTH_M = 100 m`) prevents pixel-noise amplification at range from biasing the
aggregate statistics or triggering false speeding alerts.

---

## 5. Frontend Modules

### 5.1 `App.jsx` — state hub
Owns global state (`selectedCctv`, `cctvList`, `frameData`, `switching`, `calMode`, `isCalibrated`,
`mapMode`, `sidebarTab`, `monitoredCams`, trail map). On a CCTV click: debounce, fly the map to the
camera, `POST /switch-camera`, clear `switching` when `camera_ready` arrives. Builds the vehicle trail
`PathLayer` from a reducer that appends recent positions (capped). Uses `useRef`/`useCallback`/
`React.memo` (CounterPanel, ClassBarChart, VehicleTable) so 30 fps frames don't re-render the sidebar.

**Camera hint banner.** When no camera is selected (`noCameraSelected`), a floating hint is shown at
the bottom-centre of the map. Its colours adapt to `mapMode`: light mode uses a white/slate palette
(`rgba(255,255,255,0.92)` background, dark text, `#cbd5e1` border); dark mode uses the usual dark
card (`rgba(17,24,39,0.88)`, `#374151` border). The 📷 icon gets a cyan glow on dark and no filter
on light. Size and padding are slightly larger than before (14 px text, 12 px 20 px padding).

**`CollapsibleCard`.** Defined inline in `App.jsx`. Accepts a `label` (string or React node) and an
optional `description`. When `description` is provided a small `ℹ` button opens a modal overlay.
The **Auto Calibration** card: (1) becomes visible as soon as `calibrating` is non-null (i.e., during
warm-up, before the commit), showing a `보정 중 (Ns)` / `Calibrating (Ns)` amber badge in the card
header; (2) after commit shows `focal_px` (px) and `residual_px` (colour-coded green < 5 / yellow
< 10 / red ≥ 10) in addition to the existing geometry fields; (3) always shows a **Re-calibrate /
재보정** button (when a camera is selected) that calls `POST /recalibrate`.

### 5.2 `MapView.jsx` — deck.gl rendering
Layer z-order (bottom→top): `congestion-clusters` → trails (`extraLayers`) → `cctv-fov` →
`cctvs-hit` (invisible click target) → `cctv-icons` (status-coloured SVG) → `cctv-labels` →
`vehicles` + `vehicle-labels` (only at zoom ≥ 15) → `snap-nodes` (calibration only). All layers are
memoized; `getTooltip` renders vehicle / node / congestion / CCTV tooltips. Map mode cycles
dark→light→satellite.

**CCTV location deduplication (`singles` useMemo).** Same-location cameras (within
`THRESH = 0.00015°` ≈ 16 m in both lat and lon) are grouped and only one representative icon+label
is rendered per location group, eliminating overlapping Korean + English name labels. Within a group
the currently-selected camera takes priority so its name is always visible.

**Three FOV polygon strategies** (priority):
1. **Manual** — `selectedCctv.calibGpsRing`: the actual 4 clicked GPS corners.
2. **Curved** — `computeRoadCorridorPolygon(road_pts, snap_along_m, heading, near, far, width/2)`:
   walks the centreline, decides F→T vs T→F by comparing `heading` to the local road bearing, then
   offsets ± half-width perpendicular to the road — mirrors `transform.py` so the polygon follows the
   real curve.
3. **Rectangular** — `computeCalibPolygon` (after auto-calibration; same math as the backend GPS
   corners), falling back to `computeFovPolygon` (70° trapezoid) when uncalibrated.

### 5.3 `CctvPlayer.jsx`
Floating, draggable player with tabs: Live (MJPEG/HLS), YOLO (annotated MJPEG / `/ws/detect`),
Calibration overlay, ROI overlay. hls.js handling: 15 s manifest timeout, watchdog that jumps to the
live edge when stalled, `NETWORK_ERROR` → `/cctv-refresh`, full `video.src=""` reset on switch to kill
the previous frame. Capture: JPEG quality 0.92, ≤ 640 px, every `captureIntervalMs`, `maxInFlight=2`.

### 5.4 `CalibrationMode.jsx`
An 8-step state machine alternating pixel clicks (on video) and GPS clicks (on map) for 4 pairs. When
entering a GPS step it fetches nearby NodeLink nodes (`/nodelink/nodes`) and shows them as snap
targets. Save → `POST /calibration`; the backend returns the corner GPS ring and a bearing (point
0→3), which `App` uses to orient the FOV.

### 5.5 `RoiEditor.jsx`
Canvas overlay; click to add vertices, double-click to close (≥ 3). Stores **normalized** coordinates;
`POST /roi` applies immediately to the active detector.

### 5.6 `HistoryPanel.jsx`
Recharts line charts for vehicle count (average + peak) and average speed over 6 h / 24 h / 7 d
(5-min / 15-min / 1-h buckets), peak `ReferenceLine`, CSV export. Polling pauses when the tab is hidden
(`document.hidden` + `visibilitychange`).

### 5.7 `useWebSocket.js`
Single `/ws` connection with 3 s auto-reconnect. Demultiplexes message types into
`frameData, cameraReadyInfo (+counter), calibrating, autoCalibInfo, backgroundStatus, congestionClusters, roiUpdated`
and an `error` string. `calibrating` is `{ elapsed_s } | null`; set on `"calibrating"` messages,
cleared on `"camera_ready"` and `"auto_calibrated"`. `autoCalibInfo` now includes `focal_px` and
`residual_px` from the `auto_calibrated` payload.

### 5.7b `VehicleTable.jsx` — direction tabs
The vehicle list now has a 3-tab toggle (`All / Inbound / Outbound`) above the table. A local
`dirTab` state (`"all" | "in" | "out"`) filters the `vehicles` prop by `v.direction` before
rendering. Tab badges show the count per direction (`tabCounts` memoised from the full list);
active tab colour matches the direction convention (blue = In, red = Out, neutral = All).
The speed-log summary (min/avg/max) is computed from the **currently filtered** set, not all
vehicles. An empty filtered set shows a `—` placeholder instead of an empty table.

### 5.8 `i18n`, `colorMap.js`
React-context i18n (en/ko, `{{param}}` interpolation). `colorMap` maps vehicle direction
(In=blue, Out=red, Unknown=grey; speeding overrides red; parked grey) and congestion severity colours,
with a high-contrast variant for light/satellite maps.

---

## 6. Key Workflows

1. **Camera switch** — click → `App.handleCctvClick` (debounce, fly) → `POST /switch-camera`
   (analytics reset, road snap, bearing, `speed_scale` + scale-model + **pose-prior** restore, queue) →
   `live_loop` switches stream, sets corridor, loads ROI/calibration, schedules auto-calib →
   `camera_ready` broadcast → sidebar/map update; YOLO tab opens `/ws/detect`.
2. **ROI** — ROI tab → draw polygon → `POST /roi` → applied to detector; reloaded on next switch.
3. **Manual calibration** — 4 pixel↔GPS pairs → `POST /calibration` → homography rebuilt,
   `is_calibrated = True`, FOV oriented from the ring bearing.
4. **HLS token recovery** — hls.js `NETWORK_ERROR` → `/cctv-refresh`; server `live_loop` after 3
   failed reconnects calls `_refresh_stream_url` (force) and `hls_refresh_loop` refreshes every 30 min.
5. **Background monitoring** — `POST /background/add` → 8 s `detect()` task → `background_status` →
   icon colour; fed into congestion clustering and history.
6. **Speed self-calibration** — every 5 min ITS segment speed → `calibrate_from_its` → `speed_scale`
   updated using an adaptive α schedule (0.15 → 0.05 → 0.01 for new cameras; 0.01 fixed for restored)
   with a ±10% per-update step clamp, then saved per camera. The scale accumulates across sessions as a
   running soft-reference correction; `speed_reliable` vehicles (GPS ≤ 100 m from snap) feed the
   calibration window exclusively.
7. **Warm-up / commit-once pose calibration** — on switch with no saved pose, warm-up begins:
   frames are accumulated into a ring buffer; `np.median(stack)` produces a vehicle-free clean plate;
   `detect_lane_markings` on the clean plate reliably returns `dash_obs ≥ 3`; `solve_pose` fires
   the focal-free path; pose is saved to `camera_pose.json`; `_locked = True` freezes further
   refits. Next session: saved pose is applied immediately (no warm-up). Manual re-calibrate:
   `POST /recalibrate` → delete saved pose → restart warm-up.

---

## 7. Design Decisions & Limitations

- **Shared-tracker concurrency.** `live_loop` and `/ws/detect` share one `VehicleDetector`; the
  `_detect_clients` counter gates `live_loop` to drain-only when a browser detection client is active,
  preventing two interleaved `track()` call sequences. In addition, `VehicleDetector` uses two
  `threading.Lock` objects to prevent lower-level data races:
  - `_state_lock` serializes the entire `track()` body and `reset_tracker()` against each other. This
    handles the case where a camera-switch or session-teardown triggers `reset_tracker()` while a
    thread-pool thread is mid-`track()`.
  - `_lock` (inner) serializes `model.predict()` only (GPU inference).
  All async call sites use `await asyncio.to_thread(det.reset_tracker)` so lock acquisition does not
  block the event loop. Starvation of background cameras competing for `_lock` is possible in theory
  (Python `threading.Lock` is not FIFO) but has negligible impact at current scale (≤ tens of cameras).
- **JSON TOCTOU.** Three JSON config files (`calibration_data.json`, `speed_scale.json`,
  `vehicle_calib.json`) and `roi_config.json` use read-modify-write patterns. All writes are serialized
  under their respective `threading.Lock` (`_json_file_lock` in `main.py`, `_write_lock` in
  `roi_manager.py`) to prevent concurrent coroutines from overwriting each other's keys.
- **Homography error structure.** A 4-point homography is accurate inside the calibration quad but
  extrapolates with growing error toward the frame top (far vehicles). Mitigation is four-layered:
  (1) dual-matrix manual calibration with near+far points, (2) a 0.7 s OLS window to dilute single-frame
  error, (3) the **road-model pose solver** (replacing the lane trapezoid) for a physically-consistent
  homography that persists/refines per camera, (4) ITS `speed_scale` to absorb the systematic
  (longitudinal) scale error over time.
- **Bidirectional centre fix** uses the link's overall F→T bearing, not the local snap-segment bearing
  (which differs 40–60° on curves and would fail the ±60° reverse-link test).
- **Auto-calibration & the monocular lateral/longitudinal split.** A single road vanishing point
  cannot recover the *longitudinal* (depth → speed) scale from lane width alone — that depends on the
  focal length/FoV. The warm-up clean plate solves this by letting `detect_lane_markings` see static
  dashed lane paint that is invisible on a single compressed frame; the dashed-line period anchors the
  longitudinal scale and enables the **focal-free** `solve_pose` path (5-parameter `θ = (H, pitch, yaw,
  x0, focal)`). Without this, focal is fixed at `FOCAL_RATIO·h` and the residual ~±15–20% longitudinal
  error falls to `speed_scale` (ITS) to absorb. After lock, the vehicle-scale refit is frozen so the
  calibrated pose is never destabilised by noisy bbox-width estimates. Road width is still estimated from
  NodeLink lane count; lane detection can fail at night/rain/dense traffic, in which case the saved
  prior or GPS-grid approximation is used. `is_calibrated` stays False to prompt manual calibration.
- **No camera metadata.** The ITS `cctvInfo` API exposes only position/name/URL — no installation
  height, heading, or FoV — so the pose must be solved from the image + road model, not read off.
- **"Always ×2" road width** assumes bidirectional carriageways; one-way roads are over-wide.
- **YOLO26 vs YOLOv8 transition notes.** YOLO26 is NMS-free (end-to-end), so `YOLO_IOU=0.45` passed
  to `predict()` is ignored — it was the YOLOv8 NMS threshold. `YOLO_CONF` raised to **0.30** (from
  0.25) because NMS-free models never produce post-NMS duplicates, so the earlier low threshold
  admitted more noise detections than were filtered by NMS. The `_dedup_tracks` function
  (IoU/distance based) remains necessary for ByteTrack/OcSort tiers (no ReID), but is NOT a YOLOv8
  NMS substitute — it removes tracker-level duplicates, not detection-level. YOLOv8 remains selectable
  as a legacy fallback; all bbox format handling is model-family-agnostic (supervision `xyxy`).
- **Known issues (`todo.txt`).** Polygon vs vehicle-GPS range can still mismatch on some cameras; the
  nearest NodeLink can be the wrong road (e.g. a national-route camera snapping to an adjacent
  expressway); some cameras read speed ≈ 0 for moving traffic; English names degrade to
  "CCTV xxxxxx" for non-IC/JC roads.

---

## 8. Measurement / Evaluation

The quantitative numbers for the report are produced from the real pipeline (no invented
values). There are two ways to obtain them.

### 8.1 Automatic, while the app runs (`make dev`)
`metrics.py:LiveMetrics` is a thread-safe collector wired into the running server
(`main.py`). Every processed frame feeds it: the server path (`_live_process`) records
per-stage latency (track / transform / analytics) plus tracking, speed, and detection
stats; the browser path (`/ws/detect`) adds tracking/speed/detection stats. So simply
running `make dev` and watching a camera accumulates measurements automatically:
- `history_sampler_loop` flushes `backend/eval_*.csv` + `backend/eval_summary.json` every
  30 s (only once frames have been processed).
- `GET /eval/report` returns the current aggregate as JSON (including a ready-to-paste
  Markdown table) and writes the files immediately.
- `POST /eval/reset` clears the accumulator to start a fresh experiment.

### 8.2 Offline harness (`backend/evaluate.py`)
Runs the same pipeline over a fixed clip/stream for a controlled measurement (e.g., a
repeatable latency benchmark) and shares the aggregation helpers with `metrics.py`.
Usage: `python evaluate.py --source <video-or-HLS> --frames 300 [--lat --lon --bearing]`.

### 8.3 Metrics produced (both paths)
- **Latency / throughput** — per-stage ms (track = YOLO+BoxMOT, transform, analytics),
  mean / median / p95, and end-to-end FPS (`eval_latency.csv`).
- **Tracking stability** — unique tracks, ID-appearance count, mean/median track lifetime
  (`eval_tracking.csv`).
- **Speed distribution** — mean/median/min/max measured speed and % moving, to compare
  against the ITS segment speed; learned per-camera `speed_scale` snapshot
  (`eval_speed.csv`, `eval_summary.json`).
- **Detection counts** — per-class totals as a pipeline sanity check (`eval_detections.csv`).
