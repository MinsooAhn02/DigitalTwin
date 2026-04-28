"""
main.py — FastAPI WebSocket 브로드캐스트 서버

  모드 선택 (config.REPLAY_MODE):
    True  → real_world_track_data.json을 프레임별 재생 (YOLO 불필요)
    False → ITS HLS 라이브 스트림 + YOLOv8x + ByteTrack

  엔드포인트:
    WS   /ws              실시간 JSON 스트림
    GET  /cctvs           뷰포트 범위 CCTV 목록
    POST /switch-camera   라이브 카메라 전환
    GET  /video_feed      YOLO 어노테이션 MJPEG 스트림
    GET  /health          헬스체크
"""

from __future__ import annotations
import asyncio
import json
import logging
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path

import cv2
import httpx
import numpy as np
import supervision as sv
from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from analytics import TrafficAnalytics, VehicleState
from config import (
    FPS,
    REPLAY_MODE,
    REPLAY_JSON_PATH,
    REPLAY_FPS,
    ITS_API_KEY,
    ITS_BASE_URL,
    VEHICLE_CLASSES,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

analytics = TrafficAnalytics()
_clients: set[WebSocket] = set()

# 카메라 전환 요청 큐
_camera_queue: asyncio.Queue = asyncio.Queue()

# YOLO 어노테이션된 최신 프레임
_latest_annotated: bytes | None = None
_frame_count: int = 0

# 현재 선택된 카메라 GPS (ws/detect의 GPS 변환에 사용)
_current_cam: dict | None = None   # {lat, lon}
_cam_version: int = 0              # 카메라 전환 시 증가 → tracker 리셋 신호

# 어노테이터 (전역 재사용)
_box_ann   = sv.BoxAnnotator(thickness=2)
_label_ann = sv.LabelAnnotator(text_scale=0.4, text_thickness=1, text_padding=3)

# 카메라 미선택 시 보여줄 플레이스홀더 JPEG
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
    if REPLAY_MODE:
        logger.info("Replay 모드로 시작: %s", REPLAY_JSON_PATH)
        task = asyncio.create_task(replay_loop())
    else:
        from detector import VehicleDetector, VideoStream
        logger.info("Live 모드: YOLOv8 모델 로드 중…")
        detector = await asyncio.to_thread(VehicleDetector)
        stream   = VideoStream()
        app.state.stream   = stream
        app.state.detector = detector
        task = asyncio.create_task(live_loop(detector, stream))

    yield
    task.cancel()
    if not REPLAY_MODE and hasattr(app.state, "stream"):
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
                        "id":      url or name,   # URL 없으면 이름을 ID로
                        "name":    name,
                        "lat":     lat,
                        "lon":     lon,
                        "cctvurl": url,           # 없으면 빈 문자열
                    })
                except (ValueError, TypeError):
                    continue
            logger.info("CCTV 조회 완료: %d개 (bbox %.4f~%.4f, %.4f~%.4f)", len(result), minX, maxX, minY, maxY)
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
    """클릭한 CCTV 정보를 저장. ws/detect에서 GPS 변환 + tracker 리셋에 사용."""
    global _current_cam, _cam_version
    _current_cam = {"lat": body.lat, "lon": body.lon}
    _cam_version += 1
    analytics.__init__()   # 카메라 전환 시 통계 리셋
    logger.info("카메라 전환: %s (%.4f, %.4f)", body.name, body.lat, body.lon)
    return {"ok": True}


# ── YOLO 탐지 WebSocket ────────────────────────────────────────────────
@app.websocket("/ws/detect")
async def ws_detect(ws: WebSocket):
    """
    브라우저 <video> 캔버스 프레임(JPEG bytes) 수신
    → YOLO 탐지 + 어노테이션 → 결과 반환
    → tracker + analytics 업데이트 → 전체 클라이언트에 브로드캐스트
    """
    await ws.accept()

    # detector 확보 (없으면 지연 로드)
    detector = getattr(app.state, "detector", None)
    if detector is None:
        from detector import VehicleDetector
        logger.info("ws/detect: detector 로드 중…")
        detector = await asyncio.to_thread(VehicleDetector)
        app.state.detector = detector
        logger.info("ws/detect: detector 준비 완료")

    from tracker import VehicleTracker
    tracker = VehicleTracker()
    last_cam_ver = _cam_version
    fid = 0

    logger.info("YOLO 탐지 클라이언트 연결")
    try:
        while True:
            raw = await ws.receive_bytes()

            # 카메라가 바뀌면 tracker / analytics 리셋
            if _cam_version != last_cam_ver:
                tracker = VehicleTracker()
                last_cam_ver = _cam_version
                fid = 0

            # ─ YOLO 탐지 + 어노테이션 (스레드) ─
            ann_bytes, detections, fw, fh = await asyncio.to_thread(
                _yolo_detect_annotate, raw, detector
            )

            # ─ tracker 업데이트 (async 컨텍스트, 단일 스레드 보장) ─
            tracked, in_cnt, out_cnt = tracker.update(detections, (fw, fh))
            if fid % 10 == 1:   # 10프레임마다 ByteTrack 상태 로그
                logger.info(
                    "[ByteTrack] frame=%d  det=%d  tracked=%d  in=%d  out=%d",
                    fid, len(detections), len(tracked), in_cnt, out_cnt,
                )

            # ─ GPS 변환 + VehicleState 생성 ─
            vehicles: list[VehicleState] = []
            cam = _current_cam
            for i in range(len(tracked)):
                xyxy     = tracked.xyxy[i].tolist()
                class_id = int(tracked.class_id[i])
                track_id = int(tracked.tracker_id[i])
                cx = (xyxy[0] + xyxy[2]) / 2
                cy = (xyxy[1] + xyxy[3]) / 2
                lat, lon = _pixel_to_gps(cx, cy, fw, fh, cam) if cam else (37.0, 127.0)
                vehicles.append(VehicleState(
                    track_id=track_id,
                    class_name=VEHICLE_CLASSES.get(class_id, "unknown"),
                    bbox_xyxy=xyxy,
                    center_px=(cx, cy),
                    lat=lat, lon=lon,
                ))

            # ─ analytics 업데이트 + 전체 브로드캐스트 ─
            fid += 1
            result = analytics.update(fid, time.time() * 1000, vehicles, in_cnt, out_cnt)
            await _broadcast(result.to_dict())

            # ─ 어노테이션 프레임을 탐지 클라이언트에 반환 ─
            await ws.send_bytes(ann_bytes)

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning("ws/detect 오류: %s", e)
    finally:
        logger.info("YOLO 탐지 클라이언트 해제")


def _yolo_detect_annotate(
    jpeg_bytes: bytes, detector
) -> tuple[bytes, "sv.Detections", int, int]:
    """스레드 실행: YOLO 탐지 + 어노테이션. (ann_bytes, detections, w, h) 반환."""
    global _frame_count
    arr   = np.frombuffer(jpeg_bytes, dtype=np.uint8)
    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if frame is None:
        import supervision as _sv
        return jpeg_bytes, _sv.Detections.empty(), 640, 360

    fh, fw = frame.shape[:2]
    detections = detector.detect(frame)

    labels = [
        f"{VEHICLE_CLASSES.get(int(detections.class_id[i]), '?')} {float(detections.confidence[i]):.0%}"
        for i in range(len(detections))
    ]
    annotated = _box_ann.annotate(frame.copy(), detections)
    annotated = _label_ann.annotate(annotated, detections, labels)
    cv2.putText(annotated, f"YOLO  {len(detections)} vehicles",
                (8, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 80), 2)

    _frame_count += 1
    _, buf = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return buf.tobytes(), detections, fw, fh


def _pixel_to_gps(cx: float, cy: float, fw: int, fh: int, cam: dict) -> tuple[float, float]:
    """픽셀 좌표 → 카메라 중심 기준 근사 GPS 변환."""
    dlat, dlon = 0.0006, 0.0004
    lat = cam["lat"] - ((cy - fh / 2) / fh) * dlat * 2
    lon = cam["lon"] + ((cx - fw / 2) / fw) * dlon * 2
    return lat, lon


# ── 헬스체크 ──────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    stream = getattr(app.state, "stream", None)
    return {
        "status":       "ok",
        "mode":         "replay" if REPLAY_MODE else "live",
        "clients":      len(_clients),
        "stream_open":  stream.is_open if stream else None,
        "frames_processed": _frame_count,
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
# REPLAY 모드 파이프라인
# ════════════════════════════════════════════════════════════════════════
def _load_replay_data() -> dict[int, list[dict]]:
    path = Path(REPLAY_JSON_PATH)
    if not path.exists():
        raise FileNotFoundError(f"Replay JSON 없음: {path}")
    records = json.loads(path.read_text(encoding="utf-8"))
    by_frame: dict[int, list[dict]] = defaultdict(list)
    for r in records:
        by_frame[int(r["frame_id"])].append(r)
    return by_frame


async def replay_loop() -> None:
    by_frame   = _load_replay_data()
    frame_ids  = sorted(by_frame.keys())
    frame_delay = 1.0 / REPLAY_FPS

    in_count = out_count = 0
    seen_in:  set[int] = set()
    seen_out: set[int] = set()

    logger.info("Replay 시작: %d 프레임, %d fps", len(frame_ids), REPLAY_FPS)

    while True:
        for fid in frame_ids:
            t0 = time.perf_counter()
            records = by_frame[fid]
            timestamp_ms = time.time() * 1000

            vehicles: list[VehicleState] = []
            for r in records:
                tid      = int(r["tracker_id"])
                class_id = int(r["class_id"])
                direction = str(r.get("direction", "Unknown"))

                if direction == "In"  and tid not in seen_in:
                    seen_in.add(tid); in_count += 1
                if direction == "Out" and tid not in seen_out:
                    seen_out.add(tid); out_count += 1

                vs = VehicleState(
                    track_id=tid,
                    class_name=VEHICLE_CLASSES.get(class_id, "unknown"),
                    bbox_xyxy=[0.0, 0.0, 0.0, 0.0],
                    center_px=(float(r.get("pixel_x", 0)), float(r.get("pixel_y", 0))),
                    lat=float(r["latitude"]),
                    lon=float(r["longitude"]),
                    direction=direction,
                )
                vehicles.append(vs)

            result = analytics.update(fid, timestamp_ms, vehicles, in_count, out_count)
            await _broadcast(result.to_dict())

            elapsed = time.perf_counter() - t0
            await asyncio.sleep(max(0.0, frame_delay - elapsed))

        logger.info("Replay 루프 완료, 재시작")
        in_count = out_count = 0
        seen_in.clear(); seen_out.clear()
        analytics.__init__()


# ════════════════════════════════════════════════════════════════════════
# LIVE 모드 파이프라인
# ════════════════════════════════════════════════════════════════════════
async def live_loop(detector, stream) -> None:
    from tracker import VehicleTracker
    from transform import PerspectiveTransformer

    tracker     = VehicleTracker()
    transformer = PerspectiveTransformer()
    frame_delay = 1.0 / FPS

    logger.info("Live 루프 대기 중 — 지도에서 CCTV를 클릭하여 스트림을 시작하세요")

    while True:
        # ── 카메라 전환 요청 처리 ────────────────────────────────────
        if not _camera_queue.empty():
            cam = _camera_queue.get_nowait()
            try:
                stream.switch_to(cam["url"])
                transformer.update_gps_center(cam["lat"], cam["lon"])
                tracker = VehicleTracker()   # 새 카메라용 트래커 리셋
                analytics.__init__()
            except RuntimeError as e:
                logger.warning("카메라 전환 실패: %s", e)

        # ── 아직 카메라 미선택 ────────────────────────────────────────
        if not stream.is_open:
            await asyncio.sleep(0.5)
            continue

        t0 = time.perf_counter()
        frame_id, frame = stream.read_frame()
        if frame is None:
            await stream.reconnect()
            continue

        payload = await asyncio.to_thread(
            _live_process, frame_id, frame, detector, tracker, transformer
        )
        if payload:
            await _broadcast(payload)

        elapsed = time.perf_counter() - t0
        await asyncio.sleep(max(0.0, frame_delay - elapsed))


def _live_process(frame_id, frame, detector, tracker, transformer) -> dict | None:
    global _latest_annotated, _frame_count

    h, w = frame.shape[:2]
    timestamp_ms = time.time() * 1000

    detections = detector.detect(frame)
    tracked, in_cnt, out_cnt = tracker.update(detections, (w, h))

    vehicles: list[VehicleState] = []
    for i in range(len(tracked)):
        xyxy     = tracked.xyxy[i].tolist()
        class_id = int(tracked.class_id[i])
        track_id = int(tracked.tracker_id[i])
        cx = (xyxy[0] + xyxy[2]) / 2
        cy = (xyxy[1] + xyxy[3]) / 2

        lat, lon = transformer.pixel_to_gps(cx, cy)
        x_m, y_m = transformer.pixel_to_meter(cx, cy)

        vs = VehicleState(
            track_id=track_id,
            class_name=VEHICLE_CLASSES.get(class_id, "unknown"),
            bbox_xyxy=xyxy,
            center_px=(cx, cy),
            lat=lat,
            lon=lon,
            x_m=x_m,
            y_m=y_m,
        )
        vehicles.append(vs)

    # ── YOLO 어노테이션 프레임 생성 (MJPEG용) ────────────────────────
    try:
        labels = [
            f"{VEHICLE_CLASSES.get(int(tracked.class_id[i]), '?')} #{int(tracked.tracker_id[i])}"
            for i in range(len(tracked))
        ]
        annotated = _box_ann.annotate(frame.copy(), tracked)
        annotated = _label_ann.annotate(annotated, tracked, labels)

        # 프레임 카운터 오버레이
        cv2.putText(
            annotated, f"Frame {frame_id} | {len(vehicles)} vehicles",
            (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2,
        )
        _, buf = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 75])
        _latest_annotated = buf.tobytes()
    except Exception:
        pass   # 어노테이션 실패해도 탐지 결과는 정상 반환

    _frame_count += 1
    result = analytics.update(frame_id, timestamp_ms, vehicles, in_cnt, out_cnt)
    return result.to_dict()


# ── 진입점 ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
