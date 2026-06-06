"""perf.py — 프레임별 파이프라인 타이밍 수집 및 perf_log.jsonl 자동 기록.

사용법:
  - 서버 실행만 하면 자동으로 100프레임마다 backend/perf_log.jsonl 에 한 줄씩 기록됨.
  - POST /debug/profile/start → stop 으로 yappi 함수별 상세 프로파일 수집.
  - POST /debug/perf/reset 으로 perf_log.jsonl 초기화.
"""
from __future__ import annotations

import json
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

import numpy as np

_LOGS_DIR          = Path(__file__).resolve().parent / "logs"
PERF_LOG_PATH      = _LOGS_DIR / "perf_log.jsonl"
PROFILE_STATS_PATH = _LOGS_DIR / "profile_stats.txt"
_FLUSH_EVERY = 100  # 몇 프레임마다 JSONL 한 줄 기록

try:
    import yappi as _yappi
    YAPPI_AVAILABLE = True
except ImportError:
    _yappi = None  # type: ignore
    YAPPI_AVAILABLE = False


class PerfStats:
    """100프레임마다 타이밍 분위수를 perf_log.jsonl 에 append."""

    _METRICS = ("track_ms", "annotate_ms", "transform_ms", "analytics_ms", "total_ms")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buf: dict[str, deque[float]] = {k: deque(maxlen=_FLUSH_EVERY) for k in self._METRICS}
        self._n_vehicles: deque[int] = deque(maxlen=_FLUSH_EVERY)
        self._frame_count = 0

    def record(
        self,
        *,
        track_ms: float,
        annotate_ms: float,
        transform_ms: float,
        analytics_ms: float,
        n_vehicles: int,
    ) -> None:
        total = track_ms + annotate_ms + transform_ms + analytics_ms
        with self._lock:
            self._buf["track_ms"].append(track_ms)
            self._buf["annotate_ms"].append(annotate_ms)
            self._buf["transform_ms"].append(transform_ms)
            self._buf["analytics_ms"].append(analytics_ms)
            self._buf["total_ms"].append(total)
            self._n_vehicles.append(n_vehicles)
            self._frame_count += 1
            if self._frame_count % _FLUSH_EVERY == 0:
                self._flush()

    def _flush(self) -> None:
        row: dict[str, Any] = {
            "ts": time.strftime("%H:%M:%S"),
            "frame": self._frame_count,
        }
        for key, dq in self._buf.items():
            if not dq:
                continue
            arr = np.array(dq, dtype=np.float32)
            row[key] = {
                "avg": round(float(np.mean(arr)), 2),
                "p95": round(float(np.percentile(arr, 95)), 2),
                "max": round(float(np.max(arr)), 2),
            }
        veh = np.array(self._n_vehicles, dtype=np.float32)
        row["n_vehicles_avg"] = round(float(np.mean(veh)), 1) if len(veh) else 0.0

        _LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with open(PERF_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def reset(self) -> None:
        with self._lock:
            for dq in self._buf.values():
                dq.clear()
            self._n_vehicles.clear()
            self._frame_count = 0
        if PERF_LOG_PATH.exists():
            PERF_LOG_PATH.unlink()


# ── yappi 래퍼 ────────────────────────────────────────────────────────

def yappi_start() -> dict:
    if not YAPPI_AVAILABLE:
        return {"error": "yappi not installed — run: pip install yappi"}
    _yappi.clear_stats()
    _yappi.start(builtins=False, profile_threads=True)
    return {"ok": True, "msg": "yappi profiling started (threads=all)"}


def yappi_stop() -> dict:
    if not YAPPI_AVAILABLE:
        return {"error": "yappi not installed"}
    _yappi.stop()
    stats = _yappi.get_func_stats()
    stats.sort("ttot", "desc")

    _LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with open(PROFILE_STATS_PATH, "w", encoding="utf-8") as f:
        stats.print_all(
            out=f,
            columns={
                0: ("name",  80),
                1: ("ncall",  8),
                2: ("tsub",   8),
                3: ("ttot",   8),
                4: ("tavg",   8),
            },
        )

    top: list[dict] = []
    for s in list(stats)[:30]:
        top.append({
            "func":     s.full_name,
            "ncall":    s.ncall,
            "ttot_s":   round(s.ttot, 4),
            "tavg_ms":  round(s.tavg * 1000, 3),
        })
    _yappi.clear_stats()
    return {
        "ok": True,
        "saved_to": str(PROFILE_STATS_PATH),
        "top30": top,
    }
