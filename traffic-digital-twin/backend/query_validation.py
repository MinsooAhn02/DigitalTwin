# query_validation.py — backend 폴더에서 실행
import requests, json, math

data = json.load(open("landmarks_input.json", encoding="utf-8"))
cam = data["cameras"][0]

for c in cam["correspondences"]:
    u, v = c["pixel"]
    gt = c["gps"]
    r = requests.post("http://localhost:8000/calibration/check-point",
                      json={"pixel": [u, v], "gps": gt})
    result = r.json()
    flat = result["flat"]
    curved = result["curved"]

    def dist(a, b):
        dlat = (a[0]-b[0]) * 110574
        dlon = (a[1]-b[1]) * 111320 * math.cos(math.radians(a[0]))
        return math.hypot(dlat, dlon)

    print(f"pixel={c['pixel']}")
    print(f"  gt     = {gt}")
    print(f"  flat   = {flat}  오차={dist(flat,gt):.1f}m")
    print(f"  curved = {curved}  오차={dist(curved,gt):.1f}m")
    print()