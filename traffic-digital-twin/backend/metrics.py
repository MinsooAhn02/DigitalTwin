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
            # Step 1 diagnostics
            self._depth_obs: dict[int, list[tuple[float, float]]] = defaultdict(list)
            self._anchor_residual_px: list[float] = []

    def set_context(self, source: str = "", backend: str = "", tracker: str = "") -> None:
        with self._lock:
            if source:
                self._source = source
            if backend:
                self._backend = backend
            if tracker:
                self._tracker = tracker

    def add_speed_obs(self, tid: int, row_y: float, raw_kph: float) -> None:
        """analytics._speed가 raw 속도를 계산할 때 (track_id, bbox_bottom_y, raw_kph)를 누적.

        depth-invariance CV 계산에 사용: 같은 차량이 프레임 내 다른 깊이에서 같은 속도로
        측정되는지 검사 — CV가 낮을수록 depth 보정이 정확함을 의미.
        """
        if raw_kph <= 0:
            return
        with self._lock:
            self._depth_obs[int(tid)].append((float(row_y), float(raw_kph)))

    def set_anchor_residual(self, residual_px: float) -> None:
        """camera_pose solver가 앵커 재투영 오차(px)를 기록."""
        with self._lock:
            self._anchor_residual_px.append(float(residual_px))

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

    def _compute_depth_invariance(self) -> dict:
        """depth_obs에서 depth-invariance CV를 계산한다 (lock 밖에서 호출할 것).

        각 트랙을 row_y 기준 3개 구간으로 나누어 구간별 평균 raw_kph의 CV를 구한다.
        2개 이상의 구간에 ≥2개 샘플이 있고 총 ≥6개 샘플인 트랙만 사용.
        CV 중앙값이 낮을수록 depth 보정이 정확함.
        """
        cvs: list[float] = []
        for obs in self._depth_obs.values():
            if len(obs) < 6:
                continue
            rows = np.array([o[0] for o in obs])
            spds = np.array([o[1] for o in obs])
            q33, q67 = np.percentile(rows, [33, 67])
            buckets: list[np.ndarray] = [
                spds[rows <= q33],
                spds[(rows > q33) & (rows <= q67)],
                spds[rows > q67],
            ]
            bin_means = [b.mean() for b in buckets if len(b) >= 2]
            if len(bin_means) < 2:
                continue
            bm = np.array(bin_means)
            mean_v = bm.mean()
            if mean_v > 0:
                cvs.append(float(bm.std() / mean_v))
        if not cvs:
            return {"qualifying_tracks": 0, "median_cv": None, "mean_cv": None}
        return {
            "qualifying_tracks": len(cvs),
            "median_cv": round(float(np.median(cvs)), 4),
            "mean_cv": round(float(np.mean(cvs)), 4),
        }

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
            depth_inv = self._compute_depth_invariance()
            anchor_resid = stats(self._anchor_residual_px)
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
                "depth_invariance": depth_inv,
                "anchor_residual_px": anchor_resid,
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
            write_csv(d / "eval_depth_inv.csv",
                      ["qualifying_tracks", "median_cv", "mean_cv"],
                      [[depth_inv["qualifying_tracks"],
                        depth_inv["median_cv"] or "",
                        depth_inv["mean_cv"] or ""]])
            if anchor_resid["n"] > 0:
                write_csv(d / "eval_anchor_residual.csv",
                          list(anchor_resid.keys()), [list(anchor_resid.values())])

        summary["markdown"] = summary_to_markdown(summary)
        return summary
