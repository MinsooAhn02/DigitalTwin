"""
nodelink.py — Runtime query module for the national standard node-link SQLite DB.

Usage:
    from nodelink import get_road_info, get_links_near, get_nodes_near

The DB is built once by:
    python scripts/build_nodelink_db.py
"""

from __future__ import annotations

import json
import sqlite3
import math
import threading
from pathlib import Path
from typing import TypedDict

_DB_PATH = Path(__file__).resolve().parent.parent.parent / "node-link-data" / "nodelink.sqlite"

# degrees per km (approximate, Korea latitude ~37°)
_DEG_PER_KM_LAT = 1.0 / 110.574
_DEG_PER_KM_LON = 1.0 / (111.320 * math.cos(math.radians(37.0)))


class NodeInfo(TypedDict):
    node_id: str
    node_type: str
    node_name: str
    lat: float
    lon: float
    dist_m: float


class LinkInfo(TypedDict):
    link_id: str
    f_node: str
    t_node: str
    lanes: int
    max_spd: int
    road_rank: str
    road_name: str
    length: float
    cx_lat: float
    cx_lon: float
    dist_m: float
    bearing_deg: float | None  # F_NODE → T_NODE 방향 (0=북, 시계방향)
    f_lat: float | None
    f_lon: float | None
    t_lat: float | None
    t_lon: float | None


def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Compass bearing from (lat1,lon1) to (lat2,lon2). 0=N, 90=E, clockwise."""
    dlon = math.radians(lon2 - lon1)
    lat1r, lat2r = math.radians(lat1), math.radians(lat2)
    x = math.sin(dlon) * math.cos(lat2r)
    y = math.cos(lat1r) * math.sin(lat2r) - math.sin(lat1r) * math.cos(lat2r) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def _dist_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Approximate Euclidean distance in metres (accurate enough within ~5 km)."""
    dlat = (lat2 - lat1) * 110574.0
    dlon = (lon2 - lon1) * 111320.0 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot(dlat, dlon)


_tl = threading.local()

def _get_conn() -> sqlite3.Connection:
    # asyncio.to_thread으로 여러 스레드가 동시에 호출되므로 스레드당 1개의 커넥션 유지.
    # lru_cache 단일 커넥션은 동시 접근 시 sqlite3.InterfaceError 유발.
    if not hasattr(_tl, "conn"):
        if not _DB_PATH.exists():
            raise FileNotFoundError(
                f"nodelink.sqlite not found at {_DB_PATH}.\n"
                "Run:  python scripts/build_nodelink_db.py"
            )
        con = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA query_only = 1")
        _tl.conn = con
    return _tl.conn


def _bbox(lat: float, lon: float, radius_km: float) -> tuple[float, float, float, float]:
    dlat = radius_km * _DEG_PER_KM_LAT
    dlon = radius_km * _DEG_PER_KM_LON
    return lat - dlat, lat + dlat, lon - dlon, lon + dlon


def get_nodes_near(lat: float, lon: float, radius_km: float = 0.3) -> list[NodeInfo]:
    """Return nodes within radius_km of (lat, lon), sorted by distance."""
    min_lat, max_lat, min_lon, max_lon = _bbox(lat, lon, radius_km)
    con = _get_conn()
    rows = con.execute(
        """
        SELECT n.node_id, n.node_type, n.node_name, n.lat, n.lon
        FROM nodes_rtree r
        JOIN nodes n ON n.id = r.id
        WHERE r.min_lat >= ? AND r.max_lat <= ?
          AND r.min_lon >= ? AND r.max_lon <= ?
        """,
        (min_lat, max_lat, min_lon, max_lon),
    ).fetchall()

    result: list[NodeInfo] = []
    for row in rows:
        d = _dist_m(lat, lon, row["lat"], row["lon"])
        if d <= radius_km * 1000:
            result.append(NodeInfo(
                node_id=row["node_id"],
                node_type=row["node_type"],
                node_name=row["node_name"],
                lat=row["lat"],
                lon=row["lon"],
                dist_m=round(d, 1),
            ))
    result.sort(key=lambda x: x["dist_m"])
    return result


def get_links_near(lat: float, lon: float, radius_km: float = 0.3) -> list[LinkInfo]:
    """Return links within radius_km of (lat, lon), sorted by closest segment distance."""
    min_lat, max_lat, min_lon, max_lon = _bbox(lat, lon, radius_km)
    con = _get_conn()
    # INTERSECTS: find any link whose bbox overlaps the search bbox
    # (opposite of the buggy CONTAINS query r.min_lat >= min AND r.max_lat <= max)
    rows = con.execute(
        """
        SELECT l.link_id, l.f_node, l.t_node, l.lanes, l.max_spd,
               l.road_rank, l.road_name, l.length, l.cx_lat, l.cx_lon,
               nf.lat AS f_lat, nf.lon AS f_lon,
               nt.lat AS t_lat, nt.lon AS t_lon
        FROM links_rtree r
        JOIN links l ON l.id = r.id
        LEFT JOIN nodes nf ON nf.node_id = l.f_node
        LEFT JOIN nodes nt ON nt.node_id = l.t_node
        WHERE r.max_lat >= ? AND r.min_lat <= ?
          AND r.max_lon >= ? AND r.min_lon <= ?
        """,
        (min_lat, max_lat, min_lon, max_lon),
    ).fetchall()

    R_lat_m = 110574.0
    R_lon_m = 111320.0 * math.cos(math.radians(lat))
    radius_m = radius_km * 1000.0

    result: list[LinkInfo] = []
    for row in rows:
        f_lat, f_lon = row["f_lat"], row["f_lon"]
        t_lat, t_lon = row["t_lat"], row["t_lon"]

        # Compute closest distance to the segment (not just to centre-point)
        if None not in (f_lat, f_lon, t_lat, t_lon):
            px_m = (lon - f_lon) * R_lon_m
            py_m = (lat - f_lat) * R_lat_m
            bx_m = (t_lon - f_lon) * R_lon_m
            by_m = (t_lat - f_lat) * R_lat_m
            sx_m, sy_m = _project_to_segment(px_m, py_m, bx_m, by_m)
            d = math.hypot(px_m - sx_m, py_m - sy_m)
        else:
            d = _dist_m(lat, lon, row["cx_lat"], row["cx_lon"])

        if d > radius_m:
            continue

        bearing = (
            _bearing_deg(f_lat, f_lon, t_lat, t_lon)
            if (f_lat and f_lon and t_lat and t_lon) else None
        )
        result.append(LinkInfo(
            link_id=row["link_id"],
            f_node=row["f_node"],
            t_node=row["t_node"],
            lanes=row["lanes"],
            max_spd=row["max_spd"],
            road_rank=row["road_rank"],
            road_name=row["road_name"],
            length=row["length"],
            cx_lat=row["cx_lat"],
            cx_lon=row["cx_lon"],
            dist_m=round(d, 1),
            bearing_deg=round(bearing, 1) if bearing is not None else None,
            f_lat=float(f_lat) if f_lat is not None else None,
            f_lon=float(f_lon) if f_lon is not None else None,
            t_lat=float(t_lat) if t_lat is not None else None,
            t_lon=float(t_lon) if t_lon is not None else None,
        ))
    result.sort(key=lambda x: x["dist_m"])
    return result


def _road_name_matches(link_road_name: str, hint: str) -> bool:
    """링크 road_name이 힌트와 일치하는지 확인 (공백/호선 표기 차이 허용)."""
    # 숫자만 추출해서 비교: "국도 1호선" == "국도1호선" == "1호선"
    def digits(s: str) -> str:
        return "".join(c for c in s if c.isdigit())

    ln = link_road_name.replace(" ", "").lower()
    hn = hint.replace(" ", "").lower()

    # 도로 종류 키워드 일치
    for kw in ("국도", "지방도", "고속도로", "특별시도", "광역시도"):
        if kw in hn and kw not in ln:
            return False
        if kw in hn and kw in ln:
            return digits(ln) == digits(hn)

    # 키워드 없이 숫자(호선 번호)만 비교
    return digits(ln) == digits(hn) and bool(digits(hn))


def _project_to_segment(
    px_m: float, py_m: float,
    bx_m: float, by_m: float,
) -> tuple[float, float]:
    """Clamp-project point (px,py) onto segment (0,0)→(bx,by), metres relative to F_NODE."""
    seg_sq = bx_m * bx_m + by_m * by_m
    if seg_sq < 1e-6:
        return 0.0, 0.0
    t = max(0.0, min(1.0, (px_m * bx_m + py_m * by_m) / seg_sq))
    return t * bx_m, t * by_m


def _snap_to_polyline(
    lat: float, lon: float,
    pts: list[list[float]],
) -> tuple[float, float, float, float, int]:
    """Snap (lat, lon) to the closest point on a road polyline.

    pts: [[lat0,lon0], [lat1,lon1], ...]  (ordered shape points)
    Returns (snap_lat, snap_lon, bearing_deg, dist_m, seg_idx).
    bearing_deg is the local tangent direction of the road at the snap segment.
    seg_idx is the index of the segment (pts[seg_idx]→pts[seg_idx+1]) snap falls on.
    """
    R_lat_m = 110574.0
    R_lon_m = 111320.0 * math.cos(math.radians(lat))

    best_d       = float("inf")
    best_snap_lat = pts[0][0]
    best_snap_lon = pts[0][1]
    best_bearing  = _bearing_deg(pts[0][0], pts[0][1], pts[-1][0], pts[-1][1])
    best_seg_idx  = 0

    for i in range(len(pts) - 1):
        f_lat, f_lon = pts[i][0], pts[i][1]
        t_lat, t_lon = pts[i + 1][0], pts[i + 1][1]

        px_m = (lon - f_lon) * R_lon_m
        py_m = (lat - f_lat) * R_lat_m
        bx_m = (t_lon - f_lon) * R_lon_m
        by_m = (t_lat - f_lat) * R_lat_m

        sx_m, sy_m = _project_to_segment(px_m, py_m, bx_m, by_m)
        d = math.hypot(px_m - sx_m, py_m - sy_m)

        if d < best_d:
            best_d        = d
            best_snap_lat = f_lat + sy_m / R_lat_m
            best_snap_lon = f_lon + sx_m / R_lon_m
            best_bearing  = _bearing_deg(f_lat, f_lon, t_lat, t_lon)
            best_seg_idx  = i

    return best_snap_lat, best_snap_lon, best_bearing, best_d, best_seg_idx


def _subsample_pts(pts: list[list[float]], max_pts: int = 10) -> list[list[float]]:
    """Subsample a polyline to at most max_pts points using uniform cumulative-distance
    spacing. Always preserves first and last points."""
    if len(pts) <= max_pts:
        return pts
    R_lat_m = 110574.0
    cum = [0.0]
    for i in range(1, len(pts)):
        R_lon_m = 111320.0 * math.cos(math.radians(pts[i - 1][0]))
        cum.append(cum[-1] + math.hypot(
            (pts[i][0] - pts[i - 1][0]) * R_lat_m,
            (pts[i][1] - pts[i - 1][1]) * R_lon_m,
        ))
    total = cum[-1]
    result = [pts[0]]
    for k in range(1, max_pts - 1):
        target = total * k / (max_pts - 1)
        # Find the point in pts closest to target cumulative distance
        best_i = min(range(len(cum)), key=lambda i: abs(cum[i] - target))
        if pts[best_i] != result[-1]:
            result.append(pts[best_i])
    if pts[-1] != result[-1]:
        result.append(pts[-1])
    return result


def _road_corridor_pts(
    pts: list[list[float]],
    snap_lat: float,
    snap_lon: float,
    snap_seg_idx: int,
    fwd_m: float = 250.0,
    bwd_m: float = 250.0,
) -> tuple[list[list[float]], float]:
    """Extract road centerline points within fwd_m forward / bwd_m backward of snap.

    The polyline is ordered F→T. Snap is inserted at its exact position.
    Returns (corridor_pts, snap_along_m):
      corridor_pts  – [[lat, lon], ...] in F→T order, spanning bwd_m+fwd_m along road
      snap_along_m  – cumulative distance from corridor_pts[0] to snap (meters)
    """
    R_lat_m = 110574.0

    def _seg_m(a: list[float], b: list[float]) -> float:
        R_lon_m = 111320.0 * math.cos(math.radians(a[0]))
        return math.hypot((b[0] - a[0]) * R_lat_m, (b[1] - a[1]) * R_lon_m)

    snap_pt = [snap_lat, snap_lon]
    # Insert snap between pts[snap_seg_idx] and pts[snap_seg_idx+1]
    aug = pts[: snap_seg_idx + 1] + [snap_pt] + pts[snap_seg_idx + 1 :]
    snap_idx = snap_seg_idx + 1  # position of snap in aug

    # Walk backward (toward F-node, decreasing index) collecting up to bwd_m
    bwd_pts: list[list[float]] = []
    bwd_acc = 0.0
    for i in range(snap_idx - 1, -1, -1):
        d = _seg_m(aug[i], aug[i + 1])
        remaining = bwd_m - bwd_acc
        if d >= remaining:
            frac = remaining / max(d, 1e-9)
            bwd_pts.append([
                aug[i + 1][0] + frac * (aug[i][0] - aug[i + 1][0]),
                aug[i + 1][1] + frac * (aug[i][1] - aug[i + 1][1]),
            ])
            bwd_acc = bwd_m
            break
        bwd_pts.append(aug[i])
        bwd_acc += d

    # Walk forward (toward T-node, increasing index) collecting up to fwd_m
    fwd_pts: list[list[float]] = []
    fwd_acc = 0.0
    for i in range(snap_idx, len(aug) - 1):
        d = _seg_m(aug[i], aug[i + 1])
        remaining = fwd_m - fwd_acc
        if d >= remaining:
            frac = remaining / max(d, 1e-9)
            fwd_pts.append([
                aug[i][0] + frac * (aug[i + 1][0] - aug[i][0]),
                aug[i][1] + frac * (aug[i + 1][1] - aug[i][1]),
            ])
            break
        fwd_pts.append(aug[i + 1])
        fwd_acc += d

    # Combine in F→T order: backward-end → … → snap → … → forward-end
    corridor = list(reversed(bwd_pts)) + [snap_pt] + fwd_pts

    # Limit point density so the perpendicular-offset polygon in the frontend
    # doesn't self-intersect at curves. 10 points ≈ one per 30 m on a ±150 m corridor —
    # enough to show the road curve without adjacent offsets crossing each other.
    if len(corridor) > 20:
        corridor = _subsample_pts(corridor, max_pts=10)

    return corridor, round(bwd_acc, 1)


def _extend_pts_with_adjacent(
    pts: list[list[float]],
    link: LinkInfo,
) -> list[list[float]]:
    """Extend a link's shape_pts by stitching one adjacent link at each end.

    NodeLink links end at intersections.  When the camera snap is close to a
    link boundary the corridor (±150 m) runs out of pts before covering the
    full near/far range → the FOV polygon looks nearly square.

    This function:
      • Finds the link whose F_NODE = current T_NODE  (forward continuation)
      • Finds the link whose T_NODE = current F_NODE  (backward continuation)
    filtering by same road_name + bearing ±60°, then stitches their shape_pts
    onto the primary pts so that _road_corridor_pts has enough vertices in both
    directions.

    Only one hop in each direction (enough for the typical ±150 m window).
    """
    if not (link.get("f_node") or link.get("t_node")):
        return pts

    primary_bearing = link["bearing_deg"] or 0.0
    p_name = (link["road_name"] or "").strip()

    def _best_adjacent_pts(rows: list) -> list[list[float]] | None:
        """Pick the row with same road_name + closest bearing ≤ 60°."""
        best_pts_: list[list[float]] | None = None
        best_diff_ = float("inf")
        for row in rows:
            l_name = (row["road_name"] or "").strip()
            if p_name and l_name and p_name != l_name:
                continue
            if row["f_lat"] is not None and row["t_lat"] is not None:
                lk_b = _bearing_deg(
                    float(row["f_lat"]), float(row["f_lon"]),
                    float(row["t_lat"]), float(row["t_lon"]),
                )
                diff = abs((lk_b - primary_bearing + 180) % 360 - 180)
                if diff > 60:
                    continue
                if diff < best_diff_:
                    best_diff_ = diff
                    # shape_pts 우선, 없으면 F→T 2점 fallback
                    sp = row["shape_pts"] if "shape_pts" in row.keys() else None
                    if sp:
                        try:
                            npts = json.loads(sp)
                            if len(npts) >= 2:
                                best_pts_ = npts
                                continue
                        except Exception:
                            pass
                    best_pts_ = [
                        [float(row["f_lat"]), float(row["f_lon"])],
                        [float(row["t_lat"]), float(row["t_lon"])],
                    ]
        return best_pts_

    try:
        con = _get_conn()

        # ── Forward extension: link whose F_NODE = current T_NODE ──────────
        if link.get("t_node"):
            rows = con.execute(
                """SELECT l.road_name, l.shape_pts,
                          nf.lat AS f_lat, nf.lon AS f_lon,
                          nt.lat AS t_lat, nt.lon AS t_lon
                   FROM links l
                   LEFT JOIN nodes nf ON nf.node_id = l.f_node
                   LEFT JOIN nodes nt ON nt.node_id = l.t_node
                   WHERE l.f_node = ? AND l.link_id != ?
                   LIMIT 8""",
                (link["t_node"], link["link_id"]),
            ).fetchall()
            next_pts = _best_adjacent_pts(rows)
            if next_pts:
                # next_pts[0] == pts[-1] (shared node) — skip it
                pts = pts + next_pts[1:]

        # ── Backward extension: link whose T_NODE = current F_NODE ─────────
        if link.get("f_node"):
            rows = con.execute(
                """SELECT l.road_name, l.shape_pts,
                          nf.lat AS f_lat, nf.lon AS f_lon,
                          nt.lat AS t_lat, nt.lon AS t_lon
                   FROM links l
                   LEFT JOIN nodes nf ON nf.node_id = l.f_node
                   LEFT JOIN nodes nt ON nt.node_id = l.t_node
                   WHERE l.t_node = ? AND l.link_id != ?
                   LIMIT 8""",
                (link["f_node"], link["link_id"]),
            ).fetchall()
            prev_pts = _best_adjacent_pts(rows)
            if prev_pts:
                # prev_pts[-1] == pts[0] (shared node) — skip it
                pts = prev_pts[:-1] + pts

    except Exception:
        pass

    return pts


def _find_reverse_link(
    primary: LinkInfo,
    links: list[LinkInfo],
    primary_bearing: float,
) -> "LinkInfo | None":
    """Find the opposite-direction carriageway link for the same road.

    For bidirectional roads stored as two one-way links (standard NodeLink format),
    this returns the matching reverse link so we can compute the true road center.
    Returns None if no plausible reverse link is found.
    """
    target_bearing = (primary_bearing + 180.0) % 360.0

    best: "LinkInfo | None" = None
    best_dist = float("inf")

    for lk in links:
        if lk["link_id"] == primary["link_id"]:
            continue

        # Same road name required (when both have names)
        p_name = (primary["road_name"] or "").strip()
        l_name = (lk["road_name"] or "").strip()
        if p_name and l_name:
            if p_name != l_name:
                continue
        elif p_name or l_name:
            # One has a name and the other doesn't → probably different road
            continue
        else:
            # Both unnamed → fall back to road_rank match
            if primary["road_rank"] != lk["road_rank"]:
                continue

        # Must be roughly opposite direction ±60°
        # Using link["bearing_deg"] (overall F→T) for both sides keeps the comparison
        # stable across different camera positions on the same road.
        if lk["bearing_deg"] is None:
            continue
        diff = abs((lk["bearing_deg"] - target_bearing + 180.0) % 360.0 - 180.0)
        if diff > 60.0:
            continue

        # Prefer the link closest to the query point
        if lk["dist_m"] < best_dist:
            best_dist = lk["dist_m"]
            best = lk

    return best


def _snap_for_link(link: LinkInfo, lat: float, lon: float) -> tuple[float, float]:
    """Return the snap point (lat, lon) for *link* closest to (lat, lon)."""
    try:
        con = _get_conn()
        row = con.execute(
            "SELECT shape_pts FROM links WHERE link_id = ?", (link["link_id"],)
        ).fetchone()
        if row and row["shape_pts"]:
            pts = json.loads(row["shape_pts"])
            if len(pts) >= 2:
                s_lat, s_lon, _, _, _ = _snap_to_polyline(lat, lon, pts)
                return s_lat, s_lon
    except Exception:
        pass
    # Fallback: F→T two-point segment from nodes join
    try:
        if link["f_lat"] is not None and link["t_lat"] is not None:
            seg = [[link["f_lat"], link["f_lon"]], [link["t_lat"], link["t_lon"]]]
            s_lat, s_lon, _, _, _ = _snap_to_polyline(lat, lon, seg)
            return s_lat, s_lon
    except Exception:
        pass
    return link["cx_lat"], link["cx_lon"]


def _best_link(links: list[LinkInfo], road_name_hint: str | None = None) -> "LinkInfo":
    """도로 링크 우선순위 선택.

    우선 road_name_hint 매칭, 없으면 거리 기준 상위 후보 중 도로등급/길이로 정렬.
    교차로 연결부(짧고 낮은 등급)보다 주요 도로(높은 등급, 긴 길이)를 선호.
    """
    if road_name_hint:
        matched = [l for l in links if _road_name_matches(l["road_name"], road_name_hint)]
        if matched:
            return matched[0]

    # 가장 가까운 링크 기준 ±25m 이내 후보를 도로등급·길이로 재정렬
    min_dist = links[0]["dist_m"]
    tolerance = max(25.0, min_dist * 0.5)
    candidates = [l for l in links if l["dist_m"] <= min_dist + tolerance]
    # road_rank 오름차순(101=고속도로 우선), length 내림차순(긴 도로 우선)
    candidates.sort(key=lambda l: (l["road_rank"] or "999", -(l["length"] or 0)))
    return candidates[0]


def get_road_snap(
    lat: float, lon: float, road_name_hint: str | None = None
) -> dict | None:
    """Return selected road link's info + camera GPS projected onto road centerline.

    Returns dict:
      snap_lat, snap_lon  – nearest point on road polyline (polygon/transformer origin)
      bearing_deg         – local tangent direction at snap point (from shape_pts if available,
                            otherwise F→T bearing)
      road_name, lanes, max_spd, road_rank, road_width_m
      cam_dist_m          – distance from camera GPS to snapped point
    or None if DB unavailable / no link found.
    """
    try:
        links = get_links_near(lat, lon, radius_km=0.5)
    except FileNotFoundError:
        return None
    if not links:
        return None

    link = _best_link(links, road_name_hint)

    # Fetch shape_pts for this link (full road polyline, if available in DB)
    snap_lat = link["cx_lat"]
    snap_lon = link["cx_lon"]
    local_bearing = link["bearing_deg"]
    road_pts: list[list[float]] | None = None
    snap_along_m: float | None = None

    try:
        con = _get_conn()
        row = con.execute(
            "SELECT shape_pts FROM links WHERE link_id = ?", (link["link_id"],)
        ).fetchone()
        if row and row["shape_pts"]:
            pts = json.loads(row["shape_pts"])
            if len(pts) >= 2:
                pts = _extend_pts_with_adjacent(pts, link)
                snap_lat, snap_lon, local_bearing, _, seg_idx = _snap_to_polyline(lat, lon, pts)
                road_pts, snap_along_m = _road_corridor_pts(pts, snap_lat, snap_lon, seg_idx)
        # Fallback: F→T nodes if shape_pts absent (old DB)
        if road_pts is None and link["f_lat"] is not None and link["t_lat"] is not None:
            seg_pts: list[list[float]] = [
                [link["f_lat"], link["f_lon"]],
                [link["t_lat"], link["t_lon"]],
            ]
            seg_pts = _extend_pts_with_adjacent(seg_pts, link)
            snap_lat, snap_lon, local_bearing, _, seg_idx = _snap_to_polyline(lat, lon, seg_pts)
            road_pts, snap_along_m = _road_corridor_pts(seg_pts, snap_lat, snap_lon, seg_idx)
    except Exception:
        pass  # keep cx_lat/cx_lon defaults

    # Bidirectional road center fix:
    # NodeLink stores each direction as a separate one-way link, so the primary
    # snap lands on one carriageway center. Find the reverse link and average the
    # two snap points to get the true road center. Also shift road_pts laterally
    # by the same offset so the polygon follows the real road centerline.
    #
    # Use the primary link's overall F→T bearing (link["bearing_deg"]) instead of
    # the shape-segment local_bearing. local_bearing is the bearing of the single
    # shape segment where the camera happens to snap — on curved roads this can
    # differ by 40–60° from the overall link direction, causing the reverse link
    # to fail the ±60° bearing check even when it is the correct reverse carriageway.
    _primary_bearing_for_rev = (
        link["bearing_deg"] if link["bearing_deg"] is not None else local_bearing
    )
    rev_link = _find_reverse_link(link, links, _primary_bearing_for_rev)
    if rev_link is not None:
        rev_snap_lat, rev_snap_lon = _snap_for_link(rev_link, lat, lon)
        sep_m = _dist_m(snap_lat, snap_lon, rev_snap_lat, rev_snap_lon)
        # Only average if the reverse snap is within a plausible carriageway
        # separation (3–40 m). Wider gaps suggest a different road entirely.
        if 2.0 <= sep_m <= 40.0:
            center_lat = (snap_lat + rev_snap_lat) / 2.0
            center_lon = (snap_lon + rev_snap_lon) / 2.0
            dlat = center_lat - snap_lat
            dlon = center_lon - snap_lon
            snap_lat = center_lat
            snap_lon = center_lon
            # Shift road_pts by the same lateral delta so the corridor polygon
            # is also centered on the full road (not just one carriageway).
            if road_pts is not None:
                road_pts = [[p[0] + dlat, p[1] + dlon] for p in road_pts]

    # 대부분의 도로는 왕복이므로 항상 양방향(×2)으로 계산.
    has_reverse = True
    direction_mult = 2

    rank = link.get("road_rank", "")
    # 한국 도로구조령 차선폭 기준:
    #   고속도로/도시고속 (101/102): 3.5m
    #   국도 (103): 3.5m (지방부), 3.25m (도시부) → 보수적으로 3.5m 사용
    #   지방도/광역시도 (104/105): 3.25m
    #   시도 이하 (106+): 3.0m
    if rank in ("101", "102", "103"):
        lane_w = 3.5
    elif rank in ("104", "105"):
        lane_w = 3.25
    else:
        lane_w = 3.0
    road_width_m = max(1, link["lanes"] or 2) * direction_mult * lane_w

    return {
        "snap_lat":      round(snap_lat, 7),
        "snap_lon":      round(snap_lon, 7),
        "bearing_deg":   round(local_bearing, 1) if local_bearing is not None else None,
        "road_name":     link["road_name"],
        "lanes":         link["lanes"],
        "max_spd":       link["max_spd"],
        "road_rank":     link["road_rank"],
        "road_width_m":  round(road_width_m, 1),
        "is_oneway":     not has_reverse,
        "cam_dist_m":    round(_dist_m(lat, lon, snap_lat, snap_lon), 1),
        # 도로 중심선 포인트 (지도 위 곡선 polygon 생성용)
        "road_pts":      [[round(p[0], 7), round(p[1], 7)] for p in road_pts] if road_pts else None,
        "snap_along_m":  snap_along_m,
    }


def get_road_info(lat: float, lon: float, road_name_hint: str | None = None) -> LinkInfo | None:
    """Return the nearest link's road info, or None if DB is unavailable or no link found.

    road_name_hint: CCTV 이름에서 파싱한 도로명 (예: "국도 1호선").
                    제공 시 일치하는 링크를 우선 선택하고, 없으면 거리/등급 기준 폴백.
    """
    try:
        links = get_links_near(lat, lon, radius_km=0.5)
        if not links:
            return None
        return _best_link(links, road_name_hint)
    except FileNotFoundError:
        return None
