# Traffic Digital Twin

A real-time traffic monitoring system that detects and tracks vehicles from live CCTV streams and visualizes them on an interactive map.

> Source: https://github.com/MinsooAhn02/DigitalTwin
# Traffic Digital Twin

A real-time traffic monitoring system that detects and tracks vehicles from live CCTV streams and visualizes them on an interactive map.

> Source: https://github.com/MinsooAhn02/DigitalTwin

---

## Requirements

| Item | Spec |
|------|------|
| OS | Windows 10/11 |
| Python | 3.13+ |
| Node.js | 18+ |
| GPU | NVIDIA (CUDA 12.4, TensorRT 10+) |
| Make | GnuWin32 Make |

---

## Quick Start

### 1. Install Make (once)
## Requirements

| Item | Spec |
|------|------|
| OS | Windows 10/11 |
| Python | 3.13+ |
| Node.js | 18+ |
| GPU | NVIDIA (CUDA 12.4, TensorRT 10+) |
| Make | GnuWin32 Make |

---

## Quick Start

### 1. Install Make (once)

```powershell
winget install GnuWin32.Make
[System.Environment]::SetEnvironmentVariable("PATH", $env:PATH + ";C:\Program Files (x86)\GnuWin32\bin", "User")
```

### 2. Set API Key

Create `traffic-digital-twin/backend/.env`:

```
ITS_API_KEY=your_key_here
```

> Get a key at https://www.its.go.kr/opendata/ → Sign up → Request cctvInfo API access

### 3. Run

```bash
cd traffic-digital-twin
cd traffic-digital-twin
make dev
```

On first run, the following are handled automatically:
- Python virtual environment setup and package install
- CUDA 12.4 PyTorch + TensorRT install
- Model / tracker / performance profile selection GUI
- TensorRT FP16 engine conversion (~10 min)
- Frontend package install

Open `http://localhost:5173` in your browser.
On first run, the following are handled automatically:
- Python virtual environment setup and package install
- CUDA 12.4 PyTorch + TensorRT install
- Model / tracker / performance profile selection GUI
- TensorRT FP16 engine conversion (~10 min)
- Frontend package install

Open `http://localhost:5173` in your browser.

---

## Commands
## Commands

| Command | Description |
|---------|-------------|
| `make dev` | Run backend + frontend together |
| `make backend` | Run backend only |
| `make frontend` | Run frontend only |
| `make kill` | Force-release port 8000 |
| Command | Description |
|---------|-------------|
| `make dev` | Run backend + frontend together |
| `make backend` | Run backend only |
| `make frontend` | Run frontend only |
| `make kill` | Force-release port 8000 |

---

## Recommended Settings by Hardware

| Environment | Recommended Model | Speed | Notes |
|-------------|-------------------|-------|-------|
| RTX 3070+ (8 GB+ VRAM) | YOLO26x + TensorRT FP16 | ~27 fps | Best accuracy |
| RTX 3060 / 2070 (6 GB VRAM) | YOLO26m + TensorRT FP16 | ~34 fps | High accuracy |
| Mid-range GPU (4 GB VRAM) | YOLO26s + TensorRT FP16 | ~52 fps | Balanced |
| Low-end / no VRAM | YOLO26n | ~60 fps GPU / ~9 fps CPU | Lightweight |
| CPU only | Not supported for real-time | — | |

Model and tracker tier are selected automatically at first run via a hardware-aware GUI picker.

> `.engine` files must be converted on the GPU that will run them. Files built on a different GPU will not work.

---

## Tech Stack

| Layer | Technology |
|-------|------------|
| Backend | Python 3.11, FastAPI, Uvicorn |
| AI Detection | YOLO26m (ultralytics), TensorRT FP16; YOLOv8s legacy fallback |
| Vehicle Tracking | BoxMOT (BotSort / ByteTrack / OcSort, with ReID) |
| Video Processing | OpenCV + FFmpeg (HLS streams) |
| Frontend | React 18 + Vite |
| Map | deck.gl + react-map-gl + MapLibre GL |
| HLS Playback | hls.js |
| External API | ITS Korea Traffic Information OpenAPI |

---
## Recommended Settings by Hardware

| Environment | Recommended Model | Speed | Notes |
|-------------|-------------------|-------|-------|
| RTX 3070+ (8 GB+ VRAM) | YOLO26x + TensorRT FP16 | ~27 fps | Best accuracy |
| RTX 3060 / 2070 (6 GB VRAM) | YOLO26m + TensorRT FP16 | ~34 fps | High accuracy |
| Mid-range GPU (4 GB VRAM) | YOLO26s + TensorRT FP16 | ~52 fps | Balanced |
| Low-end / no VRAM | YOLO26n | ~60 fps GPU / ~9 fps CPU | Lightweight |
| CPU only | Not supported for real-time | — | |

Model and tracker tier are selected automatically at first run via a hardware-aware GUI picker.

> `.engine` files must be converted on the GPU that will run them. Files built on a different GPU will not work.

---

## Tech Stack

| Layer | Technology |
|-------|------------|
| Backend | Python 3.11, FastAPI, Uvicorn |
| AI Detection | YOLO26m (ultralytics), TensorRT FP16; YOLOv8s legacy fallback |
| Vehicle Tracking | BoxMOT (BotSort / ByteTrack / OcSort, with ReID) |
| Video Processing | OpenCV + FFmpeg (HLS streams) |
| Frontend | React 18 + Vite |
| Map | deck.gl + react-map-gl + MapLibre GL |
| HLS Playback | hls.js |
| External API | ITS Korea Traffic Information OpenAPI |

---

## Features

- **Live CCTV Detection**: Click a camera on the map → HLS stream + AI detection starts immediately
- **Vehicle Tracking**: BoxMOT ReID-based — vehicles keep the same ID even after brief occlusion
- **Traffic Analytics**: Speed (Haversine + EMA), LOS grade (A–F), automatic parking detection
- **Direction Filter**: Per-direction vehicle list tabs (All / Inbound / Outbound) with live speed summary
- **Alerts**: Speeding (>60 km/h), bottleneck (stationary ≥2 sec)
- **Background Monitoring**: Polls up to 37 cameras every 8s; busy/congested status shown on map
- **Congestion Clustering**: DBSCAN over haversine distance → convex hull overlay on map
- **History**: SQLite time-series store (30s snapshots, 14-day retention, CSV export, peak-time detection)
- **ROI Editing**: Draw a polygon directly on the video — detections outside the region are ignored
- **Camera Calibration**: 4-point pixel↔GPS homography + road-centreline two-stage curved transform
- **FOV Visualization**: Road-corridor polygon on map following actual road curvature

---

## Documentation

For algorithm details and code-level logic, see `CODE_LOGIC.md`.
For a Korean-language system overview, see `explanation.txt`.
## Features

- **Live CCTV Detection**: Click a camera on the map → HLS stream + AI detection starts immediately
- **Vehicle Tracking**: BoxMOT ReID-based — vehicles keep the same ID even after brief occlusion
- **Traffic Analytics**: Speed (Haversine + EMA), LOS grade (A–F), automatic parking detection
- **Direction Filter**: Per-direction vehicle list tabs (All / Inbound / Outbound) with live speed summary
- **Alerts**: Speeding (>60 km/h), bottleneck (stationary ≥2 sec)
- **Background Monitoring**: Polls up to 37 cameras every 8s; busy/congested status shown on map
- **Congestion Clustering**: DBSCAN over haversine distance → convex hull overlay on map
- **History**: SQLite time-series store (30s snapshots, 14-day retention, CSV export, peak-time detection)
- **ROI Editing**: Draw a polygon directly on the video — detections outside the region are ignored
- **Camera Calibration**: 4-point pixel↔GPS homography + road-centreline two-stage curved transform
- **FOV Visualization**: Road-corridor polygon on map following actual road curvature

---

## Documentation

For algorithm details and code-level logic, see `CODE_LOGIC.md`.
For a Korean-language system overview, see `explanation.txt`.
