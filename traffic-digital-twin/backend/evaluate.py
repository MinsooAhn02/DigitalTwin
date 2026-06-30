"""
evaluate.py — Offline measurement harness for the Traffic Digital Twin report.

Runs the *real* inference pipeline (VehicleDetector.track + PerspectiveTransformer +
TrafficAnalytics) over a video/HLS source and emits the quantitative numbers used in the
report's Evaluation section. It does NOT touch the running server or any saved state — it
builds its own detector/analytics instances and only reads speed_scale.json.

Metrics
-------
1. Latency / throughput   — per-stage ms (track = YOLO+BoxMOT, transform, analytics) +
                            isolated YOLO predict, mean/median/p95, end-to-end FPS.
2. Tracking stability     — unique tracks, ID switches, mean/median track lifetime (frames).
3. Speed distribution     — mean/median/min/max measured speed and % moving (>0), so the
                            measured average can be sanity-checked against the ITS segment
                            speed for the same camera/time.
4. Detection counts       — per-class totals (pipeline sanity check).
5. speed_scale snapshot   — learned per-camera correction factors from speed_scale.json.

Outputs (written next to this script unless --outdir given):
    eval_latency.csv, eval_tracking.csv, eval_speed.csv, eval_detections.csv,
    eval_summary.json
and prints Markdown tables ready to paste into the report.

Usage
-----
    python evaluate.py --source path/to/clip.mp4 --frames 300
    python evaluate.py --source "https://.../playlist.m3u8" --frames 500 \
                       --lat 37.46 --lon 127.04 --bearing 90 --camera-key 16c3b5b2fa97

Run from the backend directory (or anywhere — the script adds its own dir to sys.path).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np

_BACKEND_DIR = Path(__file__).resolve().parent
_LOGS_DIR    = _BACKEND_DIR / "logs"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# Backend modules (import after sys.path fix). Importing detector loads the YOLO model.
from detector import VehicleDetector  # noqa: E402
from transform import PerspectiveTransformer  # noqa: E402
from analytics import TrafficAnalytics, VehicleState  # noqa: E402
from tracker import VehicleTracker  # noqa: E402
from config import VEHICLE_CLASSES  # noqa: E402

SPEED_SCALE_PATH = _BACKEND_DIR / "speed_scale.json"


# ── small stats helpers ──────────────────────────────────────────────────────
def _stats(xs: list[float]) -> dict:
    if not xs:
        return {"n": 0, "mean": 0.0, "median": 0.0, "p95": 0.0, "min": 0.0, "max": 0.0}
    arr = np.asarray(xs, dtype=float)
    return {
        "n": int(arr.size),
        "mean": round(float(arr.mean()), 3),
        "median": round(float(np.median(arr)), 3),
        "p95": round(float(np.percentile(arr, 95)), 3),
        "min": round(float(arr.min()), 3),
        "max": round(float(arr.max()), 3),
    }


def _md_table(headers: list[str], rows: list[list]) -> str:
    line = "| " + " | ".join(headers) + " |"
    sep = "| " + " | ".join("---" for _ in headers) + " |"
    body = "\n".join("| " + " | ".join(str(c) for c in r) + " |" for r in rows)
    return "\n".join([line, sep, body])


def _write_csv(path: Path, headers: list[str], rows: list[list]) -> None:
    lines = [",".join(headers)]
    lines += [",".join(str(c) for c in r) for r in rows]
    path.write_text("\n".join(lines), encoding="utf-8")


# ── core run ─────────────────────────────────────────────────────────────────
def run(args) -> None:
    outdir = Path(args.outdir) if args.outdir else _LOGS_DIR
    outdir.mkdir(parents=True, exist_ok=True)

    print(f"Loading detector (this loads the YOLO model)…")
    detector = VehicleDetector()
    info = detector.tracker_info
    print(f"  backend={info['backend']}  tracker={info['tracker']} (tier={info['tier']})")

    transformer = PerspectiveTransformer()
    if args.lat is not None and args.lon is not None:
        transformer.update_gps_center(args.lat, args.lon, bearing_deg=args.bearing or 0.0)
    analytics = TrafficAnalytics()
    if args.lat is not None and args.lon is not None:
        analytics.cam_lat, analytics.cam_lon = args.lat, args.lon
        analytics.road_bearing_deg = args.bearing
    line_tracker = VehicleTracker()

    # HLS URLs: use the detector's robust opener (resolves HTTP 302 redirects and
    # validates that frames actually decode). Local files: plain VideoCapture.
    if str(args.source).lower().startswith("http"):
        from detector import open_video_source  # noqa: E402
        try:
            cap = open_video_source(args.source)
        except Exception as exc:
            raise SystemExit(f"Could not open HLS source: {exc}")
    else:
        cap = cv2.VideoCapture(args.source, cv2.CAP_FFMPEG)
        if not cap.isOpened():
            raise SystemExit(f"Could not open source: {args.source}")

    # per-stage latency (ms)
    t_yolo: list[float] = []      # isolated detector.detect() (pure YOLO+ROI)
    t_track: list[float] = []     # detector.track() (YOLO+BoxMOT+dedup+idstab)
    t_transform: list[float] = []
    t_analytics: list[float] = []
    t_e2e: list[float] = []       # track + transform + analytics

    # tracking stability
    id_first_seen: dict[int, int] = {}
    id_last_seen: dict[int, int] = {}
    id_frame_count: dict[int, int] = defaultdict(int)
    prev_ids: set[int] = set()
    id_switches = 0

    # detection counts + speeds
    class_totals: dict[str, int] = defaultdict(int)
    speeds: list[float] = []
    moving_frames = 0
    total_vehicle_obs = 0

    n = 0
    processed = 0
    warmup = max(0, args.warmup)
    seconds = args.seconds
    t_measure_start: float | None = None  # set on first post-warmup frame
    if seconds is not None:
        print(f"Measuring for {seconds:.0f}s of decoded video (warmup {warmup} frames)…")
    else:
        print(f"Processing up to {args.frames} frames (warmup {warmup})…")
    while True:
        if seconds is not None:
            if t_measure_start is not None and (time.monotonic() - t_measure_start) >= seconds:
                break
        elif processed >= args.frames:
            break
        ok, frame = cap.read()
        if not ok or frame is None:
            print("  stream ended / read failed.")
            break
        n += 1
        h, w = frame.shape[:2]

        # isolated YOLO timing (optional; adds one extra predict per frame)
        if args.yolo_isolated:
            t0 = time.perf_counter()
            _ = detector.detect(frame)
            yolo_ms = (time.perf_counter() - t0) * 1000.0
        else:
            yolo_ms = None

        # full track
        t0 = time.perf_counter()
        tracked = detector.track(frame)
        track_ms = (time.perf_counter() - t0) * 1000.0

        tracked, in_cnt, out_cnt, in_ids, out_ids = line_tracker.update(tracked, (w, h))

        # transform (mirror _build_vehicles: ground-contact point = bbox bottom-centre)
        pts: list[tuple[float, float]] = []
        metas: list[tuple] = []
        for i in range(len(tracked)):
            xyxy = tracked.xyxy[i].tolist()
            cid = int(tracked.class_id[i]) if tracked.class_id is not None else -1
            tid = int(tracked.tracker_id[i]) if tracked.tracker_id is not None else i
            cx = (xyxy[0] + xyxy[2]) / 2
            pts.append((cx, xyxy[3]))
            metas.append((xyxy, cid, tid, cx, (xyxy[1] + xyxy[3]) / 2))

        t0 = time.perf_counter()
        gps = transformer.batch_pixel_to_gps(pts)
        met = transformer.batch_pixel_to_meter(pts)
        transform_ms = (time.perf_counter() - t0) * 1000.0

        vehicles: list[VehicleState] = []
        for (xyxy, cid, tid, cx, cy), (lat, lon), (xm, ym) in zip(metas, gps, met):
            vehicles.append(VehicleState(
                track_id=tid, class_name=VEHICLE_CLASSES.get(cid, "unknown"),
                bbox_xyxy=xyxy, center_px=(cx, cy),
                lat=lat, lon=lon, x_m=xm, y_m=ym,
            ))

        t0 = time.perf_counter()
        result = analytics.update(n, time.monotonic() * 1000.0, vehicles,
                                  in_cnt, out_cnt, in_ids, out_ids)
        analytics_ms = (time.perf_counter() - t0) * 1000.0

        if n <= warmup:
            continue
        if t_measure_start is None:
            t_measure_start = time.monotonic()
        processed += 1

        # record latency
        if yolo_ms is not None:
            t_yolo.append(yolo_ms)
        t_track.append(track_ms)
        t_transform.append(transform_ms)
        t_analytics.append(analytics_ms)
        t_e2e.append(track_ms + transform_ms + analytics_ms)

        # tracking stability
        cur_ids = {v.track_id for v in vehicles}
        for tid in cur_ids:
            id_first_seen.setdefault(tid, n)
            id_last_seen[tid] = n
            id_frame_count[tid] += 1
        id_switches += len(cur_ids - prev_ids)  # newly appearing IDs
        prev_ids = cur_ids

        # detection counts + speeds (from FrameAnalytics dict)
        for v in result.vehicles:
            class_totals[v.get("class_name", "unknown")] += 1
            total_vehicle_obs += 1
            sp = float(v.get("speed_kph", 0.0) or 0.0)
            if sp > 0:
                speeds.append(sp)
                moving_frames += 1

        if processed % 50 == 0:
            if seconds is not None:
                el = time.monotonic() - t_measure_start
                print(f"  {processed} frames / {el:.0f}s  (vehicles: {len(vehicles)})")
            else:
                print(f"  {processed}/{args.frames}  (vehicles this frame: {len(vehicles)})")

    cap.release()

    if processed == 0:
        raise SystemExit("No frames processed — check --source and --frames.")

    # ── aggregate ──────────────────────────────────────────────────────────
    lat_track = _stats(t_track)
    lat_transform = _stats(t_transform)
    lat_analytics = _stats(t_analytics)
    lat_e2e = _stats(t_e2e)
    lat_yolo = _stats(t_yolo) if t_yolo else None
    fps = round(1000.0 / lat_e2e["mean"], 1) if lat_e2e["mean"] > 0 else 0.0

    lifetimes = list(id_frame_count.values())
    track_stats = {
        "frames_processed": processed,
        "unique_tracks": len(id_first_seen),
        "id_appearances": id_switches,
        "mean_lifetime_frames": round(float(np.mean(lifetimes)), 2) if lifetimes else 0.0,
        "median_lifetime_frames": round(float(np.median(lifetimes)), 1) if lifetimes else 0.0,
        "max_lifetime_frames": int(max(lifetimes)) if lifetimes else 0,
    }

    speed_stats = _stats(speeds)
    speed_stats["pct_moving_obs"] = (
        round(100.0 * moving_frames / total_vehicle_obs, 1) if total_vehicle_obs else 0.0
    )
    speed_stats["total_vehicle_obs"] = total_vehicle_obs

    speed_scale_snapshot = {}
    if SPEED_SCALE_PATH.exists():
        try:
            speed_scale_snapshot = json.loads(SPEED_SCALE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass

    summary = {
        "source": args.source,
        "frames_processed": processed,
        "inference_backend": info["backend"],
        "tracker": info["tracker"],
        "tracker_tier": info["tier"],
        "latency_ms": {
            "yolo_isolated": lat_yolo,
            "track": lat_track,
            "transform": lat_transform,
            "analytics": lat_analytics,
            "end_to_end": lat_e2e,
        },
        "throughput_fps": fps,
        "tracking": track_stats,
        "speed": speed_stats,
        "detection_class_totals": dict(class_totals),
        "speed_scale_snapshot": speed_scale_snapshot,
    }

    (outdir / "eval_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    # CSVs
    lat_rows = [
        ["yolo_isolated", *( [lat_yolo["mean"], lat_yolo["median"], lat_yolo["p95"]] if lat_yolo else ["", "", ""])],
        ["track", lat_track["mean"], lat_track["median"], lat_track["p95"]],
        ["transform", lat_transform["mean"], lat_transform["median"], lat_transform["p95"]],
        ["analytics", lat_analytics["mean"], lat_analytics["median"], lat_analytics["p95"]],
        ["end_to_end", lat_e2e["mean"], lat_e2e["median"], lat_e2e["p95"]],
    ]
    _write_csv(outdir / "eval_latency.csv",
               ["stage", "mean_ms", "median_ms", "p95_ms"], lat_rows)

    _write_csv(outdir / "eval_tracking.csv",
               list(track_stats.keys()), [list(track_stats.values())])

    _write_csv(outdir / "eval_speed.csv",
               list(speed_stats.keys()), [list(speed_stats.values())])

    _write_csv(outdir / "eval_detections.csv",
               ["class", "total_detections"],
               [[k, v] for k, v in sorted(class_totals.items())])

    # ── print Markdown ──────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print(f"RESULTS  ({processed} frames, backend={info['backend']}, "
          f"tracker={info['tracker']})")
    print("=" * 70)

    print("\n### Latency per stage (ms)\n")
    lat_md_rows = []
    if lat_yolo:
        lat_md_rows.append(["YOLO (isolated)", lat_yolo["mean"], lat_yolo["median"], lat_yolo["p95"]])
    lat_md_rows += [
        ["Track (YOLO+BoxMOT)", lat_track["mean"], lat_track["median"], lat_track["p95"]],
        ["Transform", lat_transform["mean"], lat_transform["median"], lat_transform["p95"]],
        ["Analytics", lat_analytics["mean"], lat_analytics["median"], lat_analytics["p95"]],
        ["**End-to-end**", lat_e2e["mean"], lat_e2e["median"], lat_e2e["p95"]],
    ]
    print(_md_table(["Stage", "Mean", "Median", "p95"], lat_md_rows))
    print(f"\n**Throughput: {fps} FPS** (1000 / mean end-to-end ms)")

    print("\n### Tracking stability\n")
    print(_md_table(["Metric", "Value"], [[k, v] for k, v in track_stats.items()]))

    print("\n### Speed distribution (measured, km/h)\n")
    print(_md_table(["Metric", "Value"], [[k, v] for k, v in speed_stats.items()]))
    print("\n> NOTE: this harness uses the uncalibrated grid homography (no warm-up / "
          "focal-free pose), so these speeds are rough sanity checks, not pose-calibrated "
          "figures. Latency and tracking metrics above are unaffected by calibration.")

    print("\n### Detection class totals\n")
    print(_md_table(["Class", "Detections"],
                    [[k, v] for k, v in sorted(class_totals.items())]))

    if speed_scale_snapshot:
        print("\n### Learned speed_scale (speed_scale.json)\n")
        rows = [[k, v.get("speed_scale"), v.get("converged")]
                for k, v in speed_scale_snapshot.items()]
        print(_md_table(["camera_key", "speed_scale", "converged"], rows))

    print(f"\nWrote CSVs + eval_summary.json to: {outdir}")


def main() -> None:
    p = argparse.ArgumentParser(description="Traffic Digital Twin evaluation harness")
    p.add_argument("--source", required=True, help="Video file path or HLS URL")
    p.add_argument("--frames", type=int, default=300, help="Frames to measure (after warmup)")
    p.add_argument("--seconds", type=float, default=None,
                   help="Measure for N seconds of decoded video instead of a fixed frame count")
    p.add_argument("--warmup", type=int, default=20, help="Warmup frames to skip from stats")
    p.add_argument("--lat", type=float, default=None, help="Camera latitude (optional, for transform)")
    p.add_argument("--lon", type=float, default=None, help="Camera longitude (optional)")
    p.add_argument("--bearing", type=float, default=None, help="Road bearing deg (optional)")
    p.add_argument("--camera-key", default=None, help="(informational) camera key for the report")
    p.add_argument("--yolo-isolated", action="store_true",
                   help="Also time an isolated YOLO predict per frame (adds load)")
    p.add_argument("--outdir", default=None, help="Output directory (default: backend dir)")
    run(p.parse_args())


if __name__ == "__main__":
    main()
