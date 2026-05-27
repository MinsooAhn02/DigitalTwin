"""
전국 표준 노드링크 shapefile → SQLite 변환 스크립트 (1회 실행)

입력: node-link-data/MOCT_NODE.shp + MOCT_LINK.shp  (EPSG:5186)
출력: node-link-data/nodelink.sqlite

변경사항:
  - links 테이블에 shape_pts TEXT 컬럼 추가: JSON 배열 [[lat,lon], ...]
    도로 곡선 전체 GPS 좌표. nodelink.get_road_snap()이 이걸 사용해
    F_NODE→T_NODE 직선이 아닌 실제 도로선에 정확히 snap함.

필요 패키지 (이 스크립트 전용):
    pip install pyshp pyproj

실행:
    python scripts/build_nodelink_db.py
"""

import json
import math
import sqlite3
import sys
from pathlib import Path

try:
    import shapefile
    from pyproj import Transformer
except ImportError:
    sys.exit("pip install pyshp pyproj  후 다시 실행하세요.")

ROOT     = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "node-link-data"
NODE_SHP = DATA_DIR / "MOCT_NODE.shp"
LINK_SHP = DATA_DIR / "MOCT_LINK.shp"
OUT_DB   = DATA_DIR / "nodelink.sqlite"

to_wgs84 = Transformer.from_crs("EPSG:5186", "EPSG:4326", always_xy=True)


def _fields(sf):  # type: ignore
    return [f[0] for f in sf.fields[1:]]  # DeletionFlag 제외


def build_nodes(cur: sqlite3.Cursor) -> None:
    cur.executescript("""
        CREATE TABLE nodes (
            id       INTEGER PRIMARY KEY,
            node_id  TEXT,
            node_type TEXT,
            node_name TEXT,
            lat      REAL,
            lon      REAL
        );
        CREATE VIRTUAL TABLE nodes_rtree USING rtree(
            id, min_lat, max_lat, min_lon, max_lon
        );
    """)

    print("노드 처리 중 (MOCT_NODE.shp)...")
    sf = shapefile.Reader(str(NODE_SHP), encoding="euc-kr")
    flds = _fields(sf)
    rows, rtree = [], []

    for i, sr in enumerate(sf.iterShapeRecords(), 1):
        rec  = dict(zip(flds, sr.record))
        x, y = sr.shape.points[0]
        lon, lat = to_wgs84.transform(x, y)

        rows.append((i, rec.get("NODE_ID", ""), rec.get("NODE_TYPE", ""),
                     rec.get("NODE_NAME", ""), lat, lon))
        rtree.append((i, lat, lat, lon, lon))

        if len(rows) >= 10_000:
            cur.executemany("INSERT INTO nodes VALUES (?,?,?,?,?,?)", rows)
            cur.executemany("INSERT INTO nodes_rtree VALUES (?,?,?,?,?)", rtree)
            rows.clear(); rtree.clear()
            print(f"  {i:,}건", end="\r")

    if rows:
        cur.executemany("INSERT INTO nodes VALUES (?,?,?,?,?,?)", rows)
        cur.executemany("INSERT INTO nodes_rtree VALUES (?,?,?,?,?)", rtree)

    cur.execute("CREATE INDEX idx_node_id ON nodes(node_id)")
    print(f"\n  완료 ({i:,}개 노드)")


def build_links(cur: sqlite3.Cursor) -> None:
    cur.executescript("""
        CREATE TABLE links (
            id        INTEGER PRIMARY KEY,
            link_id   TEXT,
            f_node    TEXT,
            t_node    TEXT,
            lanes     INTEGER,
            max_spd   INTEGER,
            road_rank TEXT,
            road_name TEXT,
            length    REAL,
            cx_lat    REAL,
            cx_lon    REAL,
            shape_pts TEXT
        );
        CREATE VIRTUAL TABLE links_rtree USING rtree(
            id, min_lat, max_lat, min_lon, max_lon
        );
    """)

    print("링크 처리 중 (MOCT_LINK.shp)...")
    sf = shapefile.Reader(str(LINK_SHP), encoding="euc-kr")
    flds = _fields(sf)
    rows, rtree = [], []

    def _int(v, default=0):
        try: return int(v or default)
        except (ValueError, TypeError): return default

    def _float(v, default=0.0):
        try: return float(v or default)
        except (ValueError, TypeError): return default

    for i, sr in enumerate(sf.iterShapeRecords(), 1):
        rec  = dict(zip(flds, sr.record))
        pts  = sr.shape.points  # [(x, y), ...] EPSG:5186

        lons, lats = zip(*(to_wgs84.transform(x, y) for x, y in pts))
        min_lat, max_lat = min(lats), max(lats)
        min_lon, max_lon = min(lons), max(lons)
        cx_lat = (min_lat + max_lat) / 2
        cx_lon = (min_lon + max_lon) / 2

        # 도로 곡선 전체 GPS 좌표 저장 (정확한 snap에 사용)
        shape_pts_json = json.dumps(
            [[round(la, 7), round(lo, 7)] for la, lo in zip(lats, lons)]
        )

        rows.append((
            i, rec.get("LINK_ID", ""), rec.get("F_NODE", ""), rec.get("T_NODE", ""),
            _int(rec.get("LANES")), _int(rec.get("MAX_SPD")),
            rec.get("ROAD_RANK", ""), rec.get("ROAD_NAME", ""),
            _float(rec.get("LENGTH")), cx_lat, cx_lon, shape_pts_json,
        ))
        rtree.append((i, min_lat, max_lat, min_lon, max_lon))

        if len(rows) >= 5_000:
            cur.executemany("INSERT INTO links VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            cur.executemany("INSERT INTO links_rtree VALUES (?,?,?,?,?)", rtree)
            rows.clear(); rtree.clear()
            print(f"  {i:,}건", end="\r")

    if rows:
        cur.executemany("INSERT INTO links VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        cur.executemany("INSERT INTO links_rtree VALUES (?,?,?,?,?)", rtree)

    cur.execute("CREATE INDEX idx_link_id ON links(link_id)")
    print(f"\n  완료 ({i:,}개 링크)")


def main() -> None:
    import shutil
    # Build into a temp file first so the running server can keep using the old DB.
    tmp_db = OUT_DB.with_suffix(".tmp")
    if tmp_db.exists():
        tmp_db.unlink()

    con = sqlite3.connect(str(tmp_db))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA cache_size=-65536")  # 64 MB cache

    try:
        build_nodes(con.cursor())
        con.commit()
        build_links(con.cursor())
        con.commit()
    finally:
        con.close()

    size_mb = tmp_db.stat().st_size / 1024 ** 2
    print(f"\n빌드 완료: {tmp_db.name}  ({size_mb:.1f} MB)")

    # Replace old DB (server must be stopped for this step on Windows)
    if OUT_DB.exists():
        bak = OUT_DB.with_suffix(".bak")
        if bak.exists():
            bak.unlink()
        try:
            OUT_DB.rename(bak)
            print(f"기존 DB 백업: {bak.name}")
        except PermissionError:
            print(
                f"\n[경고] {OUT_DB.name} 파일이 사용 중입니다.\n"
                f"  백엔드 서버를 종료한 후 다음 명령을 실행하세요:\n"
                f"  move \"{tmp_db}\" \"{OUT_DB}\""
            )
            return
    shutil.move(str(tmp_db), str(OUT_DB))
    print(f"교체 완료: {OUT_DB.name}")


if __name__ == "__main__":
    main()
