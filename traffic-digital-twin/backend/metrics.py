"""
metrics.py — shared measurement utilities + a live in-process metrics collector.

Two consumers:
  • evaluate.py  — offline CLI harness (imports the stats/csv/markdown helpers).
  • main.py      — the running server feeds every processed frame into a global
                   LiveMetrics instance, so simply running `make dev` and watching
                   a camera automatically accumulates measurements. The data is
                   flushed to backend/eval_*.csv + eval_summary.json periodically
                   and on demand via GET /eval/report.

No external deps beyond numpy (already a backend dependency).
"""

from __future__ import annotations

import json
import threading
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

BACKEND_DIR = Path(__file__).resolve().parent
LOGS_DIR    = BACKEND_DIR / "logs"


# ── stats / formatting helpers (shared with evaluate.py) ─────────────────────
def stats(xs: list[float]) -> dict:
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


def md_table(headers: list[str], rows: list[list]) -> str:
    line = "| " + " | ".join(headers) + " |"
    sep = "| " + " | ".join("---" for _ in headers) + " |"
    body = "\n".join("| " + " | ".join(str(c) for c in r) + " |" for r in rows)
    return "\n".join([line, sep, body])


def write_csv(path: Path, headers: list[str], rows: list[list]) -> None:
    lines = [",".join(headers)]
    lines += [",".join(str(c) for c in r) for r in rows]
    path.write_text("\n".join(lines), encoding="utf-8")


def summary_to_markdown(s: dict) -> str:
    """Render an eval summary dict (from LiveMetrics.report / evaluate.py) as Markdown."""
    lat = s.get("latency_ms", {})
    out: list[str] = []
    out.append(f"### Latency per stage (ms) — {s.get('frames_processed', 0)} frames\n")
    rows = []
    if lat.get("yolo_isolated") and lat["yolo_isolated"].get("n"):
        y = lat["yolo_isolated"]; rows.append(["YOLO (isolated)", y["mean"], y["median"], y["p95"]])
    for key, label in (("track", "Track (YOLO+BoxMOT)"), ("transform", "Transform"),
                       ("analytics", "Analytics"), ("end_to_end", "End-to-end")):
        d = lat.get(key) or {}
        rows.append([label, d.get("mean", "—"), d.get("median", "—"), d.get("p95", "—")])
    out.append(md_table(["Stage", "Mean", "Median", "p95"], rows))
    out.append(f"\n**Throughput: {s.get('throughput_fps', 0)} FPS**\n")

    tr = s.get("tracking", {})
    out.append("### Tracking stability\n")
    out.append(md_table(["Metric", "Value"], [[k, v] for k, v in tr.items()]))

    sp = s.get("speed", {})
    out.append("\n### Speed distribution (measured, km/h)\n")
    out.append(md_table(["Metric", "Value"], [[k, v] for k, v in sp.items()]))

    dc = s.get("detection_class_totals", {})
    if dc:
        out.append("\n### Detection class totals\n")
        out.append(md_table(["Class", "Detections"], [[k, v] for k, v in sorted(dc.items())]))
    return "\n".join(out)


# ── live in-process collector ────────────────────────────────────────────────
class LiveMetrics:
    """Thread-safe accumulator fed by the running pipeline (main.py)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._track: list[float] = []
            self._transform: list[float] = []
            self._analytics: list[float] = []
            self._e2e: list[float] = []
            self._class_totals: dict[str, int] = defaultdict(int)
            self._speeds: list[float] = []
            self._moving_obs = 0
            self._total_obs = 0
            self._id_frames: dict[int, int] = defaultdict(int)
            self._prev_ids: set[int] = set()
            self._id_appearances = 0
            self._frames = 0
            self._t_start = time.monotonic()
            self._source = ""
            self._backend = ""
            self._tracker = ""

    def set_context(self, source: str = "", backend: str = "", tracker: str = "") -> None:
        with self._lock:
            if source:
                self._source = source
            if backend:
                self._backend = backend
            if tracker:
                self._tracker = tracker

    def add_frame(
        self,
        vehicles: list[dict],
        track_ms: float | None = None,
        transform_ms: float | None = None,
        analytics_ms: float | None = None,
    ) -> None:
        """Record one processed frame. Stage times are optional (live path provides
        all three; the browser /ws/detect path may pass None and still contributes
        tracking/speed/detection stats)."""
        with self._lock:
            self._frames += 1
            if track_ms is not None and transform_ms is not None and analytics_ms is not None:
                self._track.append(track_ms)
                self._transform.append(transform_ms)
                self._analytics.append(analytics_ms)
                self._e2e.append(track_ms + transform_ms + analytics_ms)

            ids: set[int] = set()
            for v in vehicles:
                cls = v.get("class_name", "unknown")
                self._class_totals[cls] += 1
                self._total_obs += 1
                sp = float(v.get("speed_kph", 0.0) or 0.0)
                if sp > 0:
                    self._speeds.append(sp)
                    self._moving_obs += 1
                tid = v.get("track_id")
                if tid is not None:
                    ids.add(int(tid))
                    self._id_frames[int(tid)] += 1
            self._id_appearances += len(ids - self._prev_ids)
            self._prev_ids = ids

    def report(self, outdir: Path | None = None, write: bool = True) -> dict:
        with self._lock:
            lat_track = stats(self._track)
            lat_tf = stats(self._transform)
            lat_an = stats(self._analytics)
            lat_e2e = stats(self._e2e)
            fps = round(1000.0 / lat_e2e["mean"], 1) if lat_e2e["mean"] > 0 else 0.0
            lifetimes = list(self._id_frames.values())
            tracking = {
                "frames_processed": self._frames,
                "unique_tracks": len(self._id_frames),
                "id_appearances": self._id_appearances,
                "mean_lifetime_frames": round(float(np.mean(lifetimes)), 2) if lifetimes else 0.0,
                "median_lifetime_frames": round(float(np.median(lifetimes)), 1) if lifetimes else 0.0,
                "max_lifetime_frames": int(max(lifetimes)) if lifetimes else 0,
            }
            speed = stats(self._speeds)
            speed["pct_moving_obs"] = (
                round(100.0 * self._moving_obs / self._total_obs, 1) if self._total_obs else 0.0
            )
            speed["total_vehicle_obs"] = self._total_obs
            class_totals = dict(self._class_totals)
            elapsed = round(time.monotonic() - self._t_start, 1)
            summary = {
                "source": self._source,
                "inference_backend": self._backend,
                "tracker": self._tracker,
                "frames_processed": self._frames,
                "elapsed_s": elapsed,
                "latency_ms": {
                    "yolo_isolated": {"n": 0},
                    "track": lat_track,
                    "transform": lat_tf,
                    "analytics": lat_an,
                    "end_to_end": lat_e2e,
                },
                "throughput_fps": fps,
                "tracking": tracking,
                "speed": speed,
                "detection_class_totals": class_totals,
            }

        # speed_scale snapshot (read outside lock)
        ssp = BACKEND_DIR / "speed_scale.json"
        if ssp.exists():
            try:
                summary["speed_scale_snapshot"] = json.loads(ssp.read_text(encoding="utf-8"))
            except Exception:
                summary["speed_scale_snapshot"] = {}

        if write:
            d = outdir or LOGS_DIR
            d.mkdir(parents=True, exist_ok=True)
            (d / "eval_summary.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
            lat = summary["latency_ms"]
            write_csv(d / "eval_latency.csv", ["stage", "mean_ms", "median_ms", "p95_ms"], [
                ["track", lat["track"]["mean"], lat["track"]["median"], lat["track"]["p95"]],
                ["transform", lat["transform"]["mean"], lat["transform"]["median"], lat["transform"]["p95"]],
                ["analytics", lat["analytics"]["mean"], lat["analytics"]["median"], lat["analytics"]["p95"]],
                ["end_to_end", lat["end_to_end"]["mean"], lat["end_to_end"]["median"], lat["end_to_end"]["p95"]],
            ])
            write_csv(d / "eval_tracking.csv",
                      list(summary["tracking"].keys()), [list(summary["tracking"].values())])
            write_csv(d / "eval_speed.csv",
                      list(summary["speed"].keys()), [list(summary["speed"].values())])
            write_csv(d / "eval_detections.csv", ["class", "total_detections"],
                      [[k, v] for k, v in sorted(class_totals.items())])

        summary["markdown"] = summary_to_markdown(summary)
        return summary
