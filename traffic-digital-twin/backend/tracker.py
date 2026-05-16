"""
tracker.py — LineZone In/Out 카운팅
  ByteTrack 제거 → BoT-SORT (detector.track()) 가 ID 추적 담당.
  이 모듈은 LineZone 교차 카운팅만 처리한다.
"""

from __future__ import annotations
import supervision as sv
from config import COUNT_LINE_START, COUNT_LINE_END, VEHICLE_CLASSES


class VehicleTracker:
    """BoT-SORT 결과를 받아 LineZone 교차 In/Out 을 누적한다."""

    def __init__(self):
        self._line_zone = sv.LineZone(
            start=sv.Point(*COUNT_LINE_START),
            end=sv.Point(*COUNT_LINE_END),
        )
        self._in_count  = 0
        self._out_count = 0

    def update(
        self, detections: sv.Detections, frame_wh: tuple[int, int]
    ) -> tuple[sv.Detections, int, int]:
        """
        Parameters
        ----------
        detections : BoT-SORT tracker_id 가 채워진 sv.Detections
        frame_wh   : (width, height) — 현재 사용 안 하지만 시그니처 유지

        Returns
        -------
        detections, cumulative_in, cumulative_out
        """
        if len(detections) and detections.tracker_id is not None:
            crossed_in, crossed_out = self._line_zone.trigger(detections)
            self._in_count  += int(crossed_in.sum())
            self._out_count += int(crossed_out.sum())
        return detections, self._in_count, self._out_count

    def reset_counts(self) -> None:
        self._in_count  = 0
        self._out_count = 0

    @staticmethod
    def class_name(class_id: int) -> str:
        return VEHICLE_CLASSES.get(int(class_id), "unknown")
