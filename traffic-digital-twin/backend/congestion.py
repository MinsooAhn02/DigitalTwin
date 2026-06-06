"""
congestion.py — 카메라 단위 정체 구간 클러스터링 ([B])

백그라운드 모니터 카메라들은 개별 차량 GPS·속도가 없고 카메라당 차량 '개수'와
상태(normal/busy/congested)만 가진다. 따라서 차량 단위가 아니라 **카메라 단위**로,
인접한 혼잡/정체 카메라들을 묶어 "정체 구간"을 만든다.

- 입력: BackgroundMonitor.snapshot() 형태의 dict (cam_key -> {lat, lon, status, vehicle_count}).
- 거리: haversine (m). eps 이내면 같은 클러스터 (그리디 DBSCAN, min_samples=1).
- 출력: 지도 폴리곤 + 심각도. 외부 의존성 없음 (numpy 불필요, 순수 파이썬).
"""

from __future__ import annotations

import math

from utils import haversine_m as _haversine_m

# 심각도 레벨 (프론트 colorMap.SEVERITY_COLORS 와 1:1 대응)
SEV_MINOR = "minor"
SEV_MEDIUM = "medium"
SEV_SEVERE = "severe"


def _cluster_points(pts: list[dict], eps_m: float) -> list[list[dict]]:
    """그리디 DBSCAN (min_samples=1) — eps_m 이내 점들을 한 클러스터로 묶음."""
    n = len(pts)
    visited = [False] * n
    clusters: list[list[dict]] = []
    for i in range(n):
        if visited[i]:
            continue
        # BFS 로 연결 요소 수집
        stack = [i]
        visited[i] = True
        members = [pts[i]]
        while stack:
            cur = stack.pop()
            for j in range(n):
                if visited[j]:
                    continue
                d = _haversine_m(pts[cur]["lat"], pts[cur]["lon"],
                                 pts[j]["lat"], pts[j]["lon"])
                if d <= eps_m:
                    visited[j] = True
                    stack.append(j)
                    members.append(pts[j])
        clusters.append(members)
    return clusters


def _convex_hull(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Andrew monotone chain. (x, y) 점들의 볼록껍질 (반시계). <3점이면 원본 반환."""
    pts = sorted(set(points))
    if len(pts) < 3:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[tuple[float, float]] = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list[tuple[float, float]] = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _circle_polygon(lat: float, lon: float, radius_m: float, segments: int = 12) -> list[list[float]]:
    """centroid 중심 원형 근사 폴리곤 ([lon, lat] 순서, deck.gl 용)."""
    # 위도 1도 ≈ 111_320 m, 경도는 cos(lat) 보정
    dlat = radius_m / 111_320.0
    dlon = radius_m / (111_320.0 * max(0.01, math.cos(math.radians(lat))))
    poly: list[list[float]] = []
    for k in range(segments):
        ang = 2 * math.pi * k / segments
        poly.append([lon + dlon * math.cos(ang), lat + dlat * math.sin(ang)])
    return poly


def _severity(members: list[dict]) -> str:
    """클러스터 멤버들의 status/차량수로 심각도 산출.

    THRESH_BUSY=6, THRESH_CONGESTED=14 (BackgroundMonitor 기준) 재사용.
    """
    congested = sum(1 for m in members if m.get("status") == "congested")
    total = sum(int(m.get("vehicle_count", 0)) for m in members)
    if congested >= 2 or total > 6 * len(members):
        return SEV_SEVERE
    if congested >= 1 or any(m.get("status") == "busy" for m in members):
        return SEV_MEDIUM
    return SEV_MINOR


def compute_clusters(snapshot: dict, eps_m: float = 500.0,
                     pad_m: float = 120.0) -> list[dict]:
    """카메라 스냅샷 → 정체 구간 클러스터 리스트.

    snapshot: {cam_key: {lat, lon, status, vehicle_count, ...}, ...}
    반환: [{id, polygon:[[lon,lat]...], severity, camera_count, total_vehicles, cameras:[...]}]
    """
    # busy / congested 카메라만 후보
    candidates: list[dict] = []
    for cam_key, s in snapshot.items():
        if s.get("status") not in ("busy", "congested"):
            continue
        lat, lon = s.get("lat"), s.get("lon")
        if lat is None or lon is None:
            continue
        candidates.append({
            "cam_key": cam_key,
            "lat": float(lat), "lon": float(lon),
            "status": s.get("status"),
            "vehicle_count": int(s.get("vehicle_count", 0)),
        })

    clusters: list[dict] = []
    for idx, members in enumerate(_cluster_points(candidates, eps_m)):
        # 폴리곤 산출
        if len(members) >= 3:
            hull = _convex_hull([(m["lon"], m["lat"]) for m in members])
            polygon = [[x, y] for (x, y) in hull]
        else:
            # 1~2점이면 centroid 원형 근사
            clat = sum(m["lat"] for m in members) / len(members)
            clon = sum(m["lon"] for m in members) / len(members)
            polygon = _circle_polygon(clat, clon, pad_m)

        clusters.append({
            "id": f"cong-{idx}",
            "polygon": polygon,
            "severity": _severity(members),
            "camera_count": len(members),
            "total_vehicles": sum(m["vehicle_count"] for m in members),
            "cameras": [m["cam_key"] for m in members],
        })
    return clusters
