"""
main.py — FastAPI WebSocket 브로드캐스트 서버

  엔드포인트:
    WS   /ws              실시간 JSON 스트림
    GET  /cctvs           뷰포트 범위 CCTV 목록
    POST /switch-camera   라이브 카메라 전환 + boxmot 트래커 리셋
    GET  /cctv-refresh    HLS 토큰 만료 시 신선한 URL 반환 (브라우저용)
    GET  /hls-proxy       ITS HLS 스트림 CORS 프록시 (m3u8 + ts 세그먼트)
    GET  /health          헬스체크

  파이프라인 구조:
    live_loop  — 서버사이드 HLS 직접 처리, ws/detect 미활성 시 브로드캐스트
    ws/detect  — 브라우저 캔버스 프레임 처리 (YOLO 탭 활성 시)
    두 파이프라인은 동일 VehicleDetector를 공유하므로 동시에 track() 호출 금지.
    ws/detect 활성 시 live_loop는 프레임만 드레인하고 track()을 건너뜀.

  도로 인식 및 GPS 보정:
    switch_camera 호출 시:
      1. nodelink SQLite DB에서 카메라 GPS 반경 500m 내 링크 쿼리
      2. 카메라 GPS를 링크 F/T 노드 세그먼트에 직교 투영 → snap_lat/snap_lon (도로 중심선)
      3. CCTV 이름에서 name_bearing 파싱 (도로명 방향 표기 기반)
         없으면 nodelink F→T bearing 사용 (effective_bearing)
      4. snap 좌표를 transformer GPS 기준점 및 FOV polygon 원점으로 사용

    live_loop 카메라 전환 시:
      5. OSM Overpass API로 도로폭 쿼리 (osm.py):
           width 태그 → lanes:forward × 차선폭 → lanes/2 × 차선폭 순 우선순위
           실패 시 nodelink lanes × 차선폭으로 폴백
      6. 소실점(VP) 기반 자동 캘리브레이션 (auto_calibrate_from_frame):
           - name_bearing 확정 시 (fix_direction=True): VP flip 스킵
           - 미확정 시: VP가 우측 편향이면 bearing 180° 반전
      7. 캘리브레이션 성공 시 auto_calibrated WS 메시지 브로드캐스트
           (heading, near_m, far_m, road_width_m, cam_h_m, pitch_deg, road_length_m)

  WS 메시지 타입:
    camera_ready     카메라 전환 완료 (road_name, road_bearing, name_bearing,
                     snap_lat, snap_lon, road_lanes, road_max_spd, calibrated)
    auto_calibrated  자동 캘리브레이션 완료 (heading, near_m, far_m,
                     road_width_m, cam_h_m, pitch_deg, road_length_m)
    camera_error     카메라 전환 실패
    [기타]           FrameAnalytics JSON (차량 추적 결과)

  FOV polygon 방향 우선순위 (프론트엔드):
    auto_calibrated.heading (곡률/VP 보정) > name_bearing > road_bearing (nodelink F→T)
"""

from __future__ import annotations
import asyncio
import json
import logging
import math
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

import cv2
import httpx
import numpy as np
import supervision as sv
from cachetools import TTLCache
from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from analytics import TrafficAnalytics, VehicleState
from transform import PerspectiveTransformer, CALIBRATION_PATH
import roi_manager

SPEED_SCALE_PATH = Path(__file__).resolve().parent / "speed_scale.json"


def _load_speed_scale(cam_key: str) -> float:
    """카메라별 저장된 속도 보정 계수 로드. 없으면 1.0."""
    try:
        if SPEED_SCALE_PATH.exists():
            data = json.loads(SPEED_SCALE_PATH.read_text(encoding="utf-8"))
            return float(data.get(cam_key, {}).get("speed_scale", 1.0))
    except Exception:
        pass
    return 1.0


def _save_speed_scale(cam_key: str, scale: float, converged: bool) -> None:
    """속도 보정 계수를 camera_key별로 JSON에 저장."""
    try:
        data: dict = {}
        if SPEED_SCALE_PATH.exists():
            try:
                data = json.loads(SPEED_SCALE_PATH.read_text(encoding="utf-8"))
            except Exception:
                pass
        from datetime import datetime, timezone
        data[cam_key] = {
            "speed_scale": scale,
            "converged": converged,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        SPEED_SCALE_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:
        logger.warning("speed_scale 저장 실패: %s", exc)
from nodelink import get_road_info, get_road_snap
from config import (
    CAPTURE_INTERVAL_MS,
    CAPTURE_QUALITY,
    CAPTURE_WIDTH,
    FPS,
    HLS_REFRESH_INTERVAL,
    ITS_API_KEY,
    ITS_BASE_URL,
    ITS_POLL_INTERVAL,
    ITS_TRAFFIC_URL,
    JPEG_QUALITY,
    MAX_IN_FLIGHT,
    RUNTIME_PROFILE_NAME,
    VEHICLE_CLASSES,
)

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

analytics    = TrafficAnalytics()
_transformer = PerspectiveTransformer()
_clients: set[WebSocket] = set()

_camera_queue: asyncio.Queue = asyncio.Queue()

_frame_count: int = 0
_detect_clients: int = 0   # ws/detect 활성 연결 수 — live_loop 브로드캐스트 억제용

# 현재 선택된 카메라 정보 (lat, lon, name, cctvurl)
_current_cam: dict | None = None
_cam_version: int = 0

# MJPEG 스트림용 최신 프레임 버퍼
_latest_frame_jpeg: bytes | None = None       # 원본 프레임
_latest_annotated_jpeg: bytes | None = None   # YOLO 어노테이션 프레임

_box_ann   = sv.BoxAnnotator(thickness=2)
_label_ann = sv.LabelAnnotator(text_scale=0.4, text_thickness=1, text_padding=3)

# 차선 감지 자동 캘리브레이션 상태
_auto_calib_attempts: int  = 0    # 남은 시도 횟수 (0 = 비활성)
_auto_calib_road_width_m: float = 7.0  # lanes × 2 × lane_width_m

# ITS API 응답 캐시 (TTL 5분, 최대 50개 bbox 조합)
_cctv_cache: TTLCache = TTLCache(maxsize=50, ttl=300)

# ITS 구간속도 (5분 주기 폴링) — None이면 데이터 없음
_its_speed_kph: float | None = None

# 현재 스트림 실제 FPS — MJPEG 슬립 간격과 live_loop target_interval에 반영
_stream_fps: float = float(FPS)


def _safe_tid(tracker_id_arr, idx: int, fallback: int) -> int:
    """BoT-SORT가 미확정 트랙에 np.nan을 반환할 때 ValueError 방지."""
    if tracker_id_arr is None:
        return fallback
    try:
        v = int(tracker_id_arr[idx])
        return fallback if math.isnan(float(v)) else v
    except (TypeError, ValueError):
        return fallback


def _make_placeholder() -> bytes:
    img = np.zeros((360, 640, 3), dtype=np.uint8)
    cv2.putText(img, "CCTV를 클릭하면 YOLO 탐지가 시작됩니다",
                (60, 170), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (120, 120, 120), 1)
    cv2.putText(img, "Waiting for stream...",
                (210, 210), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (80, 80, 80), 1)
    _, buf = cv2.imencode(".jpg", img)
    return buf.tobytes()

_placeholder_jpeg: bytes = _make_placeholder()


def _parse_traffic_items(resp_json: dict) -> list[dict]:
    """ITS 교통소통정보 API 응답에서 링크 목록 추출.
    JSON: {body: {items: [...]}}  — response 래퍼 없고 items가 직접 리스트
    XML→JSON 변환본: {response: {body: {items: {item: [...]}}}} 도 호환 처리"""
    body = resp_json.get("body") or resp_json.get("response", {}).get("body", {})
    items = body.get("items", [])
    if isinstance(items, list):
        return items
    # XML 스타일: items = {"item": [...]}
    if isinstance(items, dict):
        raw = items.get("item", [])
        return [raw] if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
    return []


async def _fetch_its_section_speed(lat: float, lon: float) -> float | None:
    """카메라 위치 주변 ITS 구간속도 평균 조회 (bbox 기반, type=all)."""
    margin = 0.005  # 약 500m (~카메라 시야 반경)
    params = {
        "apiKey":  ITS_API_KEY,
        "type":    "all",
        "drcType": "all",
        "minX": str(lon - margin), "maxX": str(lon + margin),
        "minY": str(lat - margin), "maxY": str(lat + margin),
        "getType": "json",
    }
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(ITS_TRAFFIC_URL, params=params)
            resp.raise_for_status()
            raw_json = resp.json()
        items = _parse_traffic_items(raw_json)
        logger.debug("ITS trafficInfo items=%d  sample=%s", len(items), items[:2] if items else raw_json)
        speeds = [float(it["speed"]) for it in items
                  if it.get("speed") and float(it["speed"]) > 5]
        return round(sum(speeds) / len(speeds), 1) if speeds else None
    except Exception as exc:
        logger.warning("ITS 구간속도 조회 실패: %s", exc)
        return None


async def _update_its_speed() -> None:
    global _its_speed_kph
    if not _current_cam:
        return
    result = await _fetch_its_section_speed(_current_cam["lat"], _current_cam["lon"])
    if result is not None:
        _its_speed_kph = result
        logger.info("ITS 구간속도 갱신: %.1f kph", result)
        new_scale = analytics.calibrate_from_its(result)
        if new_scale is not None:
            converged = analytics.speed_scale_converged
            logger.info("속도 보정 계수 갱신: %.4f (수렴: %s, ITS %.1f kph)", new_scale, converged, result)
            if _current_cam:
                cam_key = roi_manager.camera_key(_current_cam.get("cctvurl", ""))
                _save_speed_scale(cam_key, new_scale, converged)


_NAME_BEARING: dict[str, float] = {
    "정북": 0.0,   "북방향": 0.0,   "북쪽": 0.0,   "북측": 0.0,   "북부": 0.0,   "(북)": 0.0,
    "북동방향": 45.0, "북동쪽": 45.0, "북동측": 45.0, "(북동)": 45.0,
    "정동": 90.0,  "동방향": 90.0,  "동쪽": 90.0,  "동측": 90.0,  "동부": 90.0,  "(동)": 90.0,
    "남동방향": 135.0, "남동쪽": 135.0, "남동측": 135.0, "(남동)": 135.0,
    "정남": 180.0, "남방향": 180.0, "남쪽": 180.0, "남측": 180.0, "남부": 180.0, "(남)": 180.0,
    "남서방향": 225.0, "남서쪽": 225.0, "남서측": 225.0, "(남서)": 225.0,
    "정서": 270.0, "서방향": 270.0, "서쪽": 270.0, "서측": 270.0, "서부": 270.0, "(서)": 270.0,
    "북서방향": 315.0, "북서쪽": 315.0, "북서측": 315.0, "(북서)": 315.0,
}


_ROAD_NAME_RE = re.compile(
    # 우선순위 1: [국도 1호선] 대괄호 형식
    r"\[([^\]]*(?:국도|지방도|고속도로|특별시도|광역시도|시도|군도)[^\]]*)\]"
    r"|"
    # 우선순위 2: 대괄호 없이 도로종류 + 호선번호  예) "국도1호선", "지방도 302호"
    r"((?:국도|지방도|고속도로|특별시도|광역시도|시도|군도)\s*\d+\s*호선?)"
)


def _parse_road_name_hint(cctv_name: str) -> str | None:
    """CCTV 이름에서 도로명 힌트를 파싱.

    매칭 우선순위:
      1. '[국도 1호선]' 대괄호 형식
      2. '국도1호선', '지방도302호' 등 대괄호 없는 형식
    ITS API cctvname에는 대부분 대괄호가 없으므로 두 형식 모두 지원.
    """
    if not cctv_name:
        return None
    m = _ROAD_NAME_RE.search(cctv_name)
    if not m:
        return None
    # group(1) = 대괄호 형식, group(2) = 평문 형식
    return (m.group(1) or m.group(2) or "").strip() or None


def _parse_name_bearing(name: str, road_bearing: float | None = None) -> float | None:
    """CCTV 이름에서 방향각 추출. 명시된 방위 우선, 상행/하행은 road_bearing 보조 사용."""
    if not name:
        return None
    for keyword, deg in _NAME_BEARING.items():
        if keyword in name:
            return deg
    # 상행(toward Seoul) = road 반대방향, 하행(away from Seoul) = road 방향
    if road_bearing is not None:
        if "하행" in name:
            return road_bearing
        if "상행" in name:
            return (road_bearing + 180) % 360
    return None


def _parse_its_items(resp_json: dict) -> list[dict]:
    """ITS API 응답에서 데이터 목록 추출. 단건 dict도 list로 정규화."""
    raw = resp_json.get("response", {}).get("data", [])
    if isinstance(raw, dict):
        return [raw]
    return raw if isinstance(raw, list) else []


def _build_vehicles(
    tracked: "sv.Detections",
    frame_wh: tuple[int, int] | None = None,
) -> "list[VehicleState]":
    """BoT-SORT 결과를 VehicleState 리스트로 변환.

    frame_wh: (width, height) — 프레임 범위 밖으로 Kalman 예측된 ghost 트랙 제거용.
    """
    fw, fh = frame_wh if frame_wh else (float("inf"), float("inf"))
    vehicles: list[VehicleState] = []
    for i in range(len(tracked)):
        xyxy     = tracked.xyxy[i].tolist()
        class_id = int(tracked.class_id[i])
        track_id = _safe_tid(tracked.tracker_id, i, i)
        cx = (xyxy[0] + xyxy[2]) / 2
        cy = (xyxy[1] + xyxy[3]) / 2
        # 호모그래피는 도로 평면 기준 → ground contact point(bbox 바닥 중심) 사용
        gx, gy = cx, xyxy[3]
        # Kalman 예측으로 프레임 밖으로 이탈한 ghost 트랙 제거
        if gx < -fw * 0.1 or gx > fw * 1.1 or gy < -fh * 0.1 or gy > fh * 1.1:
            continue
        lat, lon = _transformer.pixel_to_gps(gx, gy)
        x_m, y_m = _transformer.pixel_to_meter(gx, gy)
        vehicles.append(VehicleState(
            track_id=track_id,
            class_name=VEHICLE_CLASSES.get(class_id, "unknown"),
            bbox_xyxy=xyxy,
            center_px=(cx, cy),
            lat=lat, lon=lon,
            x_m=x_m, y_m=y_m,
        ))
    return vehicles


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
    its_task     = asyncio.create_task(_its_speed_poll_loop())

    yield
    task.cancel()
    refresh_task.cancel()
    its_task.cancel()
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


# ── CCTV 이름 한영 병기 ───────────────────────────────────────────────
def _korname_to_en(name: str) -> str:
    """ITS CCTV 한국어 이름에서 영어 약칭을 파싱해 괄호로 병기한다."""
    en_parts: list[str] = []

    m = re.search(r'국도\s*(\d+)\s*호선?', name)
    if m:
        en_parts.append(f"Nat'l Rt.{m.group(1)}")

    m = re.search(r'지방도\s*(\d+)\s*호?', name)
    if m:
        en_parts.append(f"Prov.Rt.{m.group(1)}")

    if re.search(r'고속(도로|국도)', name):
        en_parts.append("Expwy")

    if '상행' in name:
        en_parts.append("NB↑")
    elif '하행' in name:
        en_parts.append("SB↓")
    elif '양방향' in name:
        en_parts.append("Both↕")

    if not en_parts:
        return name
    return f"{name} ({' '.join(en_parts)})"


# ── CCTV 목록 ─────────────────────────────────────────────────────────
@app.get("/cctvs")
async def get_cctvs(
    minX: float = Query(126.93),
    maxX: float = Query(127.14),
    minY: float = Query(37.36),
    maxY: float = Query(37.56),
):
    """ITS API에서 현재 뷰 영역의 CCTV 위치·URL 목록 반환 (5분 캐시)"""
    cache_key = (round(minX, 3), round(maxX, 3), round(minY, 3), round(maxY, 3))
    if cache_key in _cctv_cache:
        return _cctv_cache[cache_key]

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
            items = _parse_its_items(resp.json())
            result = []
            for item in items:
                try:
                    lat = float(item.get("coordy") or 0)
                    lon = float(item.get("coordx") or 0)
                    if not (lat and lon):
                        continue
                    url  = item.get("cctvurl", "")
                    name = _korname_to_en(item.get("cctvname", ""))
                    result.append({
                        "id":      url or name,
                        "name":    name,
                        "lat":     lat,
                        "lon":     lon,
                        "cctvurl": url,
                        "heading": 0,    # ITS API 미제공 — calibration으로 업데이트
                        "fov_deg": 70,
                    })
                except (ValueError, TypeError):
                    continue
            logger.info("CCTV 조회 완료: %d개 (캐시 저장)", len(result))
            _cctv_cache[cache_key] = result
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
    _cam_version += 1
    analytics.reset()
    cam_key = roi_manager.camera_key(body.cctvurl)
    analytics.speed_scale = _load_speed_scale(cam_key)
    logger.info("속도 보정 계수 복원: %.4f (camera=%s)", analytics.speed_scale, cam_key)

    # BoT-SORT 내부 상태 리셋
    det = getattr(app.state, "detector", None)
    if det is not None:
        det.reset_tracker()

    # 새 카메라 위치 기준 ITS 구간속도 즉시 갱신 (비동기, 응답 안 기다림)
    global _its_speed_kph
    _its_speed_kph = None
    asyncio.create_task(_update_its_speed())

    # 노드링크 DB에서 가장 가까운 도로의 제한속도 자동 적용
    road = await asyncio.to_thread(get_road_info, body.lat, body.lon, _parse_road_name_hint(body.name))
    road_snap = await asyncio.to_thread(get_road_snap, body.lat, body.lon, _parse_road_name_hint(body.name))
    if road and road["max_spd"] > 0:
        analytics.speed_limit_kph = float(road["max_spd"])
        logger.info("노드링크 제한속도 적용: %d kph (%s)", road["max_spd"], road.get("road_name", ""))
    else:
        from config import SPEED_LIMIT_KPH
        analytics.speed_limit_kph = SPEED_LIMIT_KPH
    # FOV 삼각형과 동일한 우선순위: name_bearing → road_bearing
    # Prefer the snap-segment bearing. On curved roads the whole-link bearing can
    # point the FOV down the wrong branch even when snapping picked the right road.
    road_bearing = (
        road_snap["bearing_deg"]
        if road_snap and road_snap.get("bearing_deg") is not None
        else (road["bearing_deg"] if road else None)
    )
    name_bearing = _parse_name_bearing(body.name, road_bearing)
    effective_bearing = name_bearing if name_bearing is not None else road_bearing
    analytics.road_bearing_deg = effective_bearing

    if road_snap:
        logger.info(
            "road_snap 성공: 도로=%s snap=(%.4f,%.4f) cam_dist=%.1fm 폭=%.1fm %s",
            road_snap.get("road_name", "?"),
            road_snap["snap_lat"], road_snap["snap_lon"],
            road_snap.get("cam_dist_m", 0),
            road_snap.get("road_width_m", 0),
            "편도" if road_snap.get("is_oneway") else "양방향",
        )
    else:
        logger.warning(
            "road_snap 실패 — 카메라 GPS (%.4f, %.4f)를 snap으로 사용. "
            "nodelink DB 미커버 도로일 가능성 있음.",
            body.lat, body.lon,
        )
    _current_cam = {
        "lat": body.lat, "lon": body.lon,
        "name": body.name, "cctvurl": body.cctvurl,
        "snap_lat":     road_snap["snap_lat"]     if road_snap else body.lat,
        "snap_lon":     road_snap["snap_lon"]     if road_snap else body.lon,
        "road_width_m": road_snap["road_width_m"] if road_snap else None,
        "is_oneway":    road_snap["is_oneway"]    if road_snap else None,
        "has_name_bearing": name_bearing is not None,
        "road_bearing": road_bearing,
        "name_bearing": name_bearing,
        "road_pts":     road_snap["road_pts"]     if road_snap else None,
        "snap_along_m": road_snap["snap_along_m"] if road_snap else None,
    }

    # live_loop 스트림 전환 큐잉
    if body.cctvurl:
        await _camera_queue.put({
            "url":          body.cctvurl,
            "lat":          body.lat,
            "lon":          body.lon,
            "name":         body.name,
            "snap_lat":     _current_cam["snap_lat"],
            "snap_lon":     _current_cam["snap_lon"],
            "road_width_m": _current_cam["road_width_m"],
            "is_oneway":    _current_cam["is_oneway"],
            "road_bearing": _current_cam["road_bearing"],
            "name_bearing": _current_cam["name_bearing"],
            "road_pts":     _current_cam["road_pts"],
            "snap_along_m": _current_cam["snap_along_m"],
        })

    analytics.cam_lat = _current_cam["snap_lat"]
    analytics.cam_lon = _current_cam["snap_lon"]
    _transformer.update_gps_center(_current_cam["snap_lat"], _current_cam["snap_lon"], bearing_deg=effective_bearing or 0.0)

    logger.info("카메라 전환: %s (%.4f, %.4f) snap=(%.4f, %.4f) bearing=%.1f°",
                body.name, body.lat, body.lon,
                _current_cam["snap_lat"], _current_cam["snap_lon"], effective_bearing or 0.0)
    return {"ok": True, "road": road}


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
            items = _parse_its_items(resp.json())
        for item in items:
            if not name or item.get("cctvname") == name:
                return {"cctvurl": item.get("cctvurl", "")}
    except Exception as e:
        logger.warning("cctv-refresh 실패: %s", e)
    return {"cctvurl": ""}


# ── HLS CORS 프록시 ──────────────────────────────────────────────────
@app.get("/hls-proxy")
async def hls_proxy(request: Request, url: str = Query(...)):
    """
    브라우저가 ITS CCTV HLS 스트림을 직접 요청하면 CORS 차단됨.
    이 엔드포인트가 백엔드에서 ITS 서버로 요청을 중계하고
    m3u8 파일 내 세그먼트 URL을 이 프록시를 통해 다시 쓴다.
    """
    import urllib.parse

    proxy_base = str(request.base_url).rstrip("/") + "/hls-proxy?url="

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": url,
    }

    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
    except Exception as exc:
        logger.warning("hls-proxy 요청 실패: %s", exc)
        return Response(status_code=502, content=str(exc))

    content_type = resp.headers.get("content-type", "")

    # m3u8 플레이리스트: 세그먼트 URL을 프록시 경유로 재작성
    if "mpegurl" in content_type or url.split("?")[0].endswith(".m3u8"):
        lines = []
        for line in resp.text.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                # urljoin이 절대·루트·상대 경로를 모두 올바르게 처리
                seg_url = urllib.parse.urljoin(url, stripped)
                lines.append(proxy_base + urllib.parse.quote(seg_url, safe=""))
            else:
                lines.append(line)
        rewritten = "\n".join(lines)
        return Response(
            content=rewritten,
            media_type="application/vnd.apple.mpegurl",
            headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache"},
        )

    # ts 세그먼트: 그대로 스트리밍
    async def stream_segment():
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            async with client.stream("GET", url, headers=headers) as r:
                async for chunk in r.aiter_bytes(8192):
                    yield chunk

    return StreamingResponse(
        stream_segment(),
        media_type="video/MP2T",
        headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache"},
    )


# ── YOLO + BoT-SORT WebSocket ────────────────────────────────────────
@app.websocket("/ws/detect")
async def ws_detect(ws: WebSocket):
    """
    브라우저 <video> 캔버스 프레임(JPEG bytes) 수신
    → BoT-SORT 탐지+추적 + 어노테이션 → 결과 반환
    → analytics 업데이트 → 전체 클라이언트에 브로드캐스트
    """
    global _detect_clients
    await ws.accept()
    _detect_clients += 1

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

    logger.info("YOLO+boxmot 탐지 클라이언트 연결 (총 %d명)", _detect_clients)
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
            tracked, in_cnt, out_cnt, in_ids, out_ids = tracker.update(detections, (fw, fh))
            if fid % 10 == 1:
                logger.info(
                    "[BoT-SORT] frame=%d  tracked=%d  in=%d  out=%d",
                    fid, len(tracked), in_cnt, out_cnt,
                )

            # GPS 변환 + VehicleState 생성
            vehicles = _build_vehicles(tracked, frame_wh=(fw, fh))
            fid += 1
            result = analytics.update(fid, fid / FPS * 1000, vehicles, in_cnt, out_cnt, in_ids, out_ids)
            await _broadcast(_inject_its_speed(result.to_dict()))
            await ws.send_bytes(ann_bytes)

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning("ws/detect 오류: %s", e)
    finally:
        _detect_clients = max(0, _detect_clients - 1)
        # ws/detect 종료 후 live_loop가 깨끗한 상태로 boxmot를 재개할 수 있도록 리셋
        if _detect_clients == 0:
            det = getattr(app.state, "detector", None)
            if det is not None:
                det.reset_tracker()
                logger.info("ws/detect 종료 → boxmot 트래커 리셋 (live_loop 재개 준비)")
        logger.info("YOLO 탐지 클라이언트 해제 (총 %d명)", _detect_clients)


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
        f"#{_safe_tid(detections.tracker_id, i, i)} "
        f"{float(detections.confidence[i]):.0%}"
        for i in range(len(detections))
    ]
    annotated = _box_ann.annotate(frame.copy(), detections)
    annotated = _label_ann.annotate(annotated, detections, labels)
    cv2.putText(annotated, f"boxmot  {len(detections)} vehicles",
                (8, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 80), 2)

    _frame_count += 1
    _, buf = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    return buf.tobytes(), detections, fw, fh


# ── ROI 관리 ──────────────────────────────────────────────────────────
@app.get("/roi/{camera_key}")
async def get_roi(camera_key: str):
    """카메라의 저장된 ROI 반환 (정규화 좌표 0~1)."""
    # camera_key 대신 cctvurl hash를 직접 받음
    # 프론트엔드에서 cctvurl을 통해 키를 계산하여 전달
    if not roi_manager._CONFIG_PATH.exists():
        return {"polygon": None}
    try:
        data = json.loads(roi_manager._CONFIG_PATH.read_text(encoding="utf-8"))
        entry = data.get(camera_key)
        return {"polygon": entry.get("polygon") if entry else None}
    except Exception:
        return {"polygon": None}


class RoiBody(BaseModel):
    cctvurl: str
    polygon: list[list[float]]


@app.post("/roi")
async def save_roi(body: RoiBody):
    """카메라의 ROI 저장 (정규화 좌표 0~1)."""
    if len(body.polygon) < 3:
        return {"ok": False, "error": "polygon must have at least 3 points"}
    roi_manager.save_roi(body.cctvurl, body.polygon, auto=False)
    # 현재 활성 카메라와 같으면 detector에 즉시 적용
    det = getattr(app.state, "detector", None)
    if det is not None and _current_cam and _current_cam.get("cctvurl") == body.cctvurl:
        det.set_roi(body.polygon)
    return {"ok": True}


# ── Calibration 관리 ──────────────────────────────────────────────────────
class CalibBody(BaseModel):
    cctvurl: str                          # camera URL (camera_key는 서버에서 계산)
    pixel_pts: list[list[float]]          # [[u,v] × 4]  실제 픽셀
    gps_pts:   list[list[float]]          # [[lat,lon] × 4]
    frame_width:  int = 640
    frame_height: int = 360


@app.get("/calibration/{camera_key}")
async def get_calibration(camera_key: str):
    """저장된 캘리브레이션 데이터 반환."""
    if not CALIBRATION_PATH.exists():
        return {"calibration": None}
    try:
        data = json.loads(CALIBRATION_PATH.read_text(encoding="utf-8"))
        return {"calibration": data.get(camera_key)}
    except Exception:
        return {"calibration": None}


@app.post("/calibration")
async def save_calibration(body: CalibBody):
    """4-point calibration 저장 + 현재 카메라면 transformer 즉시 업데이트."""
    if len(body.pixel_pts) != 4 or len(body.gps_pts) != 4:
        return {"ok": False, "error": "4쌍의 대응점이 필요합니다"}

    cam_key = roi_manager.camera_key(body.cctvurl)

    # JSON 파일 저장
    data: dict = {}
    if CALIBRATION_PATH.exists():
        try:
            data = json.loads(CALIBRATION_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    from datetime import datetime, timezone
    data[cam_key] = {
        "pixel_pts": body.pixel_pts,
        "gps_pts":   body.gps_pts,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    CALIBRATION_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 이미지 4 코너 → GPS 변환 (perspective transform 인라인 계산)
    corner_gps_pts: list[list[float]] = []
    try:
        H, _ = cv2.findHomography(np.float32(body.pixel_pts), np.float32(body.gps_pts))
        if H is not None:
            corners_px = np.float32([
                [[0, 0]],
                [[body.frame_width, 0]],
                [[body.frame_width, body.frame_height]],
                [[0, body.frame_height]],
            ])
            res = cv2.perspectiveTransform(corners_px, H)
            raw_pts = [[float(r[0, 0]), float(r[0, 1])] for r in res]

            # convex hull 순서로 정렬 → 카메라 방향과 무관하게 폴리곤 자기교차 방지
            # lon(x), lat(y) 순으로 hull 계산 후 lat,lon으로 되돌림
            coords = np.float32([[p[1], p[0]] for p in raw_pts])  # lon, lat
            hull   = cv2.convexHull(coords.reshape(-1, 1, 2))
            corner_gps_pts = [[float(h[0][1]), float(h[0][0])] for h in hull]  # lat, lon
    except Exception as exc:
        logger.warning("corner_gps_pts 계산 실패: %s", exc)

    # 현재 활성 카메라와 같으면 transformer 즉시 업데이트
    if _current_cam and roi_manager.camera_key(_current_cam.get("cctvurl", "")) == cam_key:
        try:
            _transformer.update_from_calibration(body.pixel_pts, body.gps_pts)
            logger.info("Transformer 캘리브레이션 적용: %s", cam_key)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    return {"ok": True, "camera_key": cam_key, "corner_gps_pts": corner_gps_pts}


@app.delete("/calibration/{camera_key}")
async def delete_calibration(camera_key: str):
    """캘리브레이션 삭제 (기본 근사값으로 롤백)."""
    if CALIBRATION_PATH.exists():
        try:
            data = json.loads(CALIBRATION_PATH.read_text(encoding="utf-8"))
            data.pop(camera_key, None)
            CALIBRATION_PATH.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass
    # 현재 카메라면 GPS center 근사값으로 롤백
    if _current_cam and roi_manager.camera_key(_current_cam.get("cctvurl", "")) == camera_key:
        _transformer.update_gps_center(_current_cam["lat"], _current_cam["lon"], bearing_deg=analytics.road_bearing_deg or 0.0)
    return {"ok": True}


@app.delete("/roi/{camera_key}")
async def delete_roi(camera_key: str):
    """카메라의 ROI 삭제."""
    if not roi_manager._CONFIG_PATH.exists():
        return {"ok": True}
    try:
        data = json.loads(roi_manager._CONFIG_PATH.read_text(encoding="utf-8"))
        data.pop(camera_key, None)
        roi_manager._CONFIG_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass
    det = getattr(app.state, "detector", None)
    if det is not None:
        det.set_roi(None)
    return {"ok": True}


# ── MJPEG 라이브 스트림 ───────────────────────────────────────────────
@app.get("/video-stream")
async def video_stream():
    """
    백엔드가 이미 OpenCV로 읽고 있는 프레임을 MJPEG multipart로 브라우저에 전달.
    HLS CORS 문제를 우회하고, 토큰 만료와 무관하게 항상 최신 프레임을 전송한다.
    """
    async def generate():
        boundary = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
        last_sent: bytes | None = None
        while True:
            jpeg = _latest_frame_jpeg
            sleep_s = 1.0 / max(_stream_fps, 1.0)
            if jpeg is None or jpeg is last_sent:
                await asyncio.sleep(sleep_s)
                continue
            last_sent = jpeg
            yield boundary + jpeg + b"\r\n"
            await asyncio.sleep(sleep_s)

    return StreamingResponse(
        generate(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"},
    )


# ── MJPEG YOLO 어노테이션 스트림 ─────────────────────────────────────
@app.get("/video-stream-yolo")
async def video_stream_yolo():
    """live_loop의 YOLO 탐지 결과(박스+레이블)를 MJPEG으로 스트리밍."""
    async def generate():
        boundary = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
        last_sent: bytes | None = None
        while True:
            jpeg = _latest_annotated_jpeg
            sleep_s = 1.0 / max(_stream_fps, 1.0)
            if jpeg is None or jpeg is last_sent:
                await asyncio.sleep(sleep_s)
                continue
            last_sent = jpeg
            yield boundary + jpeg + b"\r\n"
            await asyncio.sleep(sleep_s)

    return StreamingResponse(
        generate(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"},
    )


# ── 노드링크 근처 노드 조회 ───────────────────────────────────────────
@app.get("/nodelink/nodes")
async def nearby_nodes(
    lat: float = Query(...),
    lon: float = Query(...),
    radius_km: float = Query(0.3),
):
    """캘리브레이션 GPS 스냅용: 카메라 주변 도로 노드 목록 반환."""
    from nodelink import get_nodes_near
    try:
        nodes = await asyncio.to_thread(get_nodes_near, lat, lon, min(radius_km, 1.0))
        return {"nodes": nodes}
    except FileNotFoundError:
        return {"nodes": []}


# ── 카메라 정지 ──────────────────────────────────────────────────────
@app.post("/stop-camera")
async def stop_camera():
    """프론트엔드 카메라 패널 종료 시 스트림 해제 + YOLO 처리 중지."""
    global _current_cam, _latest_frame_jpeg, _latest_annotated_jpeg
    stream = getattr(app.state, "stream", None)
    if stream:
        await asyncio.to_thread(stream.release)
    det = getattr(app.state, "detector", None)
    if det is not None:
        det.reset_tracker()
    _current_cam = None
    _latest_frame_jpeg = None
    _latest_annotated_jpeg = None
    logger.info("카메라 스트림 해제 (프론트엔드 요청)")
    return {"ok": True}


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
    det = getattr(app.state, "detector", None)
    tracker_info = det.tracker_info if det else {}
    return {
        "profile": RUNTIME_PROFILE_NAME,
        "backendFps": FPS,
        "jpegQuality": JPEG_QUALITY,
        "captureIntervalMs": CAPTURE_INTERVAL_MS,
        "captureWidth": CAPTURE_WIDTH,
        "captureQuality": CAPTURE_QUALITY,
        "maxInFlight": MAX_IN_FLIGHT,
        "tracker": tracker_info.get("tracker", "unknown"),
        "trackerTier": tracker_info.get("tier", "unknown"),
        "inferenceBackend": tracker_info.get("backend", "unknown"),
    }


# ── ITS 속도 비교 주입 ────────────────────────────────────────────────
def _inject_its_speed(payload: dict) -> dict:
    """FrameAnalytics dict에 ITS 구간속도 및 오차율, 보정 계수 추가."""
    payload["speed_scale"] = round(analytics.speed_scale, 4)
    payload["speed_scale_converged"] = analytics.speed_scale_converged

    # 화면 비교용: 10분 rolling average (calibrate_from_its와 동일 창)
    # avg_speed_kph(순간값)이 아니라 같은 시간축의 평균끼리 비교해야 의미 있음
    with analytics._lock:
        now = time.monotonic()
        recent = [s for s, t in analytics._speed_samples if now - t <= 600.0]
    our_rolling_avg = round(sum(recent) / len(recent), 1) if len(recent) >= 5 else 0.0
    payload["our_avg_kph"] = our_rolling_avg

    if _its_speed_kph is None:
        return payload
    payload["its_speed_kph"] = _its_speed_kph
    if our_rolling_avg > 0 and _its_speed_kph > 0:
        payload["speed_error_pct"] = round(
            (our_rolling_avg - _its_speed_kph) / _its_speed_kph * 100, 1
        )
    return payload


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
async def _refresh_stream_url(stream: "VideoStream") -> None:
    """ITS API에서 현재 카메라 URL을 즉시 갱신. 토큰 만료 복구용."""
    cam = _current_cam
    if cam is None or not cam.get("name"):
        return
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
            items = _parse_its_items(resp.json())
        for item in items:
            if item.get("cctvname") == name:
                new_url = item.get("cctvurl", "")
                if new_url:
                    logger.info("HLS URL 긴급 갱신 (토큰 만료): %s", name)
                    _current_cam["cctvurl"] = new_url
                    await _camera_queue.put({
                        "url":          new_url,
                        "lat":          lat,
                        "lon":          lon,
                        "name":         name,
                        "snap_lat":     cam.get("snap_lat", lat),
                        "snap_lon":     cam.get("snap_lon", lon),
                        "road_width_m": cam.get("road_width_m"),
                        "is_oneway":    cam.get("is_oneway"),
                        "road_bearing": cam.get("road_bearing"),
                        "name_bearing": cam.get("name_bearing"),
                        "road_pts":     cam.get("road_pts"),
                        "snap_along_m": cam.get("snap_along_m"),
                    })
                break
    except Exception as e:
        logger.warning("HLS URL 긴급 갱신 실패: %s", e)


async def live_loop(detector, stream) -> None:
    from tracker import VehicleTracker

    tracker        = VehicleTracker()
    target_interval = 1.0 / FPS
    skip_budget    = 0
    _reconnect_fails = 0  # 연속 reconnect 실패 횟수
    global _stream_fps

    logger.info("Live 루프 대기 중 — 지도에서 CCTV를 클릭하여 스트림을 시작하세요")

    while True:
        # 카메라 전환 요청 처리 — 큐에 쌓인 항목 중 최신 것만 사용
        if not _camera_queue.empty():
            cam = _camera_queue.get_nowait()
            # 큐에 추가 항목이 있으면 가장 마지막 것만 사용 (중간 전환 스킵)
            while not _camera_queue.empty():
                cam = _camera_queue.get_nowait()
            try:
                await asyncio.to_thread(stream.switch_to, cam["url"])
                snap_lat = cam.get("snap_lat", cam["lat"])
                snap_lon = cam.get("snap_lon", cam["lon"])
                snap_ok = snap_lat != cam["lat"] or snap_lon != cam["lon"]
                if not snap_ok:
                    logger.warning(
                        "road_snap 실패 — snap이 카메라 GPS로 fallback됨 (%.4f, %.4f). "
                        "polygon이 도로 옆에 표시될 수 있음.",
                        cam["lat"], cam["lon"],
                    )
                else:
                    cam_dist_m = ((snap_lat - cam["lat"]) * 110574) ** 2
                    cam_dist_m += ((snap_lon - cam["lon"]) * 111320 * math.cos(math.radians(snap_lat))) ** 2
                    cam_dist_m = cam_dist_m ** 0.5
                    logger.info("snap 성공: 카메라→도로 거리 %.1fm", cam_dist_m)
                _transformer.update_gps_center(snap_lat, snap_lon, bearing_deg=analytics.road_bearing_deg or 0.0)
                _transformer.set_road_corridor(cam.get("road_pts"), cam.get("snap_along_m"))
                analytics.cam_lat = snap_lat
                analytics.cam_lon = snap_lon
                tracker = VehicleTracker()
                analytics.reset()
                skip_budget = 0
                _reconnect_fails = 0
                _stream_fps = stream.fps or FPS
                target_interval = 1.0 / _stream_fps
                logger.info("스트림 FPS: %.1f (target_interval=%.3fs)", _stream_fps, target_interval)

                # 수동으로 저장된 ROI만 적용 (auto-estimate는 탐지 정확도를 해칠 수 있어 자동 적용 안 함)
                cam_url = cam["url"]
                saved_roi = roi_manager.load_roi(cam_url)
                det = getattr(app.state, "detector", None)
                if det is not None:
                    det.set_roi(saved_roi)  # None이면 전체 프레임 탐지

                # 저장된 speed_scale 복원
                cam_key = roi_manager.camera_key(cam_url)
                analytics.speed_scale = _load_speed_scale(cam_key)
                logger.info("속도 보정 계수 복원: %.4f (camera=%s)", analytics.speed_scale, cam_key)

                # 저장된 calibration 자동 적용
                _manual_cal_loaded = False
                if CALIBRATION_PATH.exists():
                    try:
                        cal_data = json.loads(CALIBRATION_PATH.read_text(encoding="utf-8"))
                        cal = cal_data.get(cam_key)
                        if cal:
                            _transformer.update_from_calibration(
                                cal["pixel_pts"], cal["gps_pts"]
                            )
                            logger.info("저장된 캘리브레이션 적용: %s", cam_key)
                            _manual_cal_loaded = True
                    except Exception as exc:
                        logger.warning("캘리브레이션 로드 실패: %s", exc)

                # 스트림 준비 완료 신호를 모든 WS 클라이언트에 전송
                road_info = await asyncio.to_thread(
                    get_road_info, cam["lat"], cam["lon"], _parse_road_name_hint(cam.get("name", ""))
                )

                # 도로폭 계산 (카메라 공통 — manual/auto-calib 무관하게 broadcast에 사용)
                _cam_road_width_m: float | None = cam.get("road_width_m")
                if _cam_road_width_m is None:
                    _ri_lanes = road_info["lanes"] if road_info and road_info.get("lanes") else 2
                    _ri_rank  = str(road_info.get("road_rank", "")) if road_info else ""
                    _ri_lane_w = 3.5 if _ri_rank in ("101", "102", "103") else (3.25 if _ri_rank in ("104", "105") else 3.0)
                    _cam_road_width_m = max(1, _ri_lanes) * 2 * _ri_lane_w

                # 수동 캘리브레이션 없으면 차선 감지 자동 보정 예약
                global _auto_calib_attempts, _auto_calib_road_width_m
                if not _manual_cal_loaded:
                    _auto_calib_road_width_m = _cam_road_width_m
                    oneway = cam.get("is_oneway", False)
                    logger.info("도로폭: %.1fm (%s)", _cam_road_width_m, "편도" if oneway else "양방향")
                    _auto_calib_attempts = 5  # 최대 5프레임 시도
                road_bearing_for_ui = (
                    cam.get("road_bearing")
                    if cam.get("road_bearing") is not None
                    else (road_info["bearing_deg"] if road_info else None)
                )
                name_bearing_for_ui = (
                    cam.get("name_bearing")
                    if cam.get("name_bearing") is not None
                    else _parse_name_bearing(cam.get("name", ""), road_bearing_for_ui)
                )
                await _broadcast({
                    "type": "camera_ready",
                    "name": cam.get("name", ""),
                    "roi": saved_roi,
                    "camera_key": cam_key,
                    "calibrated": _transformer.is_calibrated,
                    "road_name": road_info["road_name"] if road_info else None,
                    "road_lanes": road_info["lanes"] if road_info else None,
                    "road_max_spd": road_info["max_spd"] if road_info else None,
                    "road_bearing": road_bearing_for_ui,
                    "name_bearing": name_bearing_for_ui,
                    "snap_lat":     snap_lat,
                    "snap_lon":     snap_lon,
                    "road_width_m": _cam_road_width_m,
                    "road_pts":     cam.get("road_pts"),
                    "snap_along_m": cam.get("snap_along_m"),
                })
                logger.info("카메라 전환 완료, camera_ready 신호 전송")
            except RuntimeError as e:
                logger.warning("카메라 전환 실패: %s", e)
                await _broadcast({"type": "camera_error", "message": str(e)})

        if not stream.is_open:
            await asyncio.sleep(0.5)
            continue

        frame_id, frame = stream.read_frame()
        if frame is None:
            ok = await stream.reconnect()
            if ok:
                _reconnect_fails = 0
            else:
                _reconnect_fails += 1
                if _reconnect_fails >= 3:
                    logger.warning("연속 %d회 reconnect 실패 — HLS 토큰 만료 의심, URL 긴급 갱신 시도", _reconnect_fails)
                    _reconnect_fails = 0
                    await _refresh_stream_url(stream)
            continue

        # 차선 감지 자동 캘리브레이션 (수동 캘리브 없을 때, 최초 N프레임 시도)
        if _auto_calib_attempts > 0 and frame is not None:
            _auto_calib_attempts -= 1
            bearing = analytics.road_bearing_deg or 0.0
            ok, used_bearing, calib_info = await asyncio.to_thread(
                _transformer.auto_calibrate_from_frame,
                frame, bearing, _auto_calib_road_width_m,
                _current_cam.get("has_name_bearing", False),
                _current_cam.get("lat"),
                _current_cam.get("lon"),
            )
            if ok:
                logger.info("차선 감지 자동 캘리브레이션 완료 (bearing=%.1f°)", used_bearing)
                _auto_calib_attempts = 0
                if analytics.road_bearing_deg is None:
                    # 노드링크 bearing 없음 → 자동 캘리브 bearing 사용
                    analytics.road_bearing_deg = used_bearing
                elif abs((used_bearing - bearing + 180) % 360 - 180) > 90:
                    # VP 반전 감지 → 갱신
                    analytics.road_bearing_deg = used_bearing
                await _broadcast({
                    "type": "auto_calibrated",
                    "heading": used_bearing,
                    **(calib_info or {}),
                })
            elif _auto_calib_attempts == 0:
                logger.info("차선 감지 실패 — GPS 근사 캘리브레이션 유지 (road_width=%.1fm)",
                            _auto_calib_road_width_m)

        # MJPEG 스트림용 버퍼 업데이트 (모든 프레임, 탐지 여부 무관)
        global _latest_frame_jpeg
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
        if ok:
            _latest_frame_jpeg = buf.tobytes()

        if skip_budget > 0:
            skip_budget -= 1
            continue

        # ws/detect 활성 시 boxmot 트래커 공유 충돌 방지 — 프레임만 드레인
        if _detect_clients > 0:
            await asyncio.sleep(target_interval)
            continue

        t0 = time.perf_counter()
        payload = await asyncio.to_thread(
            _live_process, frame_id, frame, detector, tracker, stream.fps
        )
        if payload:
            await _broadcast(payload)

        elapsed = time.perf_counter() - t0
        if elapsed > target_interval:
            skip_budget = min(int(elapsed / target_interval) - 1, 5)
        else:
            await asyncio.sleep(target_interval - elapsed)


def _live_process(frame_id, frame, detector, tracker, fps: float = 30.0) -> dict | None:
    global _frame_count, _latest_annotated_jpeg

    h, w = frame.shape[:2]
    timestamp_ms = frame_id / fps * 1000  # 처리 지연 무관, 프레임 번호 기반

    tracked = detector.track(frame)
    tracked, in_cnt, out_cnt, in_ids, out_ids = tracker.update(tracked, (w, h))

    # YOLO annotated 프레임 생성 → /video-stream-yolo 버퍼
    try:
        labels = [
            f"#{int(tracked.tracker_id[i])} {VEHICLE_CLASSES[int(tracked.class_id[i])]} "
            f"{float(tracked.confidence[i]):.0%}"
            for i in range(len(tracked))
        ]
        ann = _box_ann.annotate(frame.copy(), tracked)
        ann = _label_ann.annotate(ann, tracked, labels)
        cv2.putText(ann, f"vehicles: {len(tracked)}", (8, 28),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 80), 2)
        ok, buf = cv2.imencode(".jpg", ann, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
        if ok:
            _latest_annotated_jpeg = buf.tobytes()
    except Exception:
        pass

    vehicles = _build_vehicles(tracked, frame_wh=(w, h))
    _frame_count += 1
    result = analytics.update(frame_id, timestamp_ms, vehicles, in_cnt, out_cnt, in_ids, out_ids)
    return _inject_its_speed(result.to_dict())


# ── ITS 구간속도 폴링 ─────────────────────────────────────────────────
async def _its_speed_poll_loop() -> None:
    """ITS_POLL_INTERVAL(기본 5분)마다 ITS 구간속도 갱신. 카메라 선택 시에만 실제 조회."""
    while True:
        await asyncio.sleep(ITS_POLL_INTERVAL)
        await _update_its_speed()


# ── HLS URL 자동 갱신 (서버사이드 live_loop 용) ───────────────────────
async def hls_refresh_loop(stream) -> None:
    """ITS HLS 토큰 만료 전 URL 갱신 (HLS_REFRESH_INTERVAL, 기본 30분)."""
    while True:
        await asyncio.sleep(HLS_REFRESH_INTERVAL)
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
                items = _parse_its_items(resp.json())
            for item in items:
                if item.get("cctvname") == name:
                    new_url = item.get("cctvurl", "")
                    if new_url and new_url != stream.url:
                        logger.info("HLS URL 갱신: %s", name)
                        await _camera_queue.put({
                            "url":          new_url,
                            "lat":          lat,
                            "lon":          lon,
                            "name":         name,
                            "snap_lat":     cam.get("snap_lat", lat),
                            "snap_lon":     cam.get("snap_lon", lon),
                            "road_width_m": cam.get("road_width_m"),
                            "is_oneway":    cam.get("is_oneway"),
                            "road_bearing": cam.get("road_bearing"),
                            "name_bearing": cam.get("name_bearing"),
                            "road_pts":     cam.get("road_pts"),
                            "snap_along_m": cam.get("snap_along_m"),
                        })
                        _current_cam["cctvurl"] = new_url
                    break
        except Exception as e:
            logger.warning("HLS URL 갱신 실패: %s", e)


# ── 진입점 ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
