"""
Build nodelink.sqlite with shape_pts, f_lat, f_lon, t_lat, t_lon columns.

Steps:
  1. Add missing columns (idempotent — safe to re-run).
  2. Populate f_lat/f_lon/t_lat/t_lon from nodes JOIN (fast, pure SQL).
  3. Read MOCT_LINK.shp (EPSG:5186 TM) → convert to WGS84 → write shape_pts JSON.

Usage:
  python build_nodelink_db.py
  (run from repo root: DigitalTwin/)
"""

import json
import sqlite3
import sys
import time
from pathlib import Path

import shapefile
from pyproj import Transformer

DB_PATH  = Path("node-link-data/nodelink.sqlite")
SHP_PATH = Path("node-link-data/MOCT_LINK.shp")

# EPSG:5186 → WGS84
# always_xy=True: input=(easting, northing), output=(lon, lat)
transformer = Transformer.from_crs("EPSG:5186", "EPSG:4326", always_xy=True)


def _add_columns(con: sqlite3.Connection) -> None:
    """Add missing columns without failing if they already exist."""
    cols_to_add = [
        ("f_lat",     "REAL"),
        ("f_lon",     "REAL"),
        ("t_lat",     "REAL"),
        ("t_lon",     "REAL"),
        ("shape_pts", "TEXT"),
    ]
    cur = con.cursor()
    cur.execute("PRAGMA table_info(links)")
    existing = {row[1] for row in cur.fetchall()}
    for col, dtype in cols_to_add:
        if col not in existing:
            con.execute(f"ALTER TABLE links ADD COLUMN {col} {dtype}")
            print(f"  + Added column: {col}")
        else:
            print(f"  . Column already exists: {col}")
    con.commit()


def _populate_node_coords(con: sqlite3.Connection) -> None:
    """Fill f_lat/f_lon and t_lat/t_lon via SQL JOIN with nodes table."""
    print("\n[Step 2] Filling f_lat/f_lon/t_lat/t_lon from nodes JOIN ...")
    t0 = time.time()

    con.execute("""
        UPDATE links SET
            f_lat = (SELECT lat FROM nodes WHERE nodes.node_id = links.f_node),
            f_lon = (SELECT lon FROM nodes WHERE nodes.node_id = links.f_node)
        WHERE f_lat IS NULL
    """)
    con.execute("""
        UPDATE links SET
            t_lat = (SELECT lat FROM nodes WHERE nodes.node_id = links.t_node),
            t_lon = (SELECT lon FROM nodes WHERE nodes.node_id = links.t_node)
        WHERE t_lat IS NULL
    """)
    con.commit()

    cur = con.execute("SELECT COUNT(*) FROM links WHERE f_lat IS NOT NULL")
    n = cur.fetchone()[0]
    print(f"  Done in {time.time()-t0:.1f}s — {n:,} links with f_lat set.")


def _populate_shape_pts(con: sqlite3.Connection) -> None:
    """Read MOCT_LINK.shp and write WGS84 shape_pts JSON into DB."""
    if not SHP_PATH.exists():
        print(f"\n[Step 3] SKIP — shapefile not found: {SHP_PATH}")
        return

    print(f"\n[Step 3] Reading {SHP_PATH} ...")
    t0 = time.time()

    sf = shapefile.Reader(str(SHP_PATH), encoding="euc-kr")
    fields = [f[0] for f in sf.fields[1:]]  # skip deletion flag
    link_id_idx = fields.index("LINK_ID")

    total = len(sf)
    print(f"  Total features: {total:,}")

    BATCH = 5000
    batch: list[tuple[str, str]] = []

    def flush():
        con.executemany(
            "UPDATE links SET shape_pts = ? WHERE link_id = ?",
            batch,
        )
        con.commit()
        batch.clear()

    skipped = 0
    written = 0

    for i, sr in enumerate(sf.iterShapeRecords()):
        if i % 50000 == 0:
            elapsed = time.time() - t0
            pct = i / total * 100
            print(f"  {i:>8,} / {total:,}  ({pct:.1f}%)  {elapsed:.0f}s  written={written:,}", end="\r")

        link_id = str(sr.record[link_id_idx])
        pts_raw = sr.shape.points  # list of (x, y) in EPSG:5186 metres

        if len(pts_raw) < 2:
            skipped += 1
            continue

        # Convert EPSG:5186 (TM, northing/easting) to WGS84 lat/lon.
        # pyproj EPSG:5186: x=Easting(m), y=Northing(m)  (always_xy=False means
        # transformer.transform(easting, northing) → (lat, lon) when CRS is geographic)
        # Actually with always_xy=False the order follows CRS axis order.
        # EPSG:5186 axis order: Northing first, Easting second.
        # shapefile stores (x=Easting, y=Northing) — standard shapefile convention.
        wgs_pts = []
        for x_east, y_north in pts_raw:
            # always_xy=True: transform(easting, northing) → (lon, lat)
            lon, lat = transformer.transform(x_east, y_north)
            wgs_pts.append([round(lat, 7), round(lon, 7)])

        shape_json = json.dumps(wgs_pts, separators=(",", ":"))
        batch.append((shape_json, link_id))
        written += 1

        if len(batch) >= BATCH:
            flush()

    if batch:
        flush()

    elapsed = time.time() - t0
    print(f"\n  Done in {elapsed:.1f}s — written={written:,}  skipped={skipped:,}")


def main() -> None:
    if not DB_PATH.exists():
        print(f"ERROR: DB not found at {DB_PATH}", file=sys.stderr)
        sys.exit(1)

    print(f"DB: {DB_PATH.resolve()}")
    con = sqlite3.connect(str(DB_PATH))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA cache_size=-65536")  # 64 MB

    print("\n[Step 1] Adding missing columns ...")
    _add_columns(con)

    _populate_node_coords(con)
    _populate_shape_pts(con)

    # Verify
    print("\n[Verify]")
    for col in ("f_lat", "t_lat", "shape_pts"):
        cur = con.execute(f"SELECT COUNT(*) FROM links WHERE {col} IS NOT NULL")
        print(f"  {col} IS NOT NULL: {cur.fetchone()[0]:,}")

    con.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
