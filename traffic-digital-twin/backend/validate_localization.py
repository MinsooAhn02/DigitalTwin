"""
validate_localization.py
========================
Satellite-referenced localization accuracy harness for the traffic digital twin.

PURPOSE
-------
The system derives every vehicle's GPS from map geometry, with no on-vehicle
ground truth. This harness supplies ground truth WITHOUT instrumented vehicles by
using fixed road landmarks whose true (lat, lon) is read from high-resolution
satellite/aerial imagery. For each landmark we compare:

    system output  : PerspectiveTransformer.pixel_to_gps(u, v)   (or the curved path)
    ground truth   : (lat, lon) read off the satellite image

and report the localization error, decomposed into the along-road (longitudinal)
and lateral (off-road) components -- the two error modes the paper separates.

This mirrors the methodology of Revaud & Humenberger's `evaluate_homographies`
(which scores the homography against ground-truth references, isolating geometry
from detector/tracker noise), so the numbers are comparable in spirit to that SOTA.

WHAT YOU PROVIDE  (per camera, in a JSON file -- see SCHEMA below)
-----------------
  * camera id and the road centreline used (so we can decompose along/lateral)
  * a list of correspondences, each with:
      - pixel  (u, v)        : clicked location of a landmark in the CCTV frame
      - gps    (lat, lon)    : same landmark read from the satellite image
  Optionally a second method's output to compare (flat vs curved).

HOW TO COLLECT (one-time, per camera, ~10-15 min)
-----------------
  1. Open the CCTV still and the satellite image (e.g. national orthophoto / a
     mapping service) side by side at the same location.
  2. Pick 6-12 landmarks visible in BOTH: lane-dash ends, stop lines, arrows,
     manhole covers, painted symbols. Spread them near->far and left->right.
  3. For each: record the pixel (u,v) in the CCTV frame and the (lat,lon) from
     the satellite image. Put them in the JSON.
  4. IMPORTANT: do NOT reuse the 4 calibration points as test points -- that
     would measure fit, not generalization. Use independent landmarks.

OUTPUT
------
  * per-landmark error table (total / along / lateral, in metres)
  * per-camera summary (MAE, RMSE, p95), split by near/far depth
  * if two methods provided: paired comparison (flat vs curved)
  * a CSV for the paper + a JSON summary

This script is self-contained: it re-implements only the geometry needed to score
points; it does NOT import the backend, so it runs anywhere. If you prefer to score
the LIVE system, fill in `system_gps` per correspondence by calling your own
transformer (instructions in the SCHEMA comment).
"""

from __future__ import annotations
import json
import math
import sys
import csv
from dataclasses import dataclass


# ----------------------------------------------------------------------------
# Geometry helpers (equirectangular, same constants as the backend)
# ----------------------------------------------------------------------------
R_LAT = 110574.0

def lon_scale(lat: float) -> float:
    return 111320.0 * math.cos(math.radians(lat))

def gps_dist_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Distance in metres between two (lat, lon) points."""
    mlat = (a[0] + b[0]) / 2.0
    dlat = (a[0] - b[0]) * R_LAT
    dlon = (a[1] - b[1]) * lon_scale(mlat)
    return math.hypot(dlat, dlon)


def decompose_error(
    sys_gps: tuple[float, float],
    gt_gps: tuple[float, float],
    centreline: list[list[float]] | None,
) -> tuple[float, float, float]:
    """Return (total, along, lateral) error in metres.

    along/lateral are computed against the local road bearing at the ground-truth
    point's nearest centreline segment. If no centreline is given, lateral=total
    and along=0 (caller should treat them as undecomposed).
    """
    total = gps_dist_m(sys_gps, gt_gps)
    if not centreline or len(centreline) < 2:
        return total, float("nan"), float("nan")

    # error vector in local ENU metres (east, north), centred at GT
    lat0 = gt_gps[0]
    Rlon = lon_scale(lat0)
    ex = (sys_gps[1] - gt_gps[1]) * Rlon      # east
    ey = (sys_gps[0] - gt_gps[0]) * R_LAT     # north

    # local road bearing at nearest centreline segment to GT
    b = local_bearing(gt_gps, centreline)     # radians, atan2(east, north)
    # road forward unit (east, north) = (sin b, cos b); right normal = (cos b, -sin b)
    fwd = (math.sin(b), math.cos(b))
    rgt = (math.cos(b), -math.sin(b))
    along = abs(ex * fwd[0] + ey * fwd[1])
    lateral = abs(ex * rgt[0] + ey * rgt[1])
    return total, along, lateral


def local_bearing(p: tuple[float, float], centreline: list[list[float]]) -> float:
    """Road bearing (rad, atan2(east,north)) at the centreline segment nearest p."""
    best_d2 = float("inf")
    best_b = 0.0
    lat0 = p[0]
    Rlon = lon_scale(lat0)
    for i in range(len(centreline) - 1):
        f = centreline[i]; t = centreline[i + 1]
        # segment midpoint distance (cheap nearest-segment proxy)
        mx = (f[1] + t[1]) / 2.0
        my = (f[0] + t[0]) / 2.0
        d2 = ((p[1] - mx) * Rlon) ** 2 + ((p[0] - my) * R_LAT) ** 2
        if d2 < best_d2:
            best_d2 = d2
            de = (t[1] - f[1]) * Rlon
            dn = (t[0] - f[0]) * R_LAT
            best_b = math.atan2(de, dn)
    return best_b


# ----------------------------------------------------------------------------
# Scoring
# ----------------------------------------------------------------------------
@dataclass
class Result:
    cam_id: str
    method: str
    n: int
    mae: float
    rmse: float
    p95: float
    mae_lat: float
    mae_along: float
    near_mae: float
    far_mae: float


def percentile(xs: list[float], q: float) -> float:
    if not xs:
        return float("nan")
    s = sorted(xs)
    k = (len(s) - 1) * q
    lo = int(math.floor(k)); hi = int(math.ceil(k))
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def score_camera(cam: dict, method_key: str) -> tuple[Result, list[dict]]:
    """Score one camera's correspondences for a given method key.

    method_key selects which system output to read from each correspondence:
      'system_gps'        -> single method
      'flat_gps'/'curved_gps' -> for the ablation comparison
    """
    cam_id = cam.get("cam_id", "?")
    centreline = cam.get("centreline")           # [[lat,lon],...] or None
    cam_gps = cam.get("camera_gps")              # [lat,lon] for near/far split
    rows = []
    totals, lats, alongs = [], [], []
    near_errs, far_errs = [], []

    for c in cam["correspondences"]:
        if method_key not in c:
            continue
        gt = tuple(c["gps"])
        sysg = tuple(c[method_key])
        total, along, lateral = decompose_error(sysg, gt, centreline)
        totals.append(total)
        if not math.isnan(lateral):
            lats.append(lateral); alongs.append(along)

        depth = gps_dist_m(tuple(cam_gps), gt) if cam_gps else float("nan")
        if not math.isnan(depth):
            (near_errs if depth <= 50.0 else far_errs).append(total)

        rows.append({
            "cam_id": cam_id, "method": method_key,
            "u": c["pixel"][0], "v": c["pixel"][1],
            "gt_lat": gt[0], "gt_lon": gt[1],
            "sys_lat": sysg[0], "sys_lon": sysg[1],
            "depth_m": round(depth, 1) if not math.isnan(depth) else "",
            "err_total_m": round(total, 2),
            "err_along_m": round(along, 2) if not math.isnan(along) else "",
            "err_lateral_m": round(lateral, 2) if not math.isnan(lateral) else "",
        })

    def _mae(x): return sum(x) / len(x) if x else float("nan")
    def _rmse(x): return math.sqrt(sum(v*v for v in x) / len(x)) if x else float("nan")

    res = Result(
        cam_id=cam_id, method=method_key, n=len(totals),
        mae=_mae(totals), rmse=_rmse(totals), p95=percentile(totals, 0.95),
        mae_lat=_mae(lats), mae_along=_mae(alongs),
        near_mae=_mae(near_errs), far_mae=_mae(far_errs),
    )
    return res, rows


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main(path: str):
    with open(path) as f:
        data = json.load(f)
    cameras = data["cameras"] if "cameras" in data else [data]

    # auto-detect which method keys are present
    sample = cameras[0]["correspondences"][0]
    method_keys = [k for k in ("system_gps", "flat_gps", "curved_gps") if k in sample]
    if not method_keys:
        print("ERROR: no system output keys found "
              "(expected 'system_gps', or 'flat_gps'+'curved_gps').")
        sys.exit(1)

    all_rows = []
    results: list[Result] = []
    for cam in cameras:
        for mk in method_keys:
            res, rows = score_camera(cam, mk)
            results.append(res); all_rows.extend(rows)

    # ---- print summary ----
    print(f"\n{'cam':>8} {'method':>11} {'n':>3} {'MAE':>7} {'RMSE':>7} "
          f"{'p95':>7} {'lat':>7} {'along':>7} {'near':>7} {'far':>7}  (metres)")
    print("-" * 86)
    for r in results:
        print(f"{r.cam_id:>8} {r.method:>11} {r.n:>3} "
              f"{r.mae:7.2f} {r.rmse:7.2f} {r.p95:7.2f} "
              f"{r.mae_lat:7.2f} {r.mae_along:7.2f} {r.near_mae:7.2f} {r.far_mae:7.2f}")

    # ---- paired flat-vs-curved comparison, if available ----
    if {"flat_gps", "curved_gps"}.issubset(set(method_keys)):
        print("\nAblation (flat vs curved), lateral MAE = off-road drift:")
        by_cam = {}
        for r in results:
            by_cam.setdefault(r.cam_id, {})[r.method] = r
        print(f"{'cam':>8} {'flat lat':>9} {'curved lat':>11} {'Δ improve':>10}")
        for cid, d in by_cam.items():
            if "flat_gps" in d and "curved_gps" in d:
                fl = d["flat_gps"].mae_lat; cu = d["curved_gps"].mae_lat
                print(f"{cid:>8} {fl:9.2f} {cu:11.2f} {fl-cu:+10.2f}")

    # ---- write CSV + JSON ----
    with open("validation_rows.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(all_rows[0].keys()))
        w.writeheader(); w.writerows(all_rows)
    with open("validation_summary.json", "w") as f:
        json.dump([r.__dict__ for r in results], f, indent=2)
    print("\nWrote validation_rows.csv and validation_summary.json")


# ----------------------------------------------------------------------------
# SCHEMA + example (run with no args to emit a template)
# ----------------------------------------------------------------------------
TEMPLATE = {
    "cameras": [
        {
            "cam_id": "national_route_45_yongin",
            "camera_gps": [37.20000, 127.20000],
            "centreline": [
                [37.20010, 127.20000],
                [37.20050, 127.20030],
                [37.20090, 127.20075]
            ],
            "_comment": (
                "Fill 'correspondences' with independent landmarks (NOT the 4 "
                "calibration points). For each landmark: 'pixel' is (u,v) clicked "
                "in the CCTV frame; 'gps' is (lat,lon) read from the satellite "
                "image. Provide EITHER 'system_gps' (single method) OR both "
                "'flat_gps' and 'curved_gps' for the ablation. To get those, feed "
                "the pixel to your PerspectiveTransformer: flat = the H_gps path, "
                "curved = pixel_to_gps with road_pts set."
            ),
            "correspondences": [
                {
                    "pixel": [512, 600],
                    "gps": [37.20015, 127.20005],
                    "flat_gps": [37.20018, 127.20004],
                    "curved_gps": [37.20015, 127.20005]
                },
                {
                    "pixel": [330, 470],
                    "gps": [37.20040, 127.20025],
                    "flat_gps": [37.20047, 127.20021],
                    "curved_gps": [37.20041, 127.20025]
                }
            ]
        }
    ]
}

if __name__ == "__main__":
    if len(sys.argv) < 2:
        with open("validation_template.json", "w") as f:
            json.dump(TEMPLATE, f, indent=2)
        print("No input given. Wrote validation_template.json -- fill it in and run:")
        print("    python validate_localization.py validation_template.json")
    else:
        main(sys.argv[1])
