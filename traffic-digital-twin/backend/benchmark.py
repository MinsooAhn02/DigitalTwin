"""
benchmark.py — one-command backend/tracker comparison for the report tables.

Give a CCTV *name*; this resolves it to a live ITS HLS stream, then runs the real
pipeline (via evaluate.py) for a fixed wall-time per configuration and assembles the
two comparison tables used in the paper:

  • Latency table  — TensorRT / PyTorch / ONNX  (tracker fixed = OC-SORT)
  • Tracker table  — ByteTrack / OC-SORT / BoT-SORT  (backend fixed = TensorRT)

It is **standalone** (does NOT need the running server): it calls the same ITS fetch the
server uses to find the camera, and `nodelink` for the road bearing. Each configuration runs
in its own subprocess with `YOLO_FORCE_BACKEND` / `TRACKER_TIER` set, because the inference
backend is chosen once at import time.

Usage
-----
    python benchmark.py --name "국도3호선 성남 대원IC"
    python benchmark.py --name "성남 대원" --seconds 60 --mode both
    python benchmark.py --name "용인 원천" --mode tracker --bbox 126.6,127.5,37.0,37.8

Outputs (under logs/bench/): per-combo eval_summary.json, plus benchmark_latency.csv,
benchmark_tracker.csv, benchmark_summary.json, and Markdown + LaTeX rows printed to stdout.
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent
_BENCH_DIR = _BACKEND_DIR / "logs" / "bench"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

BACKEND_LABEL = {
    "tensorrt": "TensorRT FP16",
    "pytorch":  "PyTorch (CUDA)",
    "onnx":     "ONNX Runtime",
}
TIER_LABEL = {
    "cpu":    "ByteTrack",
    "low":    "OC-SORT",
    "medium": "BoT-SORT",
    "high":   "DeepOC-SORT",
}
DEFAULT_BBOX = (126.6, 127.5, 37.0, 37.8)  # wide Gyeonggi (Seongnam/Yongin/Namyangju…)


def _norm(s: str) -> str:
    """Normalize a Korean CCTV name for fuzzy matching (drop spaces/brackets/punct)."""
    return re.sub(r"[\s\[\]()·,_-]+", "", s or "").lower()


def resolve_camera(name: str, bbox: tuple[float, float, float, float]) -> dict:
    """Resolve a CCTV name → {name, url, lat, lon} via the ITS API (server not required)."""
    import main  # reuse the server's tested ITS fetch (import is heavy but standalone)

    minX, maxX, minY, maxY = bbox
    items = asyncio.run(main._fetch_its_cctvs(minX, maxX, minY, maxY))
    cands = []
    for it in items:
        nm = it.get("cctvname", "")
        url = it.get("cctvurl", "")
        try:
            lat = float(it.get("coordy") or 0)
            lon = float(it.get("coordx") or 0)
        except (TypeError, ValueError):
            continue
        if not (nm and url and lat and lon):
            continue
        cands.append({"name": nm, "url": url, "lat": lat, "lon": lon})

    if not cands:
        raise SystemExit(f"No CCTVs returned for bbox {bbox}. Widen --bbox.")

    q = _norm(name)
    # 1) substring match, 2) fall back to closest difflib ratio
    subs = [c for c in cands if q in _norm(c["name"])]
    pool = subs or cands
    best = max(pool, key=lambda c: difflib.SequenceMatcher(None, q, _norm(c["name"])).ratio())
    ratio = difflib.SequenceMatcher(None, q, _norm(best["name"])).ratio()
    if not subs and ratio < 0.4:
        near = sorted(cands, key=lambda c: difflib.SequenceMatcher(
            None, q, _norm(c["name"])).ratio(), reverse=True)[:8]
        print("No confident match. Closest names:")
        for c in near:
            print(f"  - {c['name']}")
        raise SystemExit(f"'{name}' not found (best ratio {ratio:.2f}). Refine --name/--bbox.")
    return best


def get_bearing(lat: float, lon: float, name_hint: str | None) -> float:
    try:
        import nodelink
        snap = nodelink.get_road_snap(lat, lon, name_hint)
        if snap and snap.get("bearing_deg") is not None:
            return float(snap["bearing_deg"])
    except Exception as exc:  # noqa: BLE001
        print(f"  (bearing lookup failed: {exc}; using 0°)")
    return 0.0


def run_combo(backend: str, tier: str, cam: dict, bearing: float, seconds: float) -> dict | None:
    """Run evaluate.py once for (backend, tier); return its eval_summary.json or None."""
    outdir = _BENCH_DIR / f"{backend}_{tier}"
    outdir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["YOLO_FORCE_BACKEND"] = backend
    env["TRACKER_TIER"] = tier
    cmd = [
        sys.executable, str(_BACKEND_DIR / "evaluate.py"),
        "--source", cam["url"],
        "--lat", str(cam["lat"]), "--lon", str(cam["lon"]),
        "--bearing", str(bearing),
        "--seconds", str(seconds),
        "--outdir", str(outdir),
    ]
    label = f"{BACKEND_LABEL.get(backend, backend)} + {TIER_LABEL.get(tier, tier)}"
    print(f"\n{'='*70}\n▶ {label}  (YOLO_FORCE_BACKEND={backend}, TRACKER_TIER={tier})\n{'='*70}")
    proc = subprocess.run(cmd, env=env, cwd=str(_BACKEND_DIR))
    summ = outdir / "eval_summary.json"
    if proc.returncode != 0 or not summ.exists():
        print(f"  ✗ {label} FAILED (rc={proc.returncode}) — skipped.")
        return None
    return json.loads(summ.read_text(encoding="utf-8"))


def _combos_for_mode(mode: str) -> list[tuple[str, str]]:
    latency = [("tensorrt", "low"), ("pytorch", "low"), ("onnx", "low")]
    tracker = [("tensorrt", "cpu"), ("tensorrt", "low"), ("tensorrt", "medium")]
    if mode == "latency":
        return latency
    if mode == "tracker":
        return tracker
    # both: union preserving order, dedup
    seen, out = set(), []
    for c in latency + tracker:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def main() -> None:
    p = argparse.ArgumentParser(description="Backend/tracker benchmark for the report")
    p.add_argument("--name", required=True, help="CCTV name (Korean), fuzzy-matched")
    p.add_argument("--seconds", type=float, default=60.0, help="Wall-time per configuration")
    p.add_argument("--mode", choices=["latency", "tracker", "both"], default="both")
    p.add_argument("--bbox", default=None, help="minX,maxX,minY,maxY (default: wide Gyeonggi)")
    args = p.parse_args()

    bbox = DEFAULT_BBOX
    if args.bbox:
        bbox = tuple(float(x) for x in args.bbox.split(","))  # type: ignore[assignment]

    cam = resolve_camera(args.name, bbox)
    bearing = get_bearing(cam["lat"], cam["lon"], cam["name"])
    print(f"\nCamera: {cam['name']}\n  lat={cam['lat']:.5f} lon={cam['lon']:.5f} bearing={bearing:.1f}°")
    print(f"  {args.seconds:.0f}s per config, mode={args.mode}")

    _BENCH_DIR.mkdir(parents=True, exist_ok=True)
    results: dict[tuple[str, str], dict] = {}
    for backend, tier in _combos_for_mode(args.mode):
        cam = resolve_camera(args.name, bbox)  # re-resolve for a fresh HLS token each run
        summ = run_combo(backend, tier, cam, bearing, args.seconds)
        if summ is not None:
            results[(backend, tier)] = summ

    if not results:
        raise SystemExit("All configurations failed — check GPU/backends/stream.")

    # ── assemble comparison tables ───────────────────────────────────────────
    def lat_row(s: dict) -> tuple[float, float, float]:
        t = s["latency_ms"]["track"]
        return t.get("mean", 0.0), t.get("p95", 0.0), s.get("throughput_fps", 0.0)

    def trk_row(s: dict) -> tuple:
        t = s["latency_ms"]["track"]; tr = s["tracking"]
        return (t.get("mean", 0.0), t.get("p95", 0.0), s.get("throughput_fps", 0.0),
                tr.get("unique_tracks", 0), tr.get("id_appearances", 0),
                tr.get("mean_lifetime_frames", 0.0))

    out_lines: list[str] = []

    if args.mode in ("latency", "both"):
        out_lines.append("\n### Latency table (tracker fixed = OC-SORT)\n")
        out_lines.append("| Backend | Mean (ms) | p95 (ms) | FPS |")
        out_lines.append("|---|---|---|---|")
        latex_l = []
        csv_l = [["backend", "mean_ms", "p95_ms", "fps"]]
        for be in ("tensorrt", "pytorch", "onnx"):
            s = results.get((be, "low"))
            if not s:
                continue
            m, p, f = lat_row(s)
            out_lines.append(f"| {BACKEND_LABEL[be]} | {m} | {p} | {f} |")
            latex_l.append(f"{BACKEND_LABEL[be]:<14}& {m:5.1f} & {p:5.1f} & {f:6.1f} \\\\")
            csv_l.append([be, m, p, f])
        (_BENCH_DIR / "benchmark_latency.csv").write_text(
            "\n".join(",".join(str(c) for c in r) for r in csv_l), encoding="utf-8")
        out_lines.append("\nLaTeX rows (tab:latency):\n```\n" + "\n".join(latex_l) + "\n```")

    if args.mode in ("tracker", "both"):
        out_lines.append("\n### Tracker table (backend fixed = TensorRT)\n")
        out_lines.append("| Tracker | Mean (ms) | p95 (ms) | FPS | IDs | ID ev. | Life (fr.) |")
        out_lines.append("|---|---|---|---|---|---|---|")
        latex_t = []
        csv_t = [["tracker", "mean_ms", "p95_ms", "fps", "ids", "id_events", "life_frames"]]
        for tier in ("cpu", "low", "medium"):
            s = results.get(("tensorrt", tier))
            if not s:
                continue
            m, p, f, ids, ev, life = trk_row(s)
            out_lines.append(f"| {TIER_LABEL[tier]} | {m} | {p} | {f} | {ids} | {ev} | {life} |")
            latex_t.append(f"{TIER_LABEL[tier]:<9}& {m:5.1f} & {p:5.1f} & {f:6.1f} "
                           f"& {ids} & {ev} & {life} \\\\")
            csv_t.append([tier, m, p, f, ids, ev, life])
        (_BENCH_DIR / "benchmark_tracker.csv").write_text(
            "\n".join(",".join(str(c) for c in r) for r in csv_t), encoding="utf-8")
        out_lines.append("\nLaTeX rows (tab:tracker):\n```\n" + "\n".join(latex_t) + "\n```")

    (_BENCH_DIR / "benchmark_summary.json").write_text(
        json.dumps({f"{b}_{t}": s for (b, t), s in results.items()},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    report = "\n".join(out_lines)
    print("\n" + "=" * 70 + "\nBENCHMARK RESULTS\n" + "=" * 70 + report)
    print(f"\nWrote CSV/JSON to: {_BENCH_DIR}")


if __name__ == "__main__":
    main()
