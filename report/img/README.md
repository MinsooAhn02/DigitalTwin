# report/img — figure images + how to finish the report

## 1) Drop the four screenshots here (exact filenames)

Until a file exists, the report compiles with a labelled placeholder box in its
place, so you can build anytime and add images later.

| Filename | Figure | Used in section |
|----------|--------|-----------------|
| `cctv_map.png`       | CCTV cameras on the map (status-coloured icons + FOV) | III. System Design (`fig:map`) |
| `yolo_detect.png`    | YOLO detection + tracking overlay                     | IV. Implementation – Detection (`fig:yolo`) |
| `auto_calib.png`     | Automatic lane-based calibration result               | IV. Implementation – Localization (`fig:calib`) |
| `congestion_map.png` | Congestion: CCTV icons recoloured by status + region  | IV. Implementation – Congestion (`fig:congestion`) |

- PNG recommended (JPG/PDF also fine — then change the extension in the `.tex`
  `\figorbox{...}` calls). Width ~1200 px; scaled to one column.
- Same filenames appear in both the base and contribution PDFs.

## 2) Get the Evaluation numbers (automatic — no manual logging)

The numbers are produced by the running app; you do **not** type them in.

1. `cd traffic-digital-twin && make dev`
2. In the browser, click a CCTV on the map and **watch it on the Live tab for
   1–2 minutes** (the Live tab is what records per-stage latency; the YOLO tab
   records tracking/speed/detection only).
3. The server auto-writes these files to `traffic-digital-twin/backend/` and
   refreshes them every 30 s:
   - `eval_summary.json`  ← main file; also contains a ready-to-paste Markdown
     table under the `"markdown"` field
   - `eval_latency.csv`, `eval_tracking.csv`, `eval_speed.csv`, `eval_detections.csv`
   - Instant snapshot any time: open `http://localhost:8000/eval/report`
   - Start a fresh run: `POST http://localhost:8000/eval/reset`

## 3) Fill the tables in the report

Copy the values from `eval_summary.json` (or the CSVs) into:
- **Table I** (latency / throughput) ← `eval_latency.csv` + `throughput_fps`
- **Table II** (tracking + speed distribution) ← `eval_tracking.csv` / `eval_speed.csv`
- **Table III** (measured vs. ITS speed) ← the live broadcast fields
  `our_avg_kph` / `its_speed_kph` / `speed_error_pct` and `backend/speed_scale.json`
- The "Setup" line in Section V (model / backend / tracker / GPU / camera / N frames)

## 4) Rebuild the PDFs

```
cd traffic-digital-twin
make report          # builds report_base.pdf and report_contribution.pdf
```
(or upload the `report/` folder to Overleaf and compile each `.tex`).

> Checklist: ① put 4 images here → ② `make dev`, watch Live tab 1–2 min →
> ③ read `backend/eval_summary.json` → ④ type those numbers into the tables →
> ⑤ `make report`.
