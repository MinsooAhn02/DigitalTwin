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
      5. nodelink lanes × 차선폭으로 도로폭 계산
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
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
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

from analytics import TrafficAnalytics, VehicleState, set_speed_debug, speed_debug_status
from transform import PerspectiveTransformer, CALIBRATION_PATH
import roi_manager
import metrics
from perf import PerfStats, yappi_start, yappi_stop, YAPPI_AVAILABLE

SPEED_SCALE_PATH    = Path(__file__).resolve().parent / "speed_scale.json"
VEHICLE_CALIB_PATH  = Path(__file__).resolve().parent / "vehicle_calib.json"
CAMERA_POSE_PATH    = Path(__file__).resolve().parent / "camera_pose.json"

# JSON 설정 파일들의 read-modify-write를 원자화하는 공용 락 (TOCTOU 방지).
# 동기 I/O이므로 asyncio.to_thread 안에서 호출하거나, 이벤트 루프에서 짧게 실행해야 한다.
_json_file_lock = threading.Lock()

# _live_process / _yolo_detect_annotate 두 스레드가 동시에 증가시키는 카운터 보호
_frame_count_lock = threading.Lock()


def _atomic_update_json(path: Path, key: str, value: dict) -> None:
    """path JSON 파일의 key 항목을 원자적으로 read-modify-write (TOCTOU 방지)."""
    with _json_file_lock:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        data[key] = value
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _atomic_delete_json(path: Path, key: str) -> None:
    """path JSON 파일의 key 항목을 원자적으로 삭제 (TOCTOU 방지)."""
    with _json_file_lock:
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        if key not in data:
            return
        data.pop(key)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_vehicle_calib(cam_key: str) -> dict | None:
    """카메라별 저장된 vehicle scale model 로드. 없으면 None."""
    try:
        if VEHICLE_CALIB_PATH.exists():
            data = json.loads(VEHICLE_CALIB_PATH.read_text(encoding="utf-8"))
            entry = data.get(cam_key)
            if entry and "B" in entry and "C" in entry:
                return entry
    except Exception as exc:
        logger.debug("vehicle_calib 로드 실패: %s", exc)
    return None


def _save_vehicle_calib(cam_key: str, params: dict) -> None:
    """vehicle scale model을 camera_key별로 JSON에 저장."""
    try:
        B, C = float(params["B"]), float(params["C"])
        value = {
            **params,
            "vp_y":       round(-C / B, 2),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        _atomic_update_json(VEHICLE_CALIB_PATH, cam_key, value)
        logger.info("vehicle_calib 저장: key=%s B=%.5f vp_y=%.1f", cam_key, B, -C / B)
    except Exception as exc:
        logger.warning("vehicle_calib 저장 실패: %s", exc)


def _load_camera_pose(cam_key: str) -> dict | None:
    """카메라별 저장된 road-model 포즈 로드. 없으면 None (Phase 12)."""
    try:
        if CAMERA_POSE_PATH.exists():
            data = json.loads(CAMERA_POSE_PATH.read_text(encoding="utf-8"))
            entry = data.get(cam_key)
            if entry and "H_m" in entry and "pitch_deg" in entry:
                return entry
    except Exception as exc:
        logger.debug("camera_pose 로드 실패: %s", exc)
    return None


def _save_camera_pose(cam_key: str, params: dict) -> None:
    """road-model 포즈를 camera_key별 JSON에 저장 (다음 세션 prior 재사용)."""
    try:
        with _json_file_lock:
            data: dict = {}
            if CAMERA_POSE_PATH.exists():
                try:
                    data = json.loads(CAMERA_POSE_PATH.read_text(encoding="utf-8"))
                except Exception:
                    pass
            prev = data.get(cam_key, {})
            data[cam_key] = {
                **params,
                "n_updates":  int(prev.get("n_updates", 0)) + 1,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            CAMERA_POSE_PATH.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        logger.info("camera_pose 저장: key=%s H=%.1f pitch=%.1f residual=%.1fpx",
                    cam_key, params.get("H_m", 0), params.get("pitch_deg", 0),
                    params.get("residual_px", -1))
    except Exception as exc:
        logger.warning("camera_pose 저장 실패: %s", exc)


def _compute_roi_gps_ring() -> list[list[float]] | None:
    """현재 활성 카메라의 ROI를 GPS ring으로 변환 (Phase 3).

    ROI 정규화 좌표 → pixel → GPS (수평선 초과 점은 FAR_CAP_M으로 clamp).
    캘리브레이션/ROI가 없으면 None 반환.
    """
    if _current_cam is None:
        return None
    url = _current_cam.get("cctvurl", "")
    roi = roi_manager.load_roi(url)
    if not roi or len(roi) < 3:
        return None
    try:
        # 최근 프레임 크기 — transformer에 frame_w/h 저장돼 있으면 사용, 아니면 기본값
        w = getattr(_transformer, "_frame_w", None) or 640
        h = getattr(_transformer, "_frame_h", None) or 360
        return _transformer.roi_to_gps_ring(roi, int(w), int(h))
    except Exception as exc:
        logger.debug("roi_to_gps_ring 실패: %s", exc)
        return None


def _apply_fov_ema(new_info: dict) -> dict:
    """Phase 4: FOV 파라미터(near_m, far_m, road_width_m)에 느린 EMA 적용.

    수동 cali가 있으면 호출하지 않음 (main.py에서 guard).
    첫 FOV_EMA_MIN_SAMPLES 회는 그냥 고정, 이후 EMA alpha로 조금씩 이동.
    """
    global _accepted_fov, _fov_ema_samples
    _fov_ema_samples += 1
    keys = ("near_m", "far_m", "road_width_m")
    if not _accepted_fov:
        # 최초 채택 — 고정
        _accepted_fov = {k: new_info[k] for k in keys if k in new_info}
        return {**new_info, **_accepted_fov}
    if _fov_ema_samples < FOV_EMA_MIN_SAMPLES:
        # 데이터 부족 — 초기값 유지
        return {**new_info, **_accepted_fov}
    # 충분히 쌓임 → EMA로 조금씩 이동
    updated = {}
    changed = False
    for k in keys:
        if k not in new_info or k not in _accepted_fov:
            continue
        ema_val = _accepted_fov[k] * (1.0 - FOV_EMA_ALPHA) + new_info[k] * FOV_EMA_ALPHA
        if abs(ema_val - _accepted_fov[k]) > 0.05:  # 미세 변화는 broadcast 생략 지원용
            changed = True
        updated[k] = round(ema_val, 2)
    if changed:
        _accepted_fov.update(updated)
    return {**new_info, **_accepted_fov}


def _load_speed_scale(cam_key: str) -> tuple[float, bool]:
    """카메라별 저장된 속도 보정 계수 로드. 반환: (scale, found). 없으면 (1.0, False)."""
    try:
        if SPEED_SCALE_PATH.exists():
            data = json.loads(SPEED_SCALE_PATH.read_text(encoding="utf-8"))
            entry = data.get(cam_key)
            if entry:
                return float(entry.get("speed_scale", 1.0)), True
    except Exception as exc:
        logger.debug("speed_scale 로드 실패 (기본 1.0 사용): %s", exc)
    return 1.0, False


def _save_speed_scale(cam_key: str, scale: float) -> None:
    """속도 보정 계수를 camera_key별로 JSON에 저장."""
    try:
        _atomic_update_json(SPEED_SCALE_PATH, cam_key, {
            "speed_scale": scale,
            "updated_at":  datetime.now(timezone.utc).isoformat(),
        })
    except Exception as exc:
        logger.warning("speed_scale 저장 실패: %s", exc)
from nodelink import get_road_info, get_road_snap
from config import (
    CAPTURE_INTERVAL_MS,
    CAPTURE_QUALITY,
    CAPTURE_WIDTH,
    CONGESTION_EPS_M,
    FPS,
    HISTORY_RETENTION_DAYS,
    HISTORY_SAMPLE_S,
    HLS_REFRESH_INTERVAL,
    EX_API_KEY,
    ITS_API_KEY,
    ITS_BASE_URL,
    ITS_POLL_INTERVAL,
    ITS_TRAFFIC_URL,
    JPEG_QUALITY,
    MAX_IN_FLIGHT,
    RUNTIME_PROFILE_NAME,
    SCALE_MIN_OBS,
    SCALE_MIN_OBS_SPARSE,
    SCALE_SPARSE_AFTER_FRAMES,
    VEHICLE_CLASSES,
    BEARING_REFINE_INTERVAL_FRAMES,
    BEARING_BROADCAST_MIN_DEG,
    FOV_EMA_MIN_SAMPLES,
    FOV_EMA_ALPHA,
    WARMUP_MAX_S,
)
import camera_pose
from congestion import compute_clusters
from history import HistoryStore, SnapshotRow, retention_cutoff

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

analytics    = TrafficAnalytics()
_transformer = PerspectiveTransformer()
# ① 깊이별 속도 보정 함수 배선 (싱글톤이므로 1회만 설정)
analytics.depth_corr_fn = _transformer.speed_correction_at
# 실행 중 자동 측정 수집기 — make dev로 실행 후 카메라를 보면 자동으로 누적되고
# history_sampler_loop(30s)가 backend/eval_*.csv + eval_summary.json 으로 flush.
# GET /eval/report 로 즉시 스냅샷, POST /eval/reset 로 초기화.
_metrics     = metrics.LiveMetrics()
analytics.speed_obs_fn = lambda tid, y, r: _metrics.add_speed_obs(tid, y, r)
# 프레임별 파이프라인 타이밍 수집기 — 100프레임마다 perf_log.jsonl 자동 기록
_perf_stats  = PerfStats()
_clients: set[WebSocket] = set()

_camera_queue: asyncio.Queue = asyncio.Queue()

_frame_count: int = 0
_scale_switch_frame: int = 0   # 카메라 전환 시점의 _frame_count (적응형 min_obs 기준)
_detect_clients: int = 0   # ws/detect 활성 연결 수 — live_loop 브로드캐스트 억제용

# 현재 선택된 카메라 정보 (lat, lon, name, cctvurl)
_current_cam: dict | None = None

# 라이브 시청 활성 여부 — 프론트가 (카메라 선택 × 페이지 visible) 상태를 보고함.
# False면 live_loop 가 YOLO/broadcast 를 스킵해 미시청 시 GPU 낭비를 막는다.
# (백그라운드 모니터는 이 플래그와 무관하게 상시 동작)
_live_viewer_active: bool = False

# ── History 저장 & 정체 클러스터 ([B]/[C]) ─────────────────────────────
_history = HistoryStore(Path(__file__).resolve().parent / "history.sqlite")
# 라이브 카메라의 최신 FrameAnalytics 캐시 (샘플러가 읽어 저장).
# _broadcast 에서 부작용 없이 갱신 — 기존 _latest_frame_jpeg 와 동일 패턴.
_latest_live_analytics: dict | None = None
# 직전 broadcast 한 클러스터 시그니처 — 변경 시에만 재전송.
_last_cluster_sig: str | None = None
_cam_version: int = 0

# MJPEG 스트림용 최신 프레임 버퍼
_latest_frame_jpeg: bytes | None = None       # 원본 프레임
_latest_annotated_jpeg: bytes | None = None   # YOLO 어노테이션 프레임

_box_ann   = sv.BoxAnnotator(thickness=2)
_label_ann = sv.LabelAnnotator(text_scale=0.4, text_thickness=1, text_padding=3)

# 차선 감지 자동 캘리브레이션 상태
_auto_calib_road_width_m: float = 7.0  # lanes × 2 × lane_width_m
_auto_calib_road_rank: str = ""   # NodeLink road_rank (차선 표시 규격 선택용)
# 마지막 auto_calibrated 브로드캐스트 내용 (bearing 재보정 시 재사용)
_last_calib_info: dict = {}
_last_broadcast_bearing: float = 0.0

# Phase 4: polygon 안정화 — 초기값 고정 + 느린 EMA
_accepted_fov: dict = {}          # 최초 채택된 FOV 파라미터 (near_m, far_m, road_width_m)
_fov_ema_samples: int = 0         # 자동 추정값 수신 횟수 (임계치 이후 EMA 반영)

# ITS API 응답 캐시 (TTL 5분, 최대 50개 bbox 조합)
_cctv_cache: TTLCache = TTLCache(maxsize=50, ttl=300)

# ITS 구간속도 (5분 주기 폴링) — None이면 데이터 없음
_its_speed_kph: float | None = None

# 현재 스트림 실제 FPS — MJPEG 슬립 간격과 live_loop target_interval에 반영
_stream_fps: float = float(FPS)


def _safe_tid(tracker_id_arr, idx: int, fallback: int) -> int:
    """BoT-SORT가 미확정 트랙에 np.nan을 반환할 때 ValueError 방지."""
    if tracker_id_arr is None or idx >= len(tracker_id_arr):
        return fallback
    try:
        v = int(tracker_id_arr[idx])
        return fallback if math.isnan(float(v)) else v
    except (TypeError, ValueError, IndexError):
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
            logger.info("속도 보정 계수 갱신: %.4f (ITS %.1f kph)", new_scale, result)
            if _current_cam:
                cam_key = roi_manager.camera_key(_current_cam.get("cctvurl", ""))
                _save_speed_scale(cam_key, new_scale)


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


# 마지막 ITS 조회의 피드별 수신 상태 (프론트엔드 상태 칩용)
_its_feed_status: dict[str, dict] = {
    "its": {"ok": None, "count": 0, "ts": 0.0},
    "ex":  {"ok": None, "count": 0, "ts": 0.0},
}


async def _fetch_its_cctvs(minX: float, maxX: float, minY: float, maxY: float) -> list[dict]:
    """ITS API에서 국도(its)·고속도로(ex) CCTV를 모두 조회해 합친다.
    한쪽 피드가 비거나 실패해도 다른 쪽 결과는 그대로 반환된다."""
    async def _fetch(client: httpx.AsyncClient, road_type: str) -> list[dict]:
        api_key = EX_API_KEY if road_type == "ex" else ITS_API_KEY
        params = {
            "apiKey":   api_key,
            "type":     road_type,
            "cctvType": "1",
            "minX": str(minX), "maxX": str(maxX),
            "minY": str(minY), "maxY": str(maxY),
            "getType":  "json",
        }
        resp = await client.get(ITS_BASE_URL, params=params)
        resp.raise_for_status()
        return _parse_its_items(resp.json())

    items: list[dict] = []
    async with httpx.AsyncClient(timeout=8.0) as client:
        results = await asyncio.gather(
            _fetch(client, "its"), _fetch(client, "ex"),
            return_exceptions=True,
        )
    now = time.time()
    for road_type, res in zip(("its", "ex"), results):
        if isinstance(res, BaseException):
            _its_feed_status[road_type] = {"ok": False, "count": 0, "ts": now}
            logger.warning("CCTV 조회 실패 (type=%s): %s: %s",
                           road_type, type(res).__name__, res)
        else:
            _its_feed_status[road_type] = {"ok": True, "count": len(res), "ts": now}
            for _item in res:
                _item["_road_type"] = road_type
            items.extend(res)
    return items


def _build_vehicles(
    tracked: "sv.Detections",
    frame_wh: tuple[int, int] | None = None,
) -> "list[VehicleState]":
    """BoT-SORT 결과를 VehicleState 리스트로 변환.

    frame_wh: (width, height) — 프레임 범위 밖으로 Kalman 예측된 ghost 트랙 제거용.
    """
    fw, fh = frame_wh if frame_wh else (float("inf"), float("inf"))

    # 1단계: ghost track 제거 + 유효 항목 수집
    valid: list[tuple] = []
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
        valid.append((xyxy, class_id, track_id, cx, cy, gx, gy))

    if not valid:
        return []

    # track_id 중복 제거 — 트래커가 같은 ID를 두 개 내보낼 때(ghost track 등) 첫 번째 유지
    seen_tids: set[int] = set()
    deduped: list[tuple] = []
    for item in valid:
        tid = item[2]
        if tid not in seen_tids:
            seen_tids.add(tid)
            deduped.append(item)
    valid = deduped

    # 2단계: 배치 homography — cv2.perspectiveTransform 1회 호출
    pts = [(v[5], v[6]) for v in valid]
    gps_coords   = _transformer.batch_pixel_to_gps(pts)
    meter_coords = _transformer.batch_pixel_to_meter(pts)

    # 3단계: VehicleState 조립
    vehicles: list[VehicleState] = []
    for (xyxy, class_id, track_id, cx, cy, gx, gy), (lat, lon), (x_m, y_m) in zip(
        valid, gps_coords, meter_coords
    ):
        vehicles.append(VehicleState(
            track_id=track_id,
            class_name=VEHICLE_CLASSES.get(class_id, "unknown"),
            bbox_xyxy=xyxy,
            center_px=(cx, cy),
            lat=lat, lon=lon,
            x_m=x_m, y_m=y_m,
        ))
    return vehicles


# ── 백그라운드 멀티-카메라 모니터 ──────────────────────────────────────
from dataclasses import dataclass as _dc, field as _field


@_dc
class _BgCamState:
    cam_key:       str
    name:          str
    name_ko:       str
    url:           str
    lat:           float
    lon:           float
    status:        str   = "loading"   # loading / normal / busy / congested / error
    vehicle_count: int   = 0
    class_counts:  dict  = _field(default_factory=dict)
    updated_at:    float = 0.0
    _task:         object = _field(default=None, repr=False)   # asyncio.Task


class BackgroundMonitor:
    """N개 CCTV를 백그라운드에서 저속(5 s) 탐지하여 WS로 상태 브로드캐스트.

    - 각 카메라는 독립 asyncio.Task로 실행 (독립 VideoStream, 공유 detector)
    - detector.detect()는 내부 threading.Lock으로 직렬화되므로 GPU 충돌 없음
    - B(클러스터링) / C(히스토리 저장) / D(Re-ID) 확장을 위한 훅 포함
    """
    POLL_S            = 8.0   # 캡처 주기 (초) — CPU 부하 절충
    THRESH_BUSY       = 6     # > 6 → busy  (7+)
    THRESH_CONGESTED  = 14    # > 14 → congested  (15+)

    def __init__(self) -> None:
        self._cams: dict[str, _BgCamState] = {}
        self._lock = asyncio.Lock()

    # ── 공개 인터페이스 ────────────────────────────────────────────────

    async def add(self, cam_key: str, name: str, name_ko: str,
                  url: str, lat: float, lon: float, detector) -> None:
        async with self._lock:
            if cam_key in self._cams:
                return
            state = _BgCamState(cam_key=cam_key, name=name, name_ko=name_ko,
                                url=url, lat=lat, lon=lon)
            state._task = asyncio.create_task(
                self._loop(cam_key, state, detector),
                name=f"bg-monitor-{cam_key}",
            )
            self._cams[cam_key] = state
        await self._emit()

    async def remove(self, cam_key: str) -> None:
        async with self._lock:
            state = self._cams.pop(cam_key, None)
            if state and state._task:
                state._task.cancel()
        await self._emit()

    def is_monitored(self, cam_key: str) -> bool:
        return cam_key in self._cams

    def snapshot(self) -> dict:
        return {
            k: {
                "name":          s.name,
                "name_ko":       s.name_ko,
                "lat":           s.lat,
                "lon":           s.lon,
                "status":        s.status,
                "vehicle_count": s.vehicle_count,
                "class_counts":  s.class_counts,
                "updated_at":    s.updated_at,
            }
            for k, s in self._cams.items()
        }

    # ── 확장 훅 (B: 클러스터링, C: 히스토리 저장 시 오버라이드) ──────────
    async def on_frame_result(self, cam_key: str, state: "_BgCamState",
                              vehicle_count: int, class_counts: dict) -> None:
        """탐지 결과가 나올 때마다 호출. 하위 기능에서 오버라이드 가능."""

    # ── 내부 ──────────────────────────────────────────────────────────

    def _classify(self, n: int) -> str:
        if n > self.THRESH_CONGESTED:
            return "congested"
        if n > self.THRESH_BUSY:
            return "busy"
        return "normal"

    async def _emit(self) -> None:
        await _broadcast({"type": "background_status", "cameras": self.snapshot()})

    async def _loop(self, cam_key: str, state: _BgCamState, detector) -> None:
        from detector import VideoStream as _VS
        stream = _VS()
        prev_sig: tuple | None = None
        try:
            await asyncio.to_thread(stream.switch_to, state.url)
            while True:
                try:
                    _, frame = stream.read_frame()
                    if frame is None:
                        ok = await stream.reconnect()
                        if not ok:
                            state.status = "error"
                            await self._emit()
                            prev_sig = None
                        await asyncio.sleep(self.POLL_S)
                        continue

                    dets = await asyncio.to_thread(detector.detect, frame)

                    cls_cnt: dict[str, int] = {}
                    if dets.class_id is not None:
                        for cid in dets.class_id:
                            cn = VEHICLE_CLASSES.get(int(cid), "unknown")
                            cls_cnt[cn] = cls_cnt.get(cn, 0) + 1

                    n = len(dets)
                    state.vehicle_count = n
                    state.class_counts  = cls_cnt
                    state.status        = self._classify(n)
                    state.updated_at    = time.time()

                    await self.on_frame_result(cam_key, state, n, cls_cnt)
                    new_sig = (state.status, state.vehicle_count)
                    if new_sig != prev_sig:
                        await self._emit()
                        prev_sig = new_sig

                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("BG monitor [%s] error: %s", cam_key, exc)
                    state.status = "error"
                    await self._emit()
                    prev_sig = None

                await asyncio.sleep(self.POLL_S)

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning("BG monitor [%s] startup error: %s", cam_key, exc)
            state.status = "error"
        finally:
            stream.release()


_bg_monitor = BackgroundMonitor()


# ── History 샘플러 + 정체 클러스터 ([B]/[C]) ──────────────────────────
def _collect_snapshot_rows(now: float) -> list[SnapshotRow]:
    """현 시점 bg 카메라 전체 + 라이브 카메라 1대를 SnapshotRow 리스트로."""
    rows: list[SnapshotRow] = []

    # 백그라운드 모니터 카메라들 (개수만, 속도 없음)
    for cam_key, s in _bg_monitor.snapshot().items():
        if s.get("status") in ("loading", "error"):
            continue
        rows.append(SnapshotRow(
            ts=now, cam_key=cam_key,
            name=s.get("name", ""), name_ko=s.get("name_ko", ""),
            lat=s.get("lat", 0.0), lon=s.get("lon", 0.0),
            source="bg",
            vehicle_count=int(s.get("vehicle_count", 0)),
            class_counts=json.dumps(s.get("class_counts", {})),
            status=s.get("status", "normal"),
            avg_speed_kph=None,
        ))

    # 라이브 카메라 (FrameAnalytics 캐시 — 평균 속도 포함)
    fa, cam = _latest_live_analytics, _current_cam
    if fa is not None and cam is not None and cam.get("cctvurl"):
        live_key = roi_manager.camera_key(cam["cctvurl"])
        # 같은 카메라가 bg 로도 모니터링 중이면 중복 저장 방지
        if not _bg_monitor.is_monitored(live_key):
            cnt = int(fa.get("vehicle_count", 0))
            rows.append(SnapshotRow(
                ts=now, cam_key=live_key,
                name=cam.get("name", ""), name_ko=cam.get("name", ""),
                lat=cam.get("lat", 0.0), lon=cam.get("lon", 0.0),
                source="live",
                vehicle_count=cnt,
                class_counts=json.dumps(fa.get("class_counts", {})),
                status=_bg_monitor._classify(cnt),
                avg_speed_kph=fa.get("avg_speed_kph"),
            ))
    return rows


async def _broadcast_clusters_if_changed() -> None:
    """bg 스냅샷으로 정체 클러스터 1회 계산 → 변경 시에만 브로드캐스트."""
    global _last_cluster_sig
    clusters = compute_clusters(_bg_monitor.snapshot(), eps_m=CONGESTION_EPS_M)
    sig = json.dumps(
        [(c["id"], c["severity"], c["camera_count"]) for c in clusters],
        sort_keys=True,
    )
    if sig == _last_cluster_sig:
        return
    _last_cluster_sig = sig
    await _broadcast({"type": "congestion_clusters", "clusters": clusters})


async def history_sampler_loop() -> None:
    """단일 주기 샘플러 — bg+live 스냅샷 누적 저장 + 정체 클러스터 갱신.

    detect 루프와 분리된 독립 주기(HISTORY_SAMPLE_S). 매 틱:
      1) 모든 모니터 카메라 + 라이브 1대를 batched INSERT (1 트랜잭션)
      2) 같은 스냅샷으로 클러스터 1회 계산 → 변경 시 broadcast
      3) 보존 기간 경과 행 prune (틱마다 가벼운 DELETE)
    """
    prune_every = max(1, int(3600 / HISTORY_SAMPLE_S))  # ≈1시간마다
    tick = 0
    logger.info("History 샘플러 시작 (주기 %ds, 보존 %d일)",
                HISTORY_SAMPLE_S, HISTORY_RETENTION_DAYS)
    while True:
        try:
            now = time.time()
            rows = _collect_snapshot_rows(now)
            if rows:
                await asyncio.to_thread(_history.record_many, rows)
            await _broadcast_clusters_if_changed()

            # 측정 자동 flush — 프레임이 처리됐을 때만 eval_*.csv + eval_summary.json 갱신
            if getattr(_metrics, "_frames", 0) > 0:
                await asyncio.to_thread(_metrics.report)

            tick += 1
            if tick % prune_every == 0:
                cutoff = retention_cutoff(HISTORY_RETENTION_DAYS, now)
                deleted = await asyncio.to_thread(_history.prune, cutoff)
                if deleted:
                    logger.info("History prune: %d개 행 삭제", deleted)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("History 샘플러 오류: %s", exc)
        await asyncio.sleep(HISTORY_SAMPLE_S)


# ── 앱 생명주기 ───────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    from detector import VideoStream
    stream = VideoStream()
    app.state.stream   = stream
    app.state.detector = None  # lazy: loaded in background so startup is instant

    async def _init_detector() -> None:
        from detector import VehicleDetector
        logger.info("YOLO 모델 백그라운드 로드 시작 (YOLO26 기본 + BoxMOT)…")
        det = await asyncio.to_thread(VehicleDetector)
        app.state.detector = det
        _metrics.set_context(
            backend=det.tracker_info.get("backend", ""),
            tracker=det.tracker_info.get("tracker", ""),
        )
        logger.info("YOLO 모델 로드 완료")

    init_task    = asyncio.create_task(_init_detector())
    task         = asyncio.create_task(live_loop(stream))
    refresh_task = asyncio.create_task(hls_refresh_loop(stream))
    its_task     = asyncio.create_task(_its_speed_poll_loop())
    hist_task    = asyncio.create_task(history_sampler_loop())

    yield
    init_task.cancel()
    task.cancel()
    refresh_task.cancel()
    its_task.cancel()
    hist_task.cancel()
    _history.close()
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
def _en_road_parts(name: str) -> list[str]:
    """도로명 영어 약칭 (국도/지방도/고속) — 두 변환 함수의 공통 코어."""
    parts: list[str] = []
    m = re.search(r'국도\s*(\d+)\s*호선?', name)
    if m:
        parts.append(f"National Route {m.group(1)}")
    m = re.search(r'지방도\s*(\d+)\s*호?', name)
    if m:
        parts.append(f"Provincial Route {m.group(1)}")
    if re.search(r'고속(도로|국도)', name):
        parts.append("Expressway")
    return parts


def _en_dir_parts(name: str) -> list[str]:
    """방향 영어 약칭 (상행/하행/양방향) — 두 변환 함수의 공통 코어."""
    if '상행' in name:
        return ["NB↑"]
    if '하행' in name:
        return ["SB↓"]
    if '양방향' in name:
        return ["Both↕"]
    return []


def _en_only_name(name: str) -> str:
    """ITS CCTV 한국어 이름에서 영어만으로 구성된 약칭을 반환한다.
    인식 가능한 패턴이 없으면 빈 문자열 반환 (프론트엔드 fallback 처리)."""
    en_parts: list[str] = _en_road_parts(name)

    # ASCII 위치 힌트 추출 (IC, JC, TG, SA — 한국 도로명에 포함된 영어 약어)
    seen: set[str] = set()
    for hint in re.findall(r'\b(IC|JC|TG|SA)\b', name, re.IGNORECASE):
        hu = hint.upper()
        if hu not in seen:
            seen.add(hu)
            en_parts.append(hu)

    # 구간 번호 (예: 1-1구간, 2구간)
    m = re.search(r'(\d+[-–]\d+)\s*구간', name)
    if m:
        en_parts.append(f"Sec {m.group(1)}")

    en_parts.extend(_en_dir_parts(name))
    return " ".join(en_parts)  # 패턴 없으면 빈 문자열


def _korname_to_en(name: str) -> str:
    """ITS CCTV 한국어 이름에 영어 약칭을 괄호로 병기한다."""
    en_parts = _en_road_parts(name) + _en_dir_parts(name)
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

    try:
        items = await _fetch_its_cctvs(minX, maxX, minY, maxY)
        result = []
        for item in items:
            try:
                lat = float(item.get("coordy") or 0)
                lon = float(item.get("coordx") or 0)
                if not (lat and lon):
                    continue
                url       = item.get("cctvurl", "")
                road_type = item.get("_road_type", "its")
                name_ko   = item.get("cctvname", "")
                if not name_ko:
                    name_ko = "고속도로 CCTV" if road_type == "ex" else "국도 CCTV"
                name    = _korname_to_en(name_ko)
                _en_fallback = "Expressway CCTV" if road_type == "ex" else "National Rd CCTV"
                name_en = _en_only_name(name_ko) or _en_fallback
                cam_key = roi_manager.camera_key(url) if url else None
                result.append({
                    "id":        url or name,
                    "name":      name,
                    "name_ko":   name_ko,
                    "name_en":   name_en,
                    "cam_key":   cam_key,
                    "lat":       lat,
                    "lon":       lon,
                    "cctvurl":   url,
                    "road_type": road_type,
                    "heading":   0,
                    "fov_deg":   70,
                })
            except (ValueError, TypeError):
                continue
        # cam_key 중복 제거 — 동일 스트림 URL이 두 번 반환된 경우 하나만 유지
        _seen_keys: set[str] = set()
        _deduped: list[dict] = []
        for _item in result:
            _ck = _item["cam_key"]
            if _ck is None or _ck not in _seen_keys:
                if _ck is not None:
                    _seen_keys.add(_ck)
                _deduped.append(_item)
        result = _deduped

        # 동일 name_en / name_ko 중복 구분: 위도 순 정렬 후 (1)(2)... 부여
        for _field in ("name_en", "name_ko", "name"):
            _name_buckets: dict[str, list] = {}
            for item in result:
                key = item[_field]
                if key:
                    _name_buckets.setdefault(key, []).append(item)
            for _key, _items in _name_buckets.items():
                if len(_items) > 1:
                    _items.sort(key=lambda x: x.get("lat", 0))
                    for _idx, _item in enumerate(_items, 1):
                        _item[_field] = f"{_key} ({_idx})"

        logger.info("CCTV 조회 완료: %d개 (캐시 저장)", len(result))
        _cctv_cache[cache_key] = result
        return result
    except Exception as e:
        logger.warning("CCTV 목록 조회 실패: %s: %s", type(e).__name__, e)
        return []


# ── CCTV 피드 상태 ────────────────────────────────────────────────────
@app.get("/cctv-feed-status")
async def cctv_feed_status():
    """국도(its)·고속도로(ex) CCTV 피드의 마지막 조회 결과."""
    return _its_feed_status


# ── 백그라운드 모니터링 엔드포인트 ────────────────────────────────────
class _BgAddBody(BaseModel):
    cam_key: str
    name:    str   = ""
    name_ko: str   = ""
    url:     str
    lat:     float = 0.0
    lon:     float = 0.0


@app.post("/background/add")
async def background_add(body: _BgAddBody, request: Request):
    """카메라를 백그라운드 모니터링 목록에 추가."""
    await _bg_monitor.add(
        cam_key = body.cam_key,
        name    = body.name or body.cam_key,
        name_ko = body.name_ko or body.name or body.cam_key,
        url     = body.url,
        lat     = body.lat,
        lon     = body.lon,
        detector= request.app.state.detector,
    )
    return {"ok": True}


@app.post("/background/remove/{cam_key}")
async def background_remove(cam_key: str):
    """카메라를 백그라운드 모니터링 목록에서 제거."""
    await _bg_monitor.remove(cam_key)
    return {"ok": True}


@app.get("/background/status")
async def background_status_endpoint():
    """현재 백그라운드 모니터링 중인 카메라들의 상태 반환."""
    return _bg_monitor.snapshot()


# ── 히스토리 분석 엔드포인트 ([C]) ────────────────────────────────────
@app.get("/history/cameras")
async def history_cameras():
    """기록이 있는 카메라 목록 (드롭다운용)."""
    return await asyncio.to_thread(_history.cameras)


@app.get("/history/series")
async def history_series(
    cam_key: str = Query(...),
    hours: float = Query(24.0),
    bucket_s: int = Query(300),
):
    """시간 버킷별 평균 차량수 / 평균 속도 시계열."""
    since = time.time() - hours * 3600.0
    return await asyncio.to_thread(_history.series, cam_key, since, bucket_s)


@app.get("/history/peak")
async def history_peak(cam_key: str = Query(...), hours: float = Query(24.0)):
    """기간 내 피크타임(최대 차량수 시점)."""
    since = time.time() - hours * 3600.0
    return await asyncio.to_thread(_history.peak, cam_key, since) or {}


@app.get("/history/export.csv")
async def history_export_csv(cam_key: str = Query(...), hours: float = Query(24.0)):
    """기간 내 raw 스냅샷을 CSV 로 내보내기."""
    since = time.time() - hours * 3600.0
    rows = await asyncio.to_thread(_history.export_rows, cam_key, since)
    lines = ["timestamp_iso,ts,source,vehicle_count,status,avg_speed_kph"]
    for r in rows:
        iso = datetime.fromtimestamp(r["ts"], tz=timezone.utc).isoformat()
        spd = "" if r["avg_speed_kph"] is None else r["avg_speed_kph"]
        lines.append(
            f'{iso},{r["ts"]:.1f},{r["source"]},{r["vehicle_count"]},'
            f'{r["status"]},{spd}'
        )
    csv = "\n".join(lines)
    fname = f"traffic_{cam_key[:8]}_{int(time.time())}.csv"
    return Response(
        content=csv,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# ── 카메라 전환 ───────────────────────────────────────────────────────
class CameraSwitch(BaseModel):
    cctvurl: str
    lat: float
    lon: float
    name: str = ""


@app.post("/switch-camera")
async def switch_camera(body: CameraSwitch):
    """클릭한 CCTV 정보 저장 + transformer 재보정 + BoT-SORT 리셋 + live_loop 스트림 전환."""
    global _current_cam, _cam_version, _scale_switch_frame
    _cam_version += 1
    _scale_switch_frame = _frame_count   # 적응형 min_obs 기준 리셋
    analytics.reset()
    cam_key = roi_manager.camera_key(body.cctvurl)
    _scale, _found = _load_speed_scale(cam_key)
    analytics.speed_scale = _scale
    analytics.its_scale_restored = _found
    logger.info("속도 보정 계수 복원: %.4f found=%s (camera=%s)", _scale, _found, cam_key)

    # Vehicle apparent-size scale model 복원
    _transformer.reset_scale_obs(clear_model=True)
    vc = _load_vehicle_calib(cam_key)
    if vc:
        _transformer.load_scale_params(vc)
        logger.info("vehicle_calib 복원: B=%.5f C=%.3f (camera=%s)", vc["B"], vc["C"], cam_key)

    # Road-model 포즈 prior 복원 → solve_pose 초기값 seed (Phase 12)
    _transformer.reset_pose(clear_prior=True)
    cp = _load_camera_pose(cam_key)
    if cp:
        _transformer.load_pose_params(cp)
        logger.info("camera_pose prior 복원: H=%.1f pitch=%.1f (camera=%s)",
                    cp["H_m"], cp["pitch_deg"], cam_key)

    # BoT-SORT 내부 상태 리셋 (to_thread: _state_lock이 이벤트 루프를 블로킹하지 않도록)
    det = getattr(app.state, "detector", None)
    if det is not None:
        await asyncio.to_thread(det.reset_tracker)

    # 새 카메라 위치 기준 ITS 구간속도 즉시 갱신 (비동기, 응답 안 기다림)
    global _its_speed_kph
    _its_speed_kph = None
    asyncio.create_task(_update_its_speed())

    # 노드링크 DB에서 가장 가까운 도로 정보 조회 — 두 쿼리 병렬 실행
    _hint = _parse_road_name_hint(body.name)
    road, road_snap = await asyncio.gather(
        asyncio.to_thread(get_road_info, body.lat, body.lon, _hint),
        asyncio.to_thread(get_road_snap, body.lat, body.lon, _hint),
    )
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

    # 이전 카메라 프레임이 새 연결에 흘러들어가지 않도록 버퍼 초기화
    global _latest_frame_jpeg, _latest_annotated_jpeg
    _latest_frame_jpeg = None
    _latest_annotated_jpeg = None

    # live_loop 스트림 전환 큐잉 — road_info도 함께 전달해 live_loop 재쿼리 방지
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
            # road_info 캐시 — live_loop가 중복 DB 쿼리 하지 않게
            "_road_name":     road["road_name"]  if road else None,
            "_road_lanes":    road["lanes"]       if road else None,
            "_road_max_spd":  road["max_spd"]     if road else None,
            "_road_rank":     road["road_rank"]   if road else None,
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
    try:
        items = await _fetch_its_cctvs(lon - 0.002, lon + 0.002,
                                       lat - 0.002, lat + 0.002)
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
                await asyncio.to_thread(detector.reset_tracker)
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
            analytics.frame_h = fh  # ① 깊이별 보정용 현재 프레임 높이
            # 브라우저 캔버스 프레임은 실시간 도착 → 벽시계를 속도 시간축으로 사용
            # (합성 fid/FPS 는 처리 지연 시 dt 과소계산 → 속도 0 버그 원인)
            result = analytics.update(fid, time.monotonic() * 1000, vehicles, in_cnt, out_cnt, in_ids, out_ids)
            _metrics.add_frame(result.vehicles)  # 브라우저 경로: 추적/속도/탐지 통계 누적
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
                await asyncio.to_thread(det.reset_tracker)
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

    with _frame_count_lock:
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
    # 현재 활성 카메라와 같으면 detector에 즉시 적용 + ROI ring 재broadcast
    det = getattr(app.state, "detector", None)
    if det is not None and _current_cam and _current_cam.get("cctvurl") == body.cctvurl:
        det.set_roi(body.polygon)
        # Phase 3: ROI 변경 → GPS ring 재계산 후 broadcast
        roi_ring = _compute_roi_gps_ring()
        if roi_ring:
            asyncio.create_task(_broadcast({
                "type": "roi_updated",
                "roi_gps_ring": roi_ring,
            }))
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

    # JSON 파일 저장 (원자적 read-modify-write)
    _atomic_update_json(CALIBRATION_PATH, cam_key, {
        "pixel_pts":  body.pixel_pts,
        "gps_pts":    body.gps_pts,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })

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


@app.post("/recalibrate")
async def recalibrate():
    """현재 카메라 warm-up 재시작 (수동 재보정 버튼).

    저장된 camera_pose를 삭제하고 warm-up을 다시 시작합니다.
    """
    if not _current_cam:
        return {"ok": False, "error": "활성 카메라 없음"}
    cam_key = roi_manager.camera_key(_current_cam.get("cctvurl", ""))
    # 저장 pose 삭제
    _atomic_delete_json(CAMERA_POSE_PATH, cam_key)
    _transformer._pose_prior = None
    _transformer.recalibrate()
    _transformer.start_warmup(
        bearing_deg=analytics.road_bearing_deg or 0.0,
        road_width_m=_auto_calib_road_width_m,
        fix_direction=_current_cam.get("has_name_bearing", False),
        cam_lat=_current_cam.get("lat"),
        cam_lon=_current_cam.get("lon"),
        road_rank=_auto_calib_road_rank,
    )
    logger.info("수동 재보정 요청 — warm-up 재시작 (camera=%s)", cam_key)
    await _broadcast({"type": "calibrating", "elapsed_s": 0, "status": "recalibrating"})
    return {"ok": True, "camera_key": cam_key}


@app.delete("/calibration/{camera_key}")
async def delete_calibration(camera_key: str):
    """캘리브레이션 삭제 (기본 근사값으로 롤백)."""
    _atomic_delete_json(CALIBRATION_PATH, camera_key)
    # 현재 카메라면 GPS center 근사값으로 롤백
    if _current_cam and roi_manager.camera_key(_current_cam.get("cctvurl", "")) == camera_key:
        _transformer.update_gps_center(_current_cam["lat"], _current_cam["lon"], bearing_deg=analytics.road_bearing_deg or 0.0)
    return {"ok": True}


@app.delete("/roi/{camera_key}")
async def delete_roi(camera_key: str):
    """카메라의 ROI 삭제."""
    _atomic_delete_json(roi_manager._CONFIG_PATH, camera_key)
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
            # annotated 프레임이 없으면(미시청 상태 등) raw 프레임으로 폴백
            jpeg = _latest_annotated_jpeg or _latest_frame_jpeg
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
        await asyncio.to_thread(det.reset_tracker)
    _current_cam = None
    _latest_frame_jpeg = None
    _latest_annotated_jpeg = None
    await _set_viewer_active(False)
    logger.info("카메라 스트림 해제 (프론트엔드 요청)")
    return {"ok": True}


# ── 라이브 시청 상태 ──────────────────────────────────────────────────
class _ViewerState(BaseModel):
    active: bool


async def _set_viewer_active(active: bool) -> None:
    global _live_viewer_active
    if _live_viewer_active == active:
        return
    _live_viewer_active = active
    if not active:
        # 미시청 전환 → 재개 시 깨끗한 트래커 보장 (to_thread: _state_lock 블로킹 방지)
        det = getattr(app.state, "detector", None)
        if det is not None:
            await asyncio.to_thread(det.reset_tracker)
    logger.info("라이브 시청 상태: %s", "active" if active else "inactive")


@app.post("/viewer-active")
async def viewer_active(body: _ViewerState):
    """프론트가 (카메라 선택 × 페이지 visible) 상태를 보고 → 미시청 시 라이브 YOLO 중단."""
    await _set_viewer_active(body.active)
    return {"ok": True}


# ── 속도 진단 토글 (브라우저에서 켜고 끄기) ──────────────────────────
@app.get("/speed-debug/{state}")
async def speed_debug_toggle(state: str):
    """state=on|off|status. 브라우저에서 http://localhost:8000/speed-debug/on 으로 즉시 토글.

    on  → backend/speed_debug.log 에 per-frame 속도 진단 기록 시작
    off → 기록 중지
    status → 현재 상태/파일 크기 확인
    """
    if state in ("on", "off"):
        path = set_speed_debug(state == "on")
        logger.info("속도 진단 로깅 %s (%s)", state, path)
    elif state != "status":
        return {"error": "state must be on|off|status"}
    return speed_debug_status()


# ── 파이프라인 타이밍 로그 ────────────────────────────────────────────
@app.post("/debug/perf/reset")
async def perf_reset():
    """perf_log.jsonl 초기화 (새 측정 구간 시작)."""
    _perf_stats.reset()
    return {"ok": True, "msg": "perf_log.jsonl cleared"}


@app.get("/debug/perf/latest")
async def perf_latest():
    """perf_log.jsonl 마지막 10줄 반환 (빠른 현황 확인)."""
    from perf import PERF_LOG_PATH
    if not PERF_LOG_PATH.exists():
        return {"lines": []}
    lines = PERF_LOG_PATH.read_text(encoding="utf-8").splitlines()
    return {"lines": [json.loads(l) for l in lines[-10:] if l.strip()]}


# ── yappi 함수별 프로파일 ──────────────────────────────────────────────
@app.post("/debug/profile/start")
async def profile_start():
    """yappi 프로파일링 시작 (전체 스레드). 먼저 pip install yappi 필요."""
    return yappi_start()


@app.post("/debug/profile/stop")
async def profile_stop():
    """yappi 프로파일링 중지 → profile_stats.txt 저장 + 상위 30개 함수 반환."""
    return await asyncio.to_thread(yappi_stop)


# ── 측정 리포트 (실행 중 자동 수집) ──────────────────────────────────
@app.get("/eval/report")
async def eval_report():
    """현재까지 누적된 측정값을 집계해 backend/eval_*.csv + eval_summary.json 으로
    저장하고 JSON(markdown 표 포함)으로 반환. 카메라를 보는 동안 자동 누적됨."""
    return await asyncio.to_thread(_metrics.report)


@app.post("/eval/reset")
async def eval_reset():
    """측정 누적값 초기화 (새 실험 시작)."""
    _metrics.reset()
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
    # FrameAnalytics(라이브 카메라) 최신값을 캐시 — I/O 없는 메모리 캡처만.
    # 실제 DB 저장은 history_sampler_loop 가 주기적으로 담당한다.
    if "frame_id" in payload:
        global _latest_live_analytics
        _latest_live_analytics = payload
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
async def _refresh_hls_url_from_its(stream: "VideoStream", *, force: bool) -> None:
    """ITS API에서 현재 카메라의 신선한 HLS URL을 조회해 live_loop 큐에 투입.

    force=True : 토큰 만료 긴급 복구 — URL 동일 여부 무관하게 갱신.
    force=False: 주기 갱신 — 기존 stream.url과 다를 때만 갱신.
    """
    cam = _current_cam
    if cam is None or not cam.get("name"):
        return
    lat, lon, name = cam["lat"], cam["lon"], cam["name"]
    label = "긴급 갱신 (토큰 만료)" if force else "갱신"
    try:
        items = await _fetch_its_cctvs(lon - 0.002, lon + 0.002,
                                       lat - 0.002, lat + 0.002)
        for item in items:
            if item.get("cctvname") == name:
                new_url = item.get("cctvurl", "")
                if new_url and (force or new_url != stream.url):
                    logger.info("HLS URL %s: %s", label, name)
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
        logger.warning("HLS URL %s 실패: %s", label, e)


async def _refresh_stream_url(stream: "VideoStream") -> None:
    """ITS API에서 현재 카메라 URL을 즉시 갱신. 토큰 만료 복구용."""
    await _refresh_hls_url_from_its(stream, force=True)


async def live_loop(stream) -> None:
    from tracker import VehicleTracker

    tracker        = VehicleTracker()
    target_interval = 1.0 / FPS
    skip_budget    = 0
    _reconnect_fails = 0  # 연속 reconnect 실패 횟수
    _switch_retry_cam: dict | None = None   # 전환 실패 시 재시도할 cam 정보
    _switch_retry_count: int = 0
    _switch_retry_at: float = 0.0           # 재시도 허용 시각 (monotonic)
    _SWITCH_MAX_RETRY = 2
    _SWITCH_RETRY_DELAY = 2.0               # 재시도 간격 (초). FFmpeg timeout 10s 기준 총 ~24s
    global _stream_fps, _latest_frame_jpeg

    logger.info("Live 루프 대기 중 — 지도에서 CCTV를 클릭하여 스트림을 시작하세요")

    while True:
        # 카메라 전환 요청 처리 — 큐에 쌓인 항목 중 최신 것만 사용
        now_mono = time.monotonic()
        if not _camera_queue.empty():
            cam = _camera_queue.get_nowait()
            while not _camera_queue.empty():
                cam = _camera_queue.get_nowait()
            # 새 요청이 오면 재시도 카운터 리셋
            _switch_retry_cam = cam
            _switch_retry_count = 0
            _switch_retry_at = now_mono
        elif (_switch_retry_cam is not None
              and _switch_retry_count < _SWITCH_MAX_RETRY
              and now_mono >= _switch_retry_at):
            cam = _switch_retry_cam
            logger.info("카메라 전환 재시도 (%d/%d): %s",
                        _switch_retry_count + 1, _SWITCH_MAX_RETRY, cam.get("url", ""))
        else:
            cam = None

        if cam is not None:
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
                # Phase 5: road_pts를 analytics에도 전달 (로컬 도로 방위 계산용)
                analytics.road_pts = cam.get("road_pts")
                tracker = VehicleTracker()
                analytics.reset()
                _metrics.set_context(source=cam.get("name", ""))
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
                _scale, _found = _load_speed_scale(cam_key)
                analytics.speed_scale = _scale
                analytics.its_scale_restored = _found
                logger.info("속도 보정 계수 복원: %.4f found=%s (camera=%s)", _scale, _found, cam_key)

                # Vehicle apparent-size scale model 복원 (live_loop 경로)
                _transformer.reset_scale_obs(clear_model=True)
                _vc = _load_vehicle_calib(cam_key)
                if _vc:
                    _transformer.load_scale_params(_vc)
                    logger.info("vehicle_calib 복원 (live_loop): B=%.5f (camera=%s)", _vc["B"], cam_key)

                # Road-model 포즈 prior 복원 → solve_pose 초기값 seed (Phase 12)
                _transformer.reset_pose(clear_prior=True)
                _cp = _load_camera_pose(cam_key)
                if _cp:
                    _transformer.load_pose_params(_cp)
                    logger.info("camera_pose prior 복원 (live_loop): H=%.1f (camera=%s)",
                                _cp["H_m"], cam_key)
                global _scale_switch_frame, _last_calib_info, _last_broadcast_bearing
                global _accepted_fov, _fov_ema_samples
                _scale_switch_frame = _frame_count   # 적응형 min_obs 기준 리셋
                _last_calib_info = {}
                _last_broadcast_bearing = analytics.road_bearing_deg or 0.0
                _accepted_fov = {}           # Phase 4: 카메라 전환 시 FOV 초기화
                _fov_ema_samples = 0

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

                # 스트림 준비 완료 신호 — switch_camera에서 캐싱된 road_info 재사용 (중복 쿼리 없음)
                _road_name    = cam.get("_road_name")
                _road_lanes   = cam.get("_road_lanes")
                _road_max_spd = cam.get("_road_max_spd")
                _road_rank    = str(cam.get("_road_rank") or "")

                # 도로폭 계산 (카메라 공통 — manual/auto-calib 무관하게 broadcast에 사용)
                _cam_road_width_m: float | None = cam.get("road_width_m")
                if _cam_road_width_m is None:
                    _ri_lanes = _road_lanes or 2
                    _ri_lane_w = 3.5 if _road_rank in ("101", "102", "103") else (3.25 if _road_rank in ("104", "105") else 3.0)
                    _cam_road_width_m = max(1, _ri_lanes) * 2 * _ri_lane_w

                # 수동 캘리브레이션 없으면 warm-up 관측 수집 시작
                global _auto_calib_road_width_m, _auto_calib_road_rank
                if not _manual_cal_loaded:
                    _auto_calib_road_width_m = _cam_road_width_m
                    _auto_calib_road_rank    = _road_rank
                    oneway = cam.get("is_oneway", False)
                    logger.info("도로폭: %.1fm (%s)", _cam_road_width_m, "편도" if oneway else "양방향")
                    # 저장 prior pose가 있으면 적용 후 즉시 lock (warm-up 스킵)
                    _prior = _load_camera_pose(cam_key)
                    if _prior:
                        _transformer._pose_prior = camera_pose.Pose(**{
                            k: _prior[k] for k in ("H_m", "pitch_deg", "yaw_deg", "focal_px")
                        })
                        _h0, _w0 = 0, 0  # apply_prior_pose는 frame shape이 필요 → 루프에서 처리
                        _transformer._warmup_params = {
                            "bearing_deg": analytics.road_bearing_deg or 0.0,
                            "road_width_m": _cam_road_width_m,
                            "fix_direction": cam.get("has_name_bearing", False),
                            "cam_lat": cam.get("lat"),
                            "cam_lon": cam.get("lon"),
                            "road_rank": _road_rank,
                        }
                        _transformer._warmup_active = False
                        _transformer._locked = False  # frame 첫 수신 시 apply_prior_pose 후 lock
                        logger.info("저장 pose 있음 — 첫 프레임에서 prior 적용 후 lock 예정")
                    else:
                        _transformer.start_warmup(
                            bearing_deg=analytics.road_bearing_deg or 0.0,
                            road_width_m=_cam_road_width_m,
                            fix_direction=cam.get("has_name_bearing", False),
                            cam_lat=cam.get("lat"),
                            cam_lon=cam.get("lon"),
                            road_rank=_road_rank,
                        )
                        logger.info("warm-up 시작 (최대 %.0fs)", WARMUP_MAX_S)
                road_bearing_for_ui = cam.get("road_bearing")
                name_bearing_for_ui = (
                    cam.get("name_bearing")
                    if cam.get("name_bearing") is not None
                    else _parse_name_bearing(cam.get("name", ""), road_bearing_for_ui)
                )
                # Phase 3: ROI를 GPS ring으로 변환해 polygon으로 표시
                _roi_ring = _compute_roi_gps_ring()
                await _broadcast({
                    "type": "camera_ready",
                    "name": cam.get("name", ""),
                    "roi": saved_roi,
                    "camera_key": cam_key,
                    "calibrated": _transformer.is_calibrated,
                    "road_name":     _road_name,
                    "road_lanes":    _road_lanes,
                    "road_max_spd":  _road_max_spd,
                    "road_bearing": road_bearing_for_ui,
                    "name_bearing": name_bearing_for_ui,
                    "snap_lat":     snap_lat,
                    "snap_lon":     snap_lon,
                    "road_width_m": _cam_road_width_m,
                    "road_pts":     cam.get("road_pts"),
                    "snap_along_m": cam.get("snap_along_m"),
                    **({"roi_gps_ring": _roi_ring} if _roi_ring else {}),
                })
                logger.info("카메라 전환 완료, camera_ready 신호 전송")
                _switch_retry_cam = None   # 성공 → 재시도 대기 해제
                _switch_retry_count = 0
            except RuntimeError as e:
                _switch_retry_count += 1
                if _switch_retry_count < _SWITCH_MAX_RETRY:
                    _switch_retry_at = time.monotonic() + _SWITCH_RETRY_DELAY
                    logger.warning(
                        "카메라 전환 실패 (%d/%d), %.0fs 후 재시도: %s",
                        _switch_retry_count, _SWITCH_MAX_RETRY, _SWITCH_RETRY_DELAY, e,
                    )
                    await _broadcast({"type": "camera_error", "message": str(e), "retrying": True})
                else:
                    logger.warning("카메라 전환 최대 재시도 초과, 포기: %s", e)
                    _switch_retry_cam = None
                    await _broadcast({"type": "camera_error", "message": str(e), "retrying": False})

        if not stream.is_open:
            await asyncio.sleep(0.5)
            continue

        frame_id, frame = stream.read_frame()
        if frame is None:
            # 복구 중엔 이전 카메라 프레임이 굳어 보이지 않도록 placeholder로 교체
            _latest_frame_jpeg = _placeholder_jpeg
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

        # MJPEG 스트림 버퍼는 뷰어 활성 여부와 무관하게 항상 업데이트.
        # 미시청 상태에서도 /video-stream 접속 시 즉시 영상이 나오도록 함.
        _mjpeg_ok, _mjpeg_buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
        if _mjpeg_ok:
            _latest_frame_jpeg = _mjpeg_buf.tobytes()

        # 라이브 미시청 시: YOLO·자동캘리브·broadcast 스킵해 GPU/CPU 낭비 차단.
        # _clients 가 비면(브라우저 종료) visibilitychange 없이도 자동 정지.
        if not (_current_cam and _live_viewer_active and _clients):
            await asyncio.sleep(0.5)
            continue

        # ── Warm-up → commit → lock 캘리브레이션 ────────────────────────
        # 수동 4-point cali(_is_calibrated) 또는 이미 locked이면 완전 스킵.
        if frame is not None and not _transformer.is_calibrated:
            cam_key_now = roi_manager.camera_key(_current_cam.get("cctvurl", "")) if _current_cam else ""

            # ① prior pose 대기 경로: 첫 프레임에서 prior 적용 후 lock
            if (not _transformer.locked and not _transformer._warmup_active
                    and _transformer._pose_prior is not None):
                _h0, _w0 = frame.shape[:2]
                if _transformer.apply_prior_pose(
                        _auto_calib_road_width_m,
                        analytics.road_bearing_deg or 0.0,
                        (_w0, _h0)):
                    _transformer._locked = True
                    logger.info("저장 prior 포즈 적용 완료 — locked")
                    await _broadcast({"type": "calibrating", "elapsed_s": 0,
                                      "status": "locked_prior"})
                else:
                    # prior 적용 실패 → warm-up으로 전환
                    _transformer.start_warmup(
                        bearing_deg=analytics.road_bearing_deg or 0.0,
                        road_width_m=_auto_calib_road_width_m,
                        fix_direction=_current_cam.get("has_name_bearing", False),
                        cam_lat=_current_cam.get("lat"),
                        cam_lon=_current_cam.get("lon"),
                        road_rank=_auto_calib_road_rank,
                    )
                    logger.info("prior 포즈 적용 실패 — warm-up 시작")

            # ② warm-up 관측 누적
            elif _transformer._warmup_active:
                should_commit, elapsed = await asyncio.to_thread(
                    _transformer.feed_warmup_frame, frame)

                # 진행 상황 주기적 broadcast (30프레임 = ~1s)
                if _frame_count % 30 == 0:
                    await _broadcast({"type": "calibrating",
                                      "elapsed_s": round(elapsed, 1),
                                      "stack_frames": len(_transformer._warmup_stack)})

                if should_commit:
                    logger.info("warm-up commit 시작 (elapsed=%.1fs, stack=%d)",
                                elapsed, len(_transformer._warmup_stack))
                    ok, used_bearing, calib_info = await asyncio.to_thread(
                        _transformer.commit_calibration, frame.shape[:2])

                    # bearing 갱신
                    bearing = analytics.road_bearing_deg or 0.0
                    if ok:
                        if analytics.road_bearing_deg is None:
                            analytics.road_bearing_deg = used_bearing
                        elif abs((used_bearing - bearing + 180) % 360 - 180) > 90:
                            analytics.road_bearing_deg = used_bearing

                    # 성공 시 pose 저장; fallback(heuristic)은 저장 안 함
                    if ok and calib_info and calib_info.get("method") == "pose":
                        _pp = _transformer.get_pose_params()
                        if _pp:
                            _save_camera_pose(cam_key_now, _pp)

                    # vehicle-scale 최종 1회 fit 후 저장
                    _min_obs = SCALE_MIN_OBS_SPARSE if len(_transformer._scale_obs) < SCALE_MIN_OBS else SCALE_MIN_OBS
                    if _transformer.fit_scale_model(_min_obs):
                        _vc_p = _transformer.get_scale_params()
                        if _vc_p:
                            _save_vehicle_calib(cam_key_now, _vc_p)
                            logger.info("vehicle-scale 최종 fit 저장 (locked)")

                    raw_info = dict(calib_info) if calib_info else {}
                    stabilized_info = _apply_fov_ema(raw_info)
                    _last_calib_info = stabilized_info
                    _last_broadcast_bearing = used_bearing
                    roi_ring = _compute_roi_gps_ring()
                    await _broadcast({
                        "type": "auto_calibrated",
                        "heading": used_bearing,
                        "warmup_elapsed_s": round(elapsed, 1),
                        **({"roi_gps_ring": roi_ring} if roi_ring else {}),
                        **_last_calib_info,
                    })
                    logger.info("warm-up commit 완료 — locked (ok=%s, method=%s)",
                                ok, calib_info.get("method") if calib_info else "fallback")

        # _latest_frame_jpeg 는 viewer-active 체크 전에 이미 업데이트됨

        if skip_budget > 0:
            skip_budget -= 1
            continue

        # ws/detect 활성 시 boxmot 트래커 공유 충돌 방지 — 프레임만 드레인
        if _detect_clients > 0:
            await asyncio.sleep(target_interval)
            continue

        detector = getattr(app.state, "detector", None)
        if detector is None:
            await asyncio.sleep(0.5)
            continue

        t0 = time.perf_counter()
        try:
            payload = await asyncio.to_thread(
                _live_process, frame_id, frame, detector, tracker, stream.pos_msec
            )
            if payload:
                await _broadcast(payload)
        except Exception as exc:
            logger.warning("live_process 오류 (루프 유지): %s", exc)

        # Task 3: bearing auto-refinement from observed vehicle flow
        # Phase 2: refine_road_pts 호출 결과를 road_pts에 반영하지 않음
        #   (bearing-bin 직선화가 OSM shape_pts 곡선을 덮어쓰는 문제 차단)
        if _frame_count % BEARING_REFINE_INTERVAL_FRAMES == 0:
            refined = analytics.refine_bearing()
            # Phase 2: new_road = None 고정 — OSM shape_pts 곡선 유지
            new_road = None

            need_broadcast = False
            if refined is not None:
                diff = abs(((refined - _last_broadcast_bearing + 180) % 360) - 180)
                if diff >= BEARING_BROADCAST_MIN_DEG:
                    _last_broadcast_bearing = refined
                    need_broadcast = True
                    logger.debug("bearing 자동 보정: %.1f° (변화량 %.1f°)", refined, diff)

            if need_broadcast:
                roi_ring = _compute_roi_gps_ring()
                await _broadcast({
                    "type": "auto_calibrated",
                    "heading": refined,
                    **({"roi_gps_ring": roi_ring} if roi_ring else {}),
                    **_last_calib_info,
                })

        elapsed = time.perf_counter() - t0
        if elapsed > target_interval:
            skip_budget = min(int(elapsed / target_interval) - 1, 5)
        else:
            await asyncio.sleep(target_interval - elapsed)


# ── 속도 계산용 실시간 타임라인 ───────────────────────────────────────
# 프레임 PTS(CAP_PROP_POS_MSEC) delta를 우선 사용하고, PTS가 0/비단조(카메라 전환,
# 미지원 스트림)면 벽시계 delta로 폴백하여 단조 증가하는 ms 타임라인을 만든다.
# 기존 frame_id/fps 합성값은 HLS 드롭/버퍼링 시 dt를 과소계산 → 속도 과대추정 →
# MAX_REASONABLE_KPH 가드에 걸려 0으로 방치되는 버그의 원인이었다.
_spd_pts_prev:  float | None = None
_spd_wall_prev: float | None = None
_spd_clock_ms:  float = 0.0


def _speed_timestamp_ms(pos_msec: float) -> float:
    global _spd_pts_prev, _spd_wall_prev, _spd_clock_ms
    now = time.monotonic() * 1000.0
    if _spd_pts_prev is None:
        _spd_pts_prev, _spd_wall_prev = pos_msec, now
        return _spd_clock_ms
    d_pts = pos_msec - _spd_pts_prev
    d_wall = now - _spd_wall_prev
    # PTS가 합리적으로 전진(0~10s)했으면 PTS delta, 아니면 벽시계 delta
    _spd_clock_ms += d_pts if 0.0 < d_pts < 10000.0 else max(0.0, d_wall)
    _spd_pts_prev, _spd_wall_prev = pos_msec, now
    return _spd_clock_ms


def _live_process(frame_id, frame, detector, tracker, pos_msec: float = 0.0) -> dict | None:
    global _frame_count, _latest_annotated_jpeg

    h, w = frame.shape[:2]
    timestamp_ms = _speed_timestamp_ms(pos_msec)  # 실시간 타임라인 (PTS 우선)

    _t0 = time.perf_counter()
    tracked = detector.track(frame)
    _track_ms = (time.perf_counter() - _t0) * 1000.0
    tracked, in_cnt, out_cnt, in_ids, out_ids = tracker.update(tracked, (w, h))

    # YOLO annotated 프레임 생성 → /video-stream-yolo 버퍼
    _t0 = time.perf_counter()
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
    _annotate_ms = (time.perf_counter() - _t0) * 1000.0

    _t0 = time.perf_counter()
    vehicles = _build_vehicles(tracked, frame_wh=(w, h))
    _transform_ms = (time.perf_counter() - _t0) * 1000.0

    # ── Vehicle apparent-size scale 관측 누적 ──────────────────────────
    for v_state in vehicles:
        x1, _, x2, y2 = v_state.bbox_xyxy
        _transformer.accumulate_scale_obs(y2, x2 - x1, v_state.class_name, h, w)

    with _frame_count_lock:
        _frame_count += 1

    # 10프레임마다 재피팅 — 적응형 최소관측수(한계2 C): 교통량 적으면 자동 하향.
    _min_obs = (SCALE_MIN_OBS_SPARSE
                if (_frame_count - _scale_switch_frame) > SCALE_SPARSE_AFTER_FRAMES
                else SCALE_MIN_OBS)
    if (not _transformer.locked
            and _frame_count % 10 == 0 and _transformer._scale_obs_since_fit >= _min_obs
            and _current_cam):
        if _transformer.fit_scale_model(_min_obs):
            new_p = _transformer.get_scale_params()
            if new_p:
                _ck = roi_manager.camera_key(_current_cam.get("cctvurl", ""))
                # Drift 감지: 저장된 vp_y와 15% 이상 차이나면 경고 후 덮어쓰기
                _saved_vc = _load_vehicle_calib(_ck)
                if _saved_vc and _saved_vc.get("vp_y") and new_p["B"] != 0:
                    _new_vp = -new_p["C"] / new_p["B"]
                    _drift = abs(_new_vp - _saved_vc["vp_y"]) / max(_saved_vc["vp_y"], 1.0)
                    if _drift > 0.15:
                        logger.warning("vehicle_calib vp_y drift %.0f%% (%.1f→%.1f) — 새 모델로 갱신",
                                       _drift * 100, _saved_vc["vp_y"], _new_vp)
                _save_vehicle_calib(_ck, new_p)

    analytics.frame_h = h  # ① 깊이별 보정용 현재 프레임 높이
    _t0 = time.perf_counter()
    result = analytics.update(frame_id, timestamp_ms, vehicles, in_cnt, out_cnt, in_ids, out_ids)
    _analytics_ms = (time.perf_counter() - _t0) * 1000.0
    _metrics.add_frame(result.vehicles, _track_ms, _transform_ms, _analytics_ms)
    _perf_stats.record(
        track_ms=_track_ms,
        annotate_ms=_annotate_ms,
        transform_ms=_transform_ms,
        analytics_ms=_analytics_ms,
        n_vehicles=len(vehicles),
    )
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
        await _refresh_hls_url_from_its(stream, force=False)


# ── 진입점 ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
