"""Quick test: read first feature from MOCT_LINK.shp and verify coordinate conversion."""
import shapefile
from pyproj import Transformer

transformer = Transformer.from_crs("EPSG:5186", "EPSG:4326", always_xy=True)

sf = shapefile.Reader("node-link-data/MOCT_LINK.shp", encoding="euc-kr")
fields = [f[0] for f in sf.fields[1:]]
print("Fields:", fields[:10])
link_id_idx = fields.index("LINK_ID")

# Check first 3 features
for i, sr in enumerate(sf.iterShapeRecords()):
    if i >= 3:
        break
    link_id = sr.record[link_id_idx]
    pts = sr.shape.points
    if not pts:
        continue
    x, y = pts[0]
    lon, lat = transformer.transform(x, y)
    print(f"LINK_ID={link_id}  raw=({x:.1f}, {y:.1f})  wgs84=({lat:.6f}, {lon:.6f})  npts={len(pts)}")
