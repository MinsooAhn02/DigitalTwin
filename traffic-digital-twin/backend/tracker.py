"""
tracker.py — LineZone In/Out 카운팅
  ByteTrack 제거 → BoT-SORT (detector.track()) 가 ID 추적 담당.
  이 모듈은 LineZone 교차 카운팅만 처리한다.
"""

from __future__ import annotations
import supervision as sv
from config import VEHICLE_CLASSES


class VehicleTracker:
    """BoT-SORT 결과를 받아 LineZone 교차 In/Out 을 누적한다."""

    def __init__(self):
        self._line_zone: sv.LineZone | None = None  # 첫 update() 시 프레임 크기 기반 초기화
        self._in_count  = 0
        self._out_count = 0

    def _ensure_line_zone(self, frame_wh: tuple[int, int]) -> None:
        """프레임 크기를 알게 된 시점에 LineZone을 생성한다 (lazy init).
        가로선을 프레임 세로 중앙(y = h/2)에 배치한다."""
        if self._line_zone is not None:
            return
        w, h = frame_wh
        mid_y = h // 2
        self._line_zone = sv.LineZone(
            start=sv.Point(0, mid_y),
            end=sv.Point(w, mid_y),
        )

    def update(
        self, detections: sv.Detections, frame_wh: tuple[int, int]
    ) -> tuple[sv.Detections, int, int, set[int], set[int]]:
        """
        Parameters
        ----------
        detections : BoT-SORT tracker_id 가 채워진 sv.Detections
        frame_wh   : (width, height)

        Returns
        -------
        detections, cumulative_in, cumulative_out, crossed_in_ids, crossed_out_ids
        """
        self._ensure_line_zone(frame_wh)
        crossed_in_ids: set[int] = set()
        crossed_out_ids: set[int] = set()
        if len(detections) and detections.tracker_id is not None:
            crossed_in, crossed_out = self._line_zone.trigger(detections)
            self._in_count  += int(crossed_in.sum())
            self._out_count += int(crossed_out.sum())
            tids = detections.tracker_id
            crossed_in_ids  = set(int(tids[i]) for i in range(len(tids)) if crossed_in[i])
            crossed_out_ids = set(int(tids[i]) for i in range(len(tids)) if crossed_out[i])
        return detections, self._in_count, self._out_count, crossed_in_ids, crossed_out_ids

    def reset_counts(self) -> None:
        self._in_count  = 0
        self._out_count = 0

    @staticmethod
    def class_name(class_id: int) -> str:
        return VEHICLE_CLASSES.get(int(class_id), "unknown")
