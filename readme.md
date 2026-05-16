# Traffic Digital Twin

A real-time traffic monitoring system that detects and tracks vehicles from live CCTV streams and visualizes them on an interactive map.

---

## Requirements

| Item | Spec |
|------|------|
| OS | Windows 10/11 |
| Python | 3.11+ |
| Node.js | 18+ |
| GPU | NVIDIA (CUDA 12.4, TensorRT 8+) |
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

> Get a key at https://openapi.its.go.kr → Sign up → Request cctvInfo API access

### 3. Run

```bash
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

---

## Commands

| Command | Description |
|---------|-------------|
| `make dev` | Run backend + frontend together |
| `make dev MODEL=s` | Run with yolov8s (lighter, lower-end GPUs) |
| `make backend` | Run backend only |
| `make frontend` | Run frontend only |
| `make kill` | Force-release port 8000 |

---

## Recommended Settings by Hardware

| Environment | Model | Speed | Command |
|-------------|-------|-------|---------|
| RTX 3070+ | yolov8x + TensorRT | ~10ms | `make dev` |
| RTX 3060 / 2070 | yolov8s + TensorRT | ~3ms | `make dev MODEL=s` |
| GTX 1080 or older | yolov8s + CUDA | ~30ms | `make dev MODEL=s` |
| CPU only | Not supported | — | — |

> `.engine` files must be converted on the GPU that will run them. Files built on a different GPU will not work.

---

## Tech Stack

| Layer | Technology |
|-------|------------|
| Backend | Python 3.11, FastAPI, Uvicorn |
| AI Detection | YOLOv8x / YOLOv8s (ultralytics), TensorRT FP16 |
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
- **Alerts**: Speeding (>60 km/h), bottleneck (stationary ≥2 sec)
- **ROI Editing**: Draw a polygon directly on the video — detections outside the region are ignored
- **Camera Calibration**: 4-point pixel↔GPS homography for accurate coordinate mapping
- **FOV Visualization**: Trapezoid overlay on map showing the camera's actual ground coverage

---

## Documentation

For system architecture, module details, and workflows, see `explanation.txt`.
