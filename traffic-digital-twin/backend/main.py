"""
main.py — FastAPI WebSocket 브로드캐스트 서버

  엔드포인트:
    WS   /ws              실시간 JSON 스트림
    GET  /cctvs           뷰포트 범위 CCTV 목록
    POST /switch-camera   라이브 카메라 전환 + BoT-SORT 리셋
    GET  /cctv-refresh    HLS 토큰 만료 시 신선한 URL 반환 (브라우저용)
    GET  /health          헬스체크
"""

from __future__ import annotations
import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager

import cv2
import httpx
import numpy as np
import supervision as sv
from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from analytics import TrafficAnalytics, VehicleState
from transform import PerspectiveTransformer
from config import (
    CAPTURE_INTERVAL_MS,
    CAPTURE_QUALITY,
    CAPTURE_WIDTH,
    FPS,
    ITS_API_KEY,
    ITS_BASE_URL,
    JPEG_QUALITY,
    MAX_IN_FLIGHT,
    RUNTIME_PROFILE_NAME,
    VEHICLE_CLASSES,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

analytics    = TrafficAnalytics()
_transformer = PerspectiveTransformer()
_clients: set[WebSocket] = set()

_camera_queue: asyncio.Queue = asyncio.Queue()

_latest_annotated: bytes | None = None
_frame_count: int = 0

# 현재 선택된 카메라 정보 (lat, lon, name, cctvurl)
_current_cam: dict | None = None
_cam_version: int = 0

_box_ann   = sv.BoxAnnotator(thickness=2)
_label_ann = sv.LabelAnnotator(text_scale=0.4, text_thickness=1, text_padding=3)


def _make_placeholder() -> bytes:
    img = np.zeros((360, 640, 3), dtype=np.uint8)
    cv2.putText(img, "CCTV를 클릭하면 YOLO 탐지가 시작됩니다",
                (60, 170), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (120, 120, 120), 1)
    cv2.putText(img, "Waiting for stream...",
                (210, 210), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (80, 80, 80), 1)
    _, buf = cv2.imencode(".jpg", img)
    return buf.tobytes()

_placeholder_jpeg: bytes = _make_placeholder()


# ── 앱 생명주기 ───────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    from detector import VehicleDetector, VideoStream
    logger.info("YOLOv8 + BoT-SORT 모델 로드 중…")
    detector = await asyncio.to_thread(VehicleDetector)
    stream   = VideoStream()
    app.state.stream   = stream
    app.state.detector = detector
    task         = asyncio.create_task(live_loop(detector, stream))
    refresh_task = asyncio.create_task(hls_refresh_loop(stream))

    yield
    task.cancel()
    refresh_task.cancel()
    if hasattr(app.state, "stream"):
        app.state.stream.release()


app = FastAPI(title="Traffic Digital Twin", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── WebSocket 엔드포인트 ──────────────────────────────────────────────
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    _clients.add(ws)
    logger.info("클라이언트 연결 (총 %d명)", len(_clients))
    try:
        while True:
            await asyncio.sleep(10)
    except WebSocketDisconnect:
        pass
    finally:
        _clients.discard(ws)
        logger.info("클라이언트 해제 (총 %d명)", len(_clients))


# ── CCTV 목록 ─────────────────────────────────────────────────────────
@app.get("/cctvs")
async def get_cctvs(
    minX: float = Query(126.93),
    maxX: float = Query(127.14),
    minY: float = Query(37.36),
    maxY: float = Query(37.56),
):
    """ITS API에서 현재 뷰 영역의 CCTV 위치·URL 목록 반환"""
    params = {
        "apiKey":   ITS_API_KEY,
        "type":     "its",
        "cctvType": "1",
        "minX": str(minX), "maxX": str(maxX),
        "minY": str(minY), "maxY": str(maxY),
        "getType":  "json",
    }
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(ITS_BASE_URL, params=params)
            resp.raise_for_status()
            items = resp.json().get("response", {}).get("data", [])
            result = []
            for item in items:
                try:
                    lat = float(item.get("coordy") or 0)
                    lon = float(item.get("coordx") or 0)
                    if not (lat and lon):
                        continue
                    url  = item.get("cctvurl", "")
                    name = item.get("cctvname", "")
                    result.append({
                        "id":      url or name,
                        "name":    name,
                        "lat":     lat,
                        "lon":     lon,
                        "cctvurl": url,
                    })
                except (ValueError, TypeError):
                    continue
            logger.info("CCTV 조회 완료: %d개", len(result))
            return result
    except Exception as e:
        logger.warning("CCTV 목록 조회 실패: %s", e)
        return []


# ── 카메라 전환 ───────────────────────────────────────────────────────
class CameraSwitch(BaseModel):
    cctvurl: str
    lat: float
    lon: float
    name: str = ""


@app.post("/switch-camera")
async def switch_camera(body: CameraSwitch):
    """클릭한 CCTV 정보 저장 + transformer 재보정 + BoT-SORT 리셋 + live_loop 스트림 전환."""
    global _current_cam, _cam_version
    _current_cam = {
        "lat": body.lat, "lon": body.lon,
        "name": body.name, "cctvurl": body.cctvurl,
    }
    _cam_version += 1
    _transformer.update_gps_center(body.lat, body.lon)
    analytics.__init__()

    # live_loop 스트림 전환 큐잉
    if body.cctvurl:
        await _camera_queue.put({"url": body.cctvurl, "lat": body.lat, "lon": body.lon})

    # BoT-SORT 내부 상태 리셋
    det = getattr(app.state, "detector", None)
    if det is not None:
        det.reset_tracker()

    logger.info("카메라 전환: %s (%.4f, %.4f)", body.name, body.lat, body.lon)
    return {"ok": True}


# ── HLS URL 갱신 (브라우저용) ────────────────────────────────────────
@app.get("/cctv-refresh")
async def cctv_refresh(
    name: str   = Query(""),
    lat:  float = Query(0.0),
    lon:  float = Query(0.0),
):
    """
    브라우저 HLS 토큰 만료 시 ITS API에서 신선한 URL 을 받아 반환.
    CctvPlayer.jsx 가 NETWORK_ERROR 시 호출한다.
    """
    if not (lat and lon):
        return {"cctvurl": ""}
    params = {
        "apiKey": ITS_API_KEY, "type": "its", "cctvType": "1",
        "minX": str(lon - 0.002), "maxX": str(lon + 0.002),
        "minY": str(lat - 0.002), "maxY": str(lat + 0.002),
        "getType": "json",
    }
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(ITS_BASE_URL, params=params)
            resp.raise_for_status()
            items = resp.json().get("response", {}).get("data", [])
        for item in items:
            if not name or item.get("cctvname") == name:
                return {"cctvurl": item.get("cctvurl", "")}
    except Exception as e:
        logger.warning("cctv-refresh 실패: %s", e)
    return {"cctvurl": ""}


# ── YOLO + BoT-SORT WebSocket ────────────────────────────────────────
@app.websocket("/ws/detect")
async def ws_detect(ws: WebSocket):
    """
    브라우저 <video> 캔버스 프레임(JPEG bytes) 수신
    → BoT-SORT 탐지+추적 + 어노테이션 → 결과 반환
    → analytics 업데이트 → 전체 클라이언트에 브로드캐스트
    """
    await ws.accept()

    detector = getattr(app.state, "detector", None)
    if detector is None:
        from detector import VehicleDetector
        logger.info("ws/detect: detector 로드 중…")
        detector = await asyncio.to_thread(VehicleDetector)
        app.state.detector = detector

    from tracker import VehicleTracker
    tracker = VehicleTracker()
    last_cam_ver = _cam_version
    fid = 0

    logger.info("BoT-SORT 탐지 클라이언트 연결")
    try:
        while True:
            raw = await ws.receive_bytes()

            # 카메라 전환 시 tracker(LineZone) + BoT-SORT 상태 리셋
            if _cam_version != last_cam_ver:
                detector.reset_tracker()
                tracker = VehicleTracker()
                last_cam_ver = _cam_version
                fid = 0

            # BoT-SORT 탐지+추적 + 어노테이션 (스레드)
            ann_bytes, detections, fw, fh = await asyncio.to_thread(
                _yolo_detect_annotate, raw, detector
            )

            # LineZone 카운팅
            tracked, in_cnt, out_cnt = tracker.update(detections, (fw, fh))
            if fid % 10 == 1:
                logger.info(
                    "[BoT-SORT] frame=%d  tracked=%d  in=%d  out=%d",
                    fid, len(tracked), in_cnt, out_cnt,
                )

            # GPS 변환 + VehicleState 생성
            vehicles: list[VehicleState] = []
            for i in range(len(tracked)):
                xyxy     = tracked.xyxy[i].tolist()
                class_id = int(tracked.class_id[i])
                track_id = (
                    int(tracked.tracker_id[i])
                    if tracked.tracker_id is not None else i
                )
                cx = (xyxy[0] + xyxy[2]) / 2
                cy = (xyxy[1] + xyxy[3]) / 2
                lat, lon = _transformer.pixel_to_gps(cx, cy)
                x_m, y_m = _transformer.pixel_to_meter(cx, cy)
                vehicles.append(VehicleState(
                    track_id=track_id,
                    class_name=VEHICLE_CLASSES.get(class_id, "unknown"),
                    bbox_xyxy=xyxy,
                    center_px=(cx, cy),
                    lat=lat, lon=lon,
                    x_m=x_m, y_m=y_m,
                ))

            fid += 1
            result = analytics.update(fid, time.time() * 1000, vehicles, in_cnt, out_cnt)
            await _broadcast(result.to_dict())
            await ws.send_bytes(ann_bytes)

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning("ws/detect 오류: %s", e)
    finally:
        logger.info("BoT-SORT 탐지 클라이언트 해제")


def _yolo_detect_annotate(
    jpeg_bytes: bytes, detector
) -> tuple[bytes, "sv.Detections", int, int]:
    """스레드 실행: BoT-SORT 탐지+추적 + 어노테이션."""
    global _frame_count
    arr   = np.frombuffer(jpeg_bytes, dtype=np.uint8)
    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if frame is None:
        return jpeg_bytes, sv.Detections.empty(), 640, 360

    fh, fw = frame.shape[:2]
    detections = detector.track(frame)  # BoT-SORT

    labels = [
        f"{VEHICLE_CLASSES.get(int(detections.class_id[i]), '?')} "
        f"#{int(detections.tracker_id[i]) if detections.tracker_id is not None else i} "
        f"{float(detections.confidence[i]):.0%}"
        for i in range(len(detections))
    ]
    annotated = _box_ann.annotate(frame.copy(), detections)
    annotated = _label_ann.annotate(annotated, detections, labels)
    cv2.putText(annotated, f"BoT-SORT  {len(detections)} vehicles",
                (8, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 80), 2)

    _frame_count += 1
    _, buf = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    return buf.tobytes(), detections, fw, fh


# ── 헬스체크 ──────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    stream = getattr(app.state, "stream", None)
    return {
        "status":           "ok",
        "clients":          len(_clients),
        "stream_open":      stream.is_open if stream else None,
        "frames_processed": _frame_count,
    }


@app.get("/runtime-config")
async def runtime_config():
    return {
        "profile": RUNTIME_PROFILE_NAME,
        "backendFps": FPS,
        "jpegQuality": JPEG_QUALITY,
        "captureIntervalMs": CAPTURE_INTERVAL_MS,
        "captureWidth": CAPTURE_WIDTH,
        "captureQuality": CAPTURE_QUALITY,
        "maxInFlight": MAX_IN_FLIGHT,
    }


# ── 브로드캐스트 헬퍼 ─────────────────────────────────────────────────
async def _broadcast(payload: dict) -> None:
    if not _clients:
        return
    msg = json.dumps(payload)
    await asyncio.gather(
        *[_safe_send(ws, msg) for ws in list(_clients)],
        return_exceptions=True,
    )


async def _safe_send(ws: WebSocket, msg: str) -> None:
    try:
        await ws.send_text(msg)
    except Exception:
        _clients.discard(ws)


# ════════════════════════════════════════════════════════════════════════
# LIVE 파이프라인 (서버사이드 HLS 직접 처리)
# ════════════════════════════════════════════════════════════════════════
async def live_loop(detector, stream) -> None:
    from tracker import VehicleTracker

    tracker        = VehicleTracker()
    target_interval = 1.0 / FPS
    skip_budget    = 0

    logger.info("Live 루프 대기 중 — 지도에서 CCTV를 클릭하여 스트림을 시작하세요")

    while True:
        # 카메라 전환 요청 처리
        if not _camera_queue.empty():
            cam = _camera_queue.get_nowait()
            try:
                stream.switch_to(cam["url"])
                _transformer.update_gps_center(cam["lat"], cam["lon"])
                tracker = VehicleTracker()
                analytics.__init__()
                skip_budget = 0
            except RuntimeError as e:
                logger.warning("카메라 전환 실패: %s", e)

        if not stream.is_open:
            await asyncio.sleep(0.5)
            continue

        frame_id, frame = stream.read_frame()
        if frame is None:
            await stream.reconnect()
            continue

        if skip_budget > 0:
            skip_budget -= 1
            continue

        t0 = time.perf_counter()
        payload = await asyncio.to_thread(
            _live_process, frame_id, frame, detector, tracker
        )
        if payload:
            await _broadcast(payload)

        elapsed = time.perf_counter() - t0
        if elapsed > target_interval:
            skip_budget = min(int(elapsed / target_interval) - 1, 5)
        else:
            await asyncio.sleep(target_interval - elapsed)


def _live_process(frame_id, frame, detector, tracker) -> dict | None:
    global _latest_annotated, _frame_count

    h, w = frame.shape[:2]
    timestamp_ms = time.time() * 1000

    # BoT-SORT 탐지+추적 (매 프레임)
    tracked = detector.track(frame)
    tracked, in_cnt, out_cnt = tracker.update(tracked, (w, h))

    vehicles: list[VehicleState] = []
    for i in range(len(tracked)):
        xyxy     = tracked.xyxy[i].tolist()
        class_id = int(tracked.class_id[i])
        track_id = (
            int(tracked.tracker_id[i])
            if tracked.tracker_id is not None else i
        )
        cx = (xyxy[0] + xyxy[2]) / 2
        cy = (xyxy[1] + xyxy[3]) / 2

        lat, lon = _transformer.pixel_to_gps(cx, cy)
        x_m, y_m = _transformer.pixel_to_meter(cx, cy)

        vehicles.append(VehicleState(
            track_id=track_id,
            class_name=VEHICLE_CLASSES.get(class_id, "unknown"),
            bbox_xyxy=xyxy,
            center_px=(cx, cy),
            lat=lat, lon=lon,
            x_m=x_m, y_m=y_m,
        ))

    try:
        labels = [
            f"{VEHICLE_CLASSES.get(int(tracked.class_id[i]), '?')} "
            f"#{int(tracked.tracker_id[i]) if tracked.tracker_id is not None else i}"
            for i in range(len(tracked))
        ]
        annotated = _box_ann.annotate(frame.copy(), tracked)
        annotated = _label_ann.annotate(annotated, tracked, labels)
        cv2.putText(
            annotated, f"Frame {frame_id} | {len(vehicles)} vehicles",
            (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2,
        )
        _, buf = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
        _latest_annotated = buf.tobytes()
    except Exception:
        pass

    _frame_count += 1
    result = analytics.update(frame_id, timestamp_ms, vehicles, in_cnt, out_cnt)
    return result.to_dict()


# ── HLS URL 자동 갱신 (서버사이드 live_loop 용) ───────────────────────
async def hls_refresh_loop(stream) -> None:
    """ITS HLS 토큰 만료 전 URL 갱신 (30분마다)."""
    REFRESH_INTERVAL = 1800
    while True:
        await asyncio.sleep(REFRESH_INTERVAL)
        cam = _current_cam
        if cam is None or not cam.get("name"):
            continue
        lat, lon, name = cam["lat"], cam["lon"], cam["name"]
        params = {
            "apiKey": ITS_API_KEY, "type": "its", "cctvType": "1",
            "minX": str(lon - 0.002), "maxX": str(lon + 0.002),
            "minY": str(lat - 0.002), "maxY": str(lat + 0.002),
            "getType": "json",
        }
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(ITS_BASE_URL, params=params)
                resp.raise_for_status()
                items = resp.json().get("response", {}).get("data", [])
            for item in items:
                if item.get("cctvname") == name:
                    new_url = item.get("cctvurl", "")
                    if new_url and new_url != stream.url:
                        logger.info("HLS URL 갱신: %s", name)
                        await _camera_queue.put({"url": new_url, "lat": lat, "lon": lon})
                        _current_cam["cctvurl"] = new_url
                    break
        except Exception as e:
            logger.warning("HLS URL 갱신 실패: %s", e)


# ── 진입점 ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
