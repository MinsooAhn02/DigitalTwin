"""
utils.py — 공통 유틸리티
"""
from __future__ import annotations
import math

_R_EARTH = 6_371_000.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """두 GPS 좌표 사이 거리 (m). min(1, sqrt) 으로 antipodal 수치 안정성 보장."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _R_EARTH * math.asin(min(1.0, math.sqrt(h)))
