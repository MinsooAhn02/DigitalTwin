"""
main.py — FastAPI WebSocket 브로드캐스트 서버

  모드 선택 (config.REPLAY_MODE):
    True  → real_world_track_data.json을 프레임별 재생 (YOLO 불필요)
    False → ITS RTSP → YOLOv8x → ByteTrack → Transform → Analytics

  엔드포인트:
    WS  /ws       실시간 JSON 스트림
    GET /health   헬스체크
"""

from __future__ import annotations
import asyncio
import json
import logging
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import numpy as np
from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from analytics import TrafficAnalytics, VehicleState
from config import (
    FPS,
    REPLAY_MODE,
    REPLAY_JSON_PATH,
    REPLAY_FPS,
    ITS_CCTV_IDS,
    ITS_API_KEY,
    VEHICLE_CLASSES,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

analytics = TrafficAnalytics()
_clients: set[WebSocket] = set()


# ── 앱 생명주기 ───────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    if REPLAY_MODE:
        logger.info("Replay 모드로 시작: %s", REPLAY_JSON_PATH)
        task = asyncio.create_task(replay_loop())
    else:
        from detector import VideoStream, fetch_rtsp_url
        rtsp_url = None
        if ITS_API_KEY != "YOUR_API_KEY_HERE":
            rtsp_url = await fetch_rtsp_url(ITS_CCTV_IDS[0])
        stream = VideoStream(rtsp_url)
        app.state.stream = stream
        task = asyncio.create_task(live_loop(stream))

    yield
    task.cancel()
    if not REPLAY_MODE:
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


@app.get("/cctvs")
async def get_cctvs(
    minX: float = Query(126.93),
    maxX: float = Query(127.14),
    minY: float = Query(37.36),
    maxY: float = Query(37.56),
):
    """ITS API에서 현재 뷰 영역의 CCTV 위치 목록 반환"""
    params = {
        "apiKey":    ITS_API_KEY,
        "type":      "its",
        "cctvType":  "1",
        "minX": str(minX), "maxX": str(maxX),
        "minY": str(minY), "maxY": str(maxY),
        "getType":   "json",
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
                    if lat and lon:
                        result.append({
                            "id":   item.get("cctvid", ""),
                            "name": item.get("cctvname", item.get("cctvid", "")),
                            "lat":  lat,
                            "lon":  lon,
                        })
                except (ValueError, TypeError):
                    continue
            return result
    except Exception as e:
        logger.warning("CCTV 목록 조회 실패: %s", e)
        return []


@app.get("/health")
async def health():
    return {"status": "ok", "mode": "replay" if REPLAY_MODE else "live", "clients": len(_clients)}


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
    """JSON → {frame_id: [record, ...]} 딕셔너리."""
    path = Path(REPLAY_JSON_PATH)
    if not path.exists():
        raise FileNotFoundError(f"Replay JSON 없음: {path}")
    records = json.loads(path.read_text(encoding="utf-8"))
    by_frame: dict[int, list[dict]] = defaultdict(list)
    for r in records:
        by_frame[int(r["frame_id"])].append(r)
    return by_frame


async def replay_loop() -> None:
    """
    JSON 데이터를 frame_id 순서로 읽어
    각 프레임 데이터를 analytics로 처리 후 WebSocket 브로드캐스트.
    마지막 프레임 도달 시 처음부터 반복(Loop).
    """
    by_frame = _load_replay_data()
    frame_ids = sorted(by_frame.keys())
    frame_delay = 1.0 / REPLAY_FPS

    # In/Out 누적 카운터 (direction 필드 기반)
    in_count = 0
    out_count = 0
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

                # In/Out 첫 등장만 카운팅 (중복 방지)
                if direction == "In"  and tid not in seen_in:
                    seen_in.add(tid)
                    in_count += 1
                if direction == "Out" and tid not in seen_out:
                    seen_out.add(tid)
                    out_count += 1

                vs = VehicleState(
                    track_id=tid,
                    class_name=VEHICLE_CLASSES.get(class_id, "unknown"),
                    bbox_xyxy=[0.0, 0.0, 0.0, 0.0],   # replay엔 bbox 없음
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

        # 루프 종료 → 카운터 리셋 후 재시작
        logger.info("Replay 루프 완료, 재시작")
        in_count = out_count = 0
        seen_in.clear()
        seen_out.clear()
        analytics.__init__()   # 내부 상태 리셋


# ════════════════════════════════════════════════════════════════════════
# LIVE 모드 파이프라인
# ════════════════════════════════════════════════════════════════════════
async def live_loop(stream) -> None:
    from detector import VehicleDetector
    from tracker import VehicleTracker
    from transform import PerspectiveTransformer

    detector    = VehicleDetector()
    tracker     = VehicleTracker()
    transformer = PerspectiveTransformer()
    frame_delay = 1.0 / FPS

    while True:
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

    result = analytics.update(frame_id, timestamp_ms, vehicles, in_cnt, out_cnt)
    return result.to_dict()


# ── 진입점 ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
