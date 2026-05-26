"""
nodelink.py — Runtime query module for the national standard node-link SQLite DB.

Usage:
    from nodelink import get_road_info, get_links_near, get_nodes_near

The DB is built once by:
    python scripts/build_nodelink_db.py
"""

from __future__ import annotations

import sqlite3
import math
from pathlib import Path
from functools import lru_cache
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


@lru_cache(maxsize=1)
def _get_conn() -> sqlite3.Connection:
    if not _DB_PATH.exists():
        raise FileNotFoundError(
            f"nodelink.sqlite not found at {_DB_PATH}.\n"
            "Run:  python scripts/build_nodelink_db.py"
        )
    con = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only = 1")
    return con


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
    """Return links whose centre-point is within radius_km of (lat, lon), sorted by distance."""
    min_lat, max_lat, min_lon, max_lon = _bbox(lat, lon, radius_km)
    con = _get_conn()
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
        WHERE r.min_lat >= ? AND r.max_lat <= ?
          AND r.min_lon >= ? AND r.max_lon <= ?
        """,
        (min_lat, max_lat, min_lon, max_lon),
    ).fetchall()

    result: list[LinkInfo] = []
    for row in rows:
        d = _dist_m(lat, lon, row["cx_lat"], row["cx_lon"])
        if d <= radius_km * 1000:
            f_lat, f_lon = row["f_lat"], row["f_lon"]
            t_lat, t_lon = row["t_lat"], row["t_lon"]
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
                f_lat=float(row["f_lat"]) if row["f_lat"] is not None else None,
                f_lon=float(row["f_lon"]) if row["f_lon"] is not None else None,
                t_lat=float(row["t_lat"]) if row["t_lat"] is not None else None,
                t_lon=float(row["t_lon"]) if row["t_lon"] is not None else None,
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


def get_road_snap(
    lat: float, lon: float, road_name_hint: str | None = None
) -> dict | None:
    """Return selected road link's info + camera GPS projected onto road centerline.

    Returns dict:
      snap_lat, snap_lon  – nearest point on road segment (use as polygon/transformer origin)
      bearing_deg         – F→T bearing
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

    # Same selection logic as get_road_info
    link = links[0]
    if road_name_hint:
        matched = [l for l in links if _road_name_matches(l["road_name"], road_name_hint)]
        if matched:
            link = matched[0]

    f_lat, f_lon = link["f_lat"], link["f_lon"]
    t_lat, t_lon = link["t_lat"], link["t_lon"]

    # Default snap = link centre-point
    snap_lat, snap_lon = link["cx_lat"], link["cx_lon"]

    if None not in (f_lat, f_lon, t_lat, t_lon):
        R_lat_m = 110574.0
        R_lon_m = 111320.0 * math.cos(math.radians(lat))
        px_m = (lon - f_lon) * R_lon_m
        py_m = (lat - f_lat) * R_lat_m
        bx_m = (t_lon - f_lon) * R_lon_m
        by_m = (t_lat - f_lat) * R_lat_m
        sx_m, sy_m = _project_to_segment(px_m, py_m, bx_m, by_m)
        snap_lat = f_lat + sy_m / R_lat_m
        snap_lon = f_lon + sx_m / R_lon_m

    rank = link.get("road_rank", "")
    lane_w = 3.5 if rank in ("101", "102") else (3.25 if rank == "103" else 3.0)
    road_width_m = max(1, link["lanes"] or 2) * lane_w

    return {
        "snap_lat":    round(snap_lat, 7),
        "snap_lon":    round(snap_lon, 7),
        "bearing_deg": link["bearing_deg"],
        "road_name":   link["road_name"],
        "lanes":       link["lanes"],
        "max_spd":     link["max_spd"],
        "road_rank":   link["road_rank"],
        "road_width_m": round(road_width_m, 1),
        "cam_dist_m":  round(_dist_m(lat, lon, snap_lat, snap_lon), 1),
    }


def get_road_info(lat: float, lon: float, road_name_hint: str | None = None) -> LinkInfo | None:
    """Return the nearest link's road info, or None if DB is unavailable or no link found.

    road_name_hint: CCTV 이름에서 파싱한 도로명 (예: "국도 1호선").
                    제공 시 일치하는 링크를 우선 선택하고, 없으면 거리 기준 폴백.
    """
    try:
        links = get_links_near(lat, lon, radius_km=0.5)
        if not links:
            return None
        if road_name_hint:
            matched = [l for l in links if _road_name_matches(l["road_name"], road_name_hint)]
            if matched:
                return matched[0]
        return links[0]
    except FileNotFoundError:
        return None
