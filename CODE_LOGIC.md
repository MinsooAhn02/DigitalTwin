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
2. detects vehicles (YOLOv8) and tracks them across frames (BoxMOT multi-object tracker),
3. converts each vehicle's pixel position to a GPS coordinate via a homography that is either
   manually calibrated, automatically estimated from lane geometry, or approximated from the road
   network,
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
| Detection | ultralytics **YOLOv8** (classes 2/3/5/7 = car/motorcycle/bus/truck) |
| Inference backend | **TensorRT FP16 `.engine` → ONNX Runtime → PyTorch** (auto-selected, `detector.py:resolve_model_selection`) |
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
| External data | ITS OpenAPI `cctvInfo` + `trafficInfo`; OSM Overpass; MOCT NodeLink shapefile → SQLite |

---

## 4. Backend Modules

### 4.1 `detector.py` — detection + tracking

**Model selection (`resolve_model_selection`).** Walks candidate variant stems (`yolov8{x,l,m,s,n}`
or the configured `YOLO_MODEL`) and picks, in priority order: an existing `.engine` (TensorRT, if CUDA
+ tensorrt present), else export `.pt→.engine` on the fly (`YOLO_AUTO_EXPORT_ENGINE`), else
`.onnx` (ONNX Runtime), else `.pt` (PyTorch). FP16 (`half=True`) is used only for the PyTorch path on
CUDA; TensorRT engines are already FP16.

**Tracker tiers.** `TRACKER_TIER` (`auto` by default). `_auto_tracker_tier` picks by VRAM:
≥10 GB → `high`, ≥6 GB → `medium`, ≥3.5 GB → `low`, else `cpu`.

| Tier | Tracker | ReID weights | Notes |
|------|---------|--------------|-------|
| cpu | ByteTrack | none | fastest, no GPU; patched (see below) |
| low | OcSort | none | `min_hits=1, max_age=BYTE_TRACK_BUFFER`; strong occlusion handling |
| medium | BotSort | `osnet_x0_25_msmt17.pt` | appearance ReID; `cmc_method="sof"` (optical flow) to avoid ECC failures on low-texture night frames |
| high | DeepOcSort | `osnet_x1_0_msmt17.pt` | appearance ReID, 8 GB+ |

**`track(frame)` pipeline.**
1. `should_detect = (frame_count-1) % detect_interval == 0`. `_detect_interval` is forced to **1** on
   TensorRT/ONNX (inference is cheap); only CPU honours `YOLO_DETECT_INTERVAL`.
2. On detect frames: YOLO `predict` → `sv.Detections` → ROI mask (`_apply_roi` via `sv.PolygonZone`).
3. **Empty-detection grace:** if YOLO returns nothing and the empty streak ≤ `_YOLO_MISS_GRACE` (2),
   it returns the *last* tracks and skips `tracker.update`. Calling `update(empty)` would make
   ByteTrack/OcSort mark all tracks LOST and re-issue new raw IDs on the next real detection →
   IDStabilizer mismatch → duplicate IDs. Preserving tracker state lets the same raw ID re-match.
4. On non-detect frames: re-feed the **last** detection array (never empty) so IoU matching keeps IDs
   and the Kalman filter advances.
5. For `cpu`/`low` tiers (no ReID): `_dedup_tracks` then `IDStabilizer.update`.

**`_dedup_tracks`.** Removes duplicate tracks where IoU > 0.3 **or** centre distance < 40 px, keeping
the lower (older) ID. Handles ByteTrack emitting two IDs for one car.

**`IDStabilizer`** (only for cpu/low, which lack appearance ReID). Restores a vehicle's previous ID
after a brief miss, using last-known-centre nearest-neighbour matching (≤ 80 px). Two-pass design:
- **Pass 1:** raw IDs already in `_remap` reclaim their stable ID first (prevents a new track from
  stealing it via `_find_lost`).
- **Pass 2:** unmatched tracks match against `_lost`; on match it **purges all stale `_remap`
  entries** pointing at that stable ID (otherwise dozens of old raw→stable mappings accumulate and the
  tracker reusing those raw IDs collapses them into one display row).
- Tracks that disappear within 50 px of a frame edge are evicted immediately (they left the scene, so
  their ID must not be handed to a newly entering vehicle).
- Display IDs are renumbered 1,2,3… (`_display_map`) so the UI never shows ByteTrack's 200+ counters.

**ByteTrack patch.** boxmot 12.x sets `STrack.is_activated` only when `frame_id == 1`; with
`DETECT_INTERVAL > 1` new tracks were never returned. `_patched_activate` forces `is_activated = True`.

**`VideoStream`.** OpenCV `CAP_FFMPEG`. Deliberately does **not** set `CAP_PROP_BUFFERSIZE=1`: jumping
to the newest frame creates large inter-frame motion that breaks BoT-SORT's camera-motion compensation
(ECC). It exposes `pos_msec` (stream PTS) for an accurate speed time axis (§4.11). `reconnect()` waits
3 s and reopens the same URL.

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

**Automatic calibration from one frame (`auto_calibrate_from_frame`).** Estimates a homography from
lane geometry when no manual calibration exists. Steps:
1. Gray → Gaussian blur → Canny; keep only the lower 55 % (road ROI).
2. `HoughLinesP`; keep diagonals (< 60° from vertical).
3. Sample road edges at 5 vertical levels; per level take the 15th/85th percentile x as left/right.
4. Least-squares fit `x = a·y + b` for each edge.
5. **Vanishing point** = intersection of the two fitted lines (with sane bounds; parallel-line
   fallback uses the median of pairwise Hough intersections).
6. **Direction decision:** curvature match if available, else compare the VP horizontal angle
   `φ = atan2(vp_x − w/2, fy)` against the camera→snap bearing (flip 180° if the reverse candidate is
   closer), else fall back to `vp_x > 0.55·w`. Skipped entirely when `fix_direction=True`
   (a name-derived bearing is already trusted).
7. Pitch `= atan2(h/2 − vp_y, fy)` clamped to 3–50°; `fy = h·1.2` (assumed ~45° vertical FoV).
8. Pinhole near distance `d_near = road_width_m · fy / road_px_w`; camera height
   `cam_h = d_near · tan(pitch + vfov/2)` clamped 3–40 m; far distance from `cam_h / tan(pitch − vfov/2)`.
9. Build a trapezoid `src_pts` from the fitted edges, GPS corners from bearing + near/far + half-width,
   then both homographies. `is_calibrated` stays **False** (auto-calib is an approximation), so the UI
   still shows "not calibrated" and `speed_scale` keeps correcting.

**Fallback grid (`update_gps_center`).** With no calibration at all, builds a trapezoidal homography
(top edge 25–75 % of width, near 15 m / far 80 m / half-width 25 m) rotated to the road bearing. Also
sets `is_calibrated = False`.

### 4.4 `analytics.py` — metrics engine

`VehicleState` (per vehicle): `track_id, class_name, bbox_xyxy, center_px, lat, lon, x_m, y_m,
direction, speed_kph, is_speeding, dwell_frames, is_bottleneck, is_parked`.
`FrameAnalytics` (per frame): `frame_id, timestamp_ms, vehicles[], vehicle_count, avg_speed_kph,
los_grade, in_count, out_count, class_counts`.

**Speed pipeline (`_speed`, the most-tuned logic).** Per track:
1. **Duplicate skip** — identical metre coordinates (non-detect frames) are not appended.
2. **Physics jump guard** — before appending, if `step_m > (MAX_REASONABLE_KPH/3.6/speed_scale)·dt·1.5`
   the sample is dropped but the window is *kept* (an earlier version cleared the whole window and
   produced 0 speed ~47 % of the time). `dt > 2 s` clears the window (track re-appeared).
3. **OLS regression** — over a sliding window of `SPEED_WINDOW_FRAMES = 18` `(x_m, y_m, t)` samples,
   fit velocity by least squares: `kph = hypot(vx, vy)·3.6`. Window displacement
   `< SPEED_JITTER_THRESHOLD_M = 0.5 m` ⇒ speed 0 (jitter).
4. **Per-track EMA** (`_speed_ema`, α = `SPEED_EMA_ALPHA` = 0.35). Spike reject: if a confirmed EMA
   (> 5) and `scaled > ema·2.5 + 20`, ignore the sample. The EMA is **never seeded at 0** (a 0 seed
   makes spike-reject block all real speeds → stuck at 0). Stop decay: when stopped, decay ×0.6 and
   floor to 0 below `SPEED_MIN_KPH = 5`.
5. **Scale + flag** — `speed_kph = round(raw · speed_scale, 1)`; `is_speeding = speed_kph > limit`.
   `MAX_REASONABLE_KPH = 180` rejects only ID-swap/homography blow-ups (so legit highway speed passes
   and feeds the ITS calibration).

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

**ITS self-calibration (`calibrate_from_its`).** Compares a 10-min rolling average of measured speed
against the ITS segment speed:
- needs ≥ 50 samples in the 600 s window; skip if average < 3 kph.
- **Volatility guard:** coefficient of variation > 0.4 ⇒ skip (traffic in transition).
- `target = old_scale · ITS / our_avg`, **clamped to [0.3, 5.0]** (a clamp hit logs a warning —
  surfaces a badly-off homography instead of failing silently).
- Learning rate `α = 0.5` before convergence, `0.95` after (slow once stable).
- Convergence: `_stable_count ≥ 3` consecutive updates with < 1 % change. The 10-min window is double
  the ITS 5-min aggregation so the ITS window is always contained regardless of poll phase.

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

### 4.6 `osm.py` — OSM road width
`get_road_width_m(lat, lon, radius_m=30)` queries Overpass for the nearest `highway=*` way. Width
priority: explicit `width` tag → `lanes:forward × lane_w` → `lanes/2 × lane_w`. Lane width 3.5 m
(motorway/trunk), 3.25 m (primary), else 3.0 m. 7 s timeout; on any failure returns `None` and the
caller falls back to NodeLink lanes.

### 4.7 `congestion.py` — camera-level clustering
Background cameras have only a vehicle *count* and a status, not per-vehicle GPS, so congestion is
clustered at the **camera** level. `_cluster_points` is a greedy DBSCAN (haversine distance, `eps`
= `CONGESTION_EPS_M` = 500 m, `min_samples` = 1, BFS connected components) over busy/congested
cameras. Polygon: Andrew monotone-chain convex hull for ≥ 3 cameras, else a 120 m circle. Severity
(`_severity`): **severe** if ≥ 2 congested or total > 6·members; **medium** if any congested/busy;
else **minor**.

### 4.8 `history.py` — SQLite time-series
WAL-mode SQLite, single connection + lock (called via `asyncio.to_thread`). One `snapshots` table
(`ts, cam_key, name, name_ko, lat, lon, source ['bg'|'live'], vehicle_count, class_counts JSON,
status, avg_speed_kph`) with an `(cam_key, ts)` index. `record_many` batches one sampler tick.
`series` buckets by `CAST(ts/bucket)·bucket` returning per-bucket average + peak vehicle count and
average speed. `peak` returns the max-count timestamp. `export_rows` feeds CSV. `prune` deletes rows
older than `retention_cutoff(HISTORY_RETENTION_DAYS = 14)`.

### 4.9 `roi_manager.py`, `config.py`, `utils.py`
- `roi_manager` — ROI polygons stored as **normalized** [0,1] coordinates (resolution-independent),
  keyed by `camera_key = md5(url)[:12]`; `roi_to_pixels` converts for `sv.PolygonZone`.
- `config.py` — all constants (YOLO params, tracker buffers, speed thresholds, LOS, history,
  congestion, ITS URLs/keys). Runtime profile (`.runtime_profile.json`) overrides capture/FPS/JPEG.
- `utils.py` — `haversine_m` geodesic distance.

### 4.10 `main.py` — server, endpoints, orchestration

**REST endpoints.**
| Method · path | Purpose |
|---|---|
| `GET /cctvs` | ITS CCTV list for the viewport bbox (5-min `TTLCache`); adds EN names + dedup numbering |
| `POST /switch-camera` | switch the live camera (see below) |
| `GET /cctv-refresh` | fresh HLS URL after token expiry (browser) |
| `GET /hls-proxy` | CORS proxy that rewrites m3u8 segment URLs and streams .ts |
| `GET /video-stream`, `/video-stream-yolo` | MJPEG of raw / annotated live frames |
| `GET /nodelink/nodes` | nearby road nodes for calibration GPS snapping |
| `GET/POST/DELETE /roi`, `/calibration` | ROI and 4-point calibration CRUD |
| `POST /background/add`, `/background/remove/{key}`, `GET /background/status` | multi-camera monitoring |
| `GET /history/cameras`, `/history/series`, `/history/peak`, `/history/export.csv` | history analytics |
| `POST /viewer-active` | report tab visibility (pauses live GPU work) |
| `POST /stop-camera`, `GET /health`, `/runtime-config`, `/speed-debug/{state}` | control/diagnostics |

**WebSockets.** `/ws` (broadcast sink) and `/ws/detect` (browser JPEG → annotate → analytics).
Messages on `/ws`: `camera_ready`, `auto_calibrated`, `camera_error`, `background_status`,
`congestion_clusters`, else a `FrameAnalytics` JSON.

**`switch_camera`.** Bumps `_cam_version` (so `/ws/detect` resets its tracker), resets analytics,
restores the saved per-camera `speed_scale`, resets the BoxMOT tracker, kicks an async ITS speed
fetch, queries NodeLink (`get_road_info` + `get_road_snap` with the parsed road-name hint), sets
`speed_limit_kph` and the effective bearing (priority **name_bearing ?? snap bearing ?? link
bearing**), stores `_current_cam`, and queues the stream switch for `live_loop`.

**`live_loop` camera-switch block.** Switches the OpenCV stream, sets the road corridor
(`set_road_corridor`), restores saved ROI and manual calibration (if any), schedules 5 auto-calibration
attempts when there's no manual calibration, computes road width (camera value or NodeLink fallback),
and broadcasts `camera_ready` (then `auto_calibrated` once lane detection succeeds).

**Speed time axis (`_speed_timestamp_ms`).** Builds a monotonic ms clock preferring the stream PTS
(`pos_msec`) delta and falling back to wall-clock when PTS is 0/non-monotonic. The old `frame_id/fps`
synthetic time under-counted `dt` during HLS drops → over-estimated speed → clipped to 0; the browser
path uses wall-clock directly for the same reason.

**`_build_vehicles`.** Uses the **bbox bottom-centre** (ground-contact point) for the homography
(not the geometric centre), culls Kalman ghost tracks outside the frame, and batches all
pixel→GPS/metre transforms into single `cv2.perspectiveTransform` calls.

**`_inject_its_speed`.** Adds `speed_scale`, `speed_scale_converged`, `our_avg_kph` (10-min rolling,
needs ≥ 5 samples), `its_speed_kph`, and `speed_error_pct` to every broadcast.

**Name parsing.** `_ROAD_NAME_RE` matches both `[국도 1호선]` (bracket) and plain `국도1호선`;
`_NAME_BEARING` maps Korean direction words to degrees; 상행/하행 derive from the road bearing.
`_en_only_name`/`_korname_to_en` build English aliases (National/Provincial Route N, Expressway,
IC/JC/TG/SA, section number, NB↑/SB↓/Both↕).

**`BackgroundMonitor`.** Each camera is an independent `asyncio.Task` polling every `POLL_S = 8 s`
with `detector.detect()` (no tracker, so no contention; detect is lock-serialized). Status:
`normal` (≤3), `busy` (>3), `congested` (>6). Emits `background_status` only when (status, count)
changes.

**`history_sampler_loop`.** Every 30 s collects bg + live snapshots, batches the INSERT, recomputes
clusters and broadcasts `congestion_clusters` only when the signature changes, and prunes hourly.

### 4.11 Why the speed time axis matters
Speed = distance / time. Pixel→metre distance is from the homography; **time must come from the frame
content**, not the loop. HLS buffering/drops make naive `frame_id/fps` wrong. PTS-first
(`_speed_timestamp_ms`) plus the OLS window plus EMA smoothing plus the ITS scale together form a
four-layer defence against speed error (§7).

---

## 5. Frontend Modules

### 5.1 `App.jsx` — state hub
Owns global state (`selectedCctv`, `cctvList`, `frameData`, `switching`, `calMode`, `isCalibrated`,
`mapMode`, `sidebarTab`, `monitoredCams`, trail map). On a CCTV click: debounce, fly the map to the
camera, `POST /switch-camera`, clear `switching` when `camera_ready` arrives. Builds the vehicle trail
`PathLayer` from a reducer that appends recent positions (capped). Uses `useRef`/`useCallback`/
`React.memo` (CounterPanel, ClassBarChart, VehicleTable) so 30 fps frames don't re-render the sidebar.

### 5.2 `MapView.jsx` — deck.gl rendering
Layer z-order (bottom→top): `congestion-clusters` → trails (`extraLayers`) → `cctv-fov` →
`cctvs-hit` (invisible click target) → `cctv-icons` (status-coloured SVG) → `cctv-labels` →
`vehicles` + `vehicle-labels` (only at zoom ≥ 15) → `snap-nodes` (calibration only). All layers are
memoized; `getTooltip` renders vehicle / node / congestion / CCTV tooltips. Map mode cycles
dark→light→satellite.

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
Single `/ws` connection with 3 s auto-reconnect. Demultiplexes 6 message types into
`frameData, cameraReadyInfo (+counter), autoCalibInfo, backgroundStatus, congestionClusters` and an
`error` string.

### 5.8 `i18n`, `colorMap.js`
React-context i18n (en/ko, `{{param}}` interpolation). `colorMap` maps vehicle direction
(In=blue, Out=red, Unknown=grey; speeding overrides red; parked grey) and congestion severity colours,
with a high-contrast variant for light/satellite maps.

---

## 6. Key Workflows

1. **Camera switch** — click → `App.handleCctvClick` (debounce, fly) → `POST /switch-camera`
   (analytics reset, road snap, bearing, speed_scale restore, queue) → `live_loop` switches stream,
   sets corridor, loads ROI/calibration, schedules auto-calib → `camera_ready` broadcast → sidebar/map
   update; YOLO tab opens `/ws/detect`.
2. **ROI** — ROI tab → draw polygon → `POST /roi` → applied to detector; reloaded on next switch.
3. **Manual calibration** — 4 pixel↔GPS pairs → `POST /calibration` → homography rebuilt,
   `is_calibrated = True`, FOV oriented from the ring bearing.
4. **HLS token recovery** — hls.js `NETWORK_ERROR` → `/cctv-refresh`; server `live_loop` after 3
   failed reconnects calls `_refresh_stream_url` (force) and `hls_refresh_loop` refreshes every 30 min.
5. **Background monitoring** — `POST /background/add` → 8 s `detect()` task → `background_status` →
   icon colour; fed into congestion clustering and history.
6. **Speed self-calibration** — every 5 min ITS segment speed → `calibrate_from_its` → `speed_scale`
   converges (3× < 1 % change) and is saved per camera.

---

## 7. Design Decisions & Limitations

- **Shared-tracker concurrency.** `live_loop` and `/ws/detect` share one `VehicleDetector`; concurrent
  `track()` calls interleave frame sequences and corrupt tracking. `/ws/detect` activity makes
  `live_loop` drain-only; `reset_tracker()` runs when `/ws/detect` ends.
- **Homography error structure.** A 4-point homography is accurate inside the calibration quad but
  extrapolates with growing error toward the frame top (far vehicles). Mitigation is four-layered:
  (1) dual-matrix manual calibration with near+far points, (2) 18-frame OLS to dilute single-frame
  error, (3) lane-based auto-calibration for an initial estimate, (4) ITS `speed_scale` to absorb the
  systematic scale error over time.
- **Bidirectional centre fix** uses the link's overall F→T bearing, not the local snap-segment bearing
  (which differs 40–60° on curves and would fail the ±60° reverse-link test).
- **Auto-calibration limits.** `fy = h·1.2` (unknown true focal length) → ±20–30 % scale error; road
  width is estimated from NodeLink lane count, not measured; lane detection fails at night / in rain /
  in dense traffic, leaving the fallback grid (`is_calibrated` stays False to prompt manual
  calibration). Residual scale error is absorbed by `speed_scale`.
- **"Always ×2" road width** assumes bidirectional carriageways; one-way roads are over-wide.
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
