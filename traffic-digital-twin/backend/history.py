"""
history.py — 트래픽 히스토리 SQLite 저장소 ([C] 데이터 저장 & 분석)

단일 주기 샘플러(main.py:history_sampler_loop)가 백그라운드 모니터 카메라들과
현재 라이브 카메라의 스냅샷을 누적 저장한다. 시간대별 차량 수 / 평균 속도
시계열, 피크타임 탐지, CSV 내보내기를 위한 집계 조회를 제공한다.

동시성: 단일 connection(check_same_thread=False) + threading.Lock.
        async 코드에서는 asyncio.to_thread 로 호출해 이벤트 루프를 막지 않는다.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SnapshotRow:
    """샘플러 한 틱에서 카메라 1대의 기록 단위."""
    ts:            float
    cam_key:       str
    name:          str
    name_ko:       str
    lat:           float
    lon:           float
    source:        str          # 'bg' | 'live'
    vehicle_count: int
    class_counts:  str          # JSON 문자열
    status:        str
    avg_speed_kph: float | None  # live 만 채움


_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL    NOT NULL,
    cam_key       TEXT    NOT NULL,
    name          TEXT,
    name_ko       TEXT,
    lat           REAL,
    lon           REAL,
    source        TEXT    NOT NULL,
    vehicle_count INTEGER,
    class_counts  TEXT,
    status        TEXT,
    avg_speed_kph REAL
);
CREATE INDEX IF NOT EXISTS idx_cam_ts ON snapshots(cam_key, ts);
"""


class HistoryStore:
    """트래픽 스냅샷 시계열 저장소 (SQLite)."""

    def __init__(self, db_path: str | Path) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # ── 쓰기 ───────────────────────────────────────────────────────────
    def record_many(self, rows: list[SnapshotRow]) -> None:
        """샘플러 1틱의 모든 행을 단일 트랜잭션으로 INSERT."""
        if not rows:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT INTO snapshots "
                "(ts, cam_key, name, name_ko, lat, lon, source, "
                " vehicle_count, class_counts, status, avg_speed_kph) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (r.ts, r.cam_key, r.name, r.name_ko, r.lat, r.lon, r.source,
                     r.vehicle_count, r.class_counts, r.status, r.avg_speed_kph)
                    for r in rows
                ],
            )
            self._conn.commit()

    def prune(self, before_ts: float) -> int:
        """보존 기간을 넘긴 행 삭제. 삭제된 행 수 반환."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM snapshots WHERE ts < ?", (before_ts,)
            )
            self._conn.commit()
            return cur.rowcount

    # ── 읽기 ───────────────────────────────────────────────────────────
    def cameras(self) -> list[dict]:
        """기록이 있는 카메라 목록 (드롭다운용)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT cam_key, "
                "       MAX(name)    AS name, "
                "       MAX(name_ko) AS name_ko, "
                "       COUNT(*)     AS samples, "
                "       MAX(ts)      AS last_ts "
                "FROM snapshots GROUP BY cam_key ORDER BY last_ts DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def series(self, cam_key: str, since: float, bucket_s: int) -> list[dict]:
        """시간 버킷별 평균 차량수 / 평균 속도 집계.

        bucket_s 초 단위로 묶어 버킷 시작시각(ts)·평균차량수·평균속도를 반환.
        """
        bucket_s = max(1, int(bucket_s))
        with self._lock:
            rows = self._conn.execute(
                "SELECT CAST(ts / ? AS INTEGER) * ? AS bucket, "
                "       AVG(vehicle_count)       AS vehicle_count, "
                "       AVG(avg_speed_kph)       AS avg_speed_kph, "
                "       MAX(vehicle_count)       AS peak_count "
                "FROM snapshots "
                "WHERE cam_key = ? AND ts >= ? "
                "GROUP BY bucket ORDER BY bucket",
                (bucket_s, bucket_s, cam_key, since),
            ).fetchall()
        out: list[dict] = []
        for r in rows:
            out.append({
                "ts": float(r["bucket"]),
                "vehicle_count": round(r["vehicle_count"], 2)
                if r["vehicle_count"] is not None else 0.0,
                "avg_speed_kph": round(r["avg_speed_kph"], 1)
                if r["avg_speed_kph"] is not None else None,
                "peak_count": int(r["peak_count"]) if r["peak_count"] is not None else 0,
            })
        return out

    def peak(self, cam_key: str, since: float) -> dict | None:
        """기간 내 차량수가 가장 많았던 시점(피크타임) 반환."""
        with self._lock:
            r = self._conn.execute(
                "SELECT ts, vehicle_count FROM snapshots "
                "WHERE cam_key = ? AND ts >= ? "
                "ORDER BY vehicle_count DESC, ts ASC LIMIT 1",
                (cam_key, since),
            ).fetchone()
        if r is None:
            return None
        return {"ts": float(r["ts"]), "vehicle_count": int(r["vehicle_count"])}

    def export_rows(self, cam_key: str, since: float) -> list[dict]:
        """CSV 내보내기용 raw 행 (시간 오름차순)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, source, vehicle_count, status, avg_speed_kph, class_counts "
                "FROM snapshots WHERE cam_key = ? AND ts >= ? ORDER BY ts",
                (cam_key, since),
            ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def retention_cutoff(retention_days: float, now: float | None = None) -> float:
    """보존 기간 기준 cutoff 타임스탬프."""
    return (now if now is not None else time.time()) - retention_days * 86400.0
