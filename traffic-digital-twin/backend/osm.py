"""
osm.py — OpenStreetMap Overpass API queries for road attributes.
"""

from __future__ import annotations
import logging
import httpx

logger = logging.getLogger(__name__)

_OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# highway types to consider (excludes footways, paths, etc.)
_ROAD_TYPES = (
    "motorway", "trunk", "primary", "secondary", "tertiary",
    "unclassified", "residential",
    "motorway_link", "trunk_link", "primary_link", "secondary_link", "tertiary_link",
)

_LANE_WIDTH_BY_HIGHWAY = {
    "motorway": 3.5,
    "trunk": 3.5,
    "motorway_link": 3.5,
    "trunk_link": 3.5,
    "primary": 3.25,
    "primary_link": 3.25,
}
_LANE_WIDTH_DEFAULT = 3.0


def _lane_width(highway: str) -> float:
    return _LANE_WIDTH_BY_HIGHWAY.get(highway, _LANE_WIDTH_DEFAULT)


async def get_road_width_m(
    lat: float,
    lon: float,
    radius_m: int = 30,
) -> float | None:
    """Query Overpass for road width near (lat, lon).

    Priority:
      1. `width` tag — direct measured width (metres)
      2. `lanes:forward` tag — one-direction lane count × lane_width
      3. `lanes` tag / 2 — total lanes assumed bidirectional

    Returns width in metres (one travel direction), or None on failure / no data.
    """
    highway_filter = "|".join(_ROAD_TYPES)
    query = (
        f"[out:json][timeout:6];"
        f'way(around:{radius_m},{lat},{lon})[highway~"^({highway_filter})$"];'
        f"out tags 5;"
    )
    try:
        async with httpx.AsyncClient(timeout=7.0) as client:
            resp = await client.post(_OVERPASS_URL, content=query.encode())
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.debug("OSM Overpass 요청 실패: %s", exc)
        return None

    elements = data.get("elements", [])
    if not elements:
        return None

    for el in elements:
        tags = el.get("tags", {})
        highway = tags.get("highway", "")

        # 1. Direct width tag (most accurate)
        width_str = tags.get("width", "").replace("m", "").strip()
        if width_str:
            try:
                return float(width_str)
            except ValueError:
                pass

        lw = _lane_width(highway)

        # 2. lanes = total lanes (CCTV typically sees the whole road)
        # 3. lanes:forward fallback if lanes tag absent
        lanes_str = tags.get("lanes", "").strip()
        if lanes_str:
            try:
                return int(lanes_str) * lw
            except ValueError:
                pass

        # fallback: lanes:forward × 2 (양방향 도로 추정)
        fwd = tags.get("lanes:forward", "").strip()
        if fwd:
            try:
                return int(fwd) * 2 * lw
            except ValueError:
                pass

    return None
