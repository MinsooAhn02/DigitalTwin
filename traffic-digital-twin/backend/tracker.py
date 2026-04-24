"""
tracker.py — ByteTrack ID 추적 + LineZone In/Out 카운팅
  입력 : VehicleDetector.detect() 결과 (sv.Detections)
  출력 : (tracked_detections, in_count, out_count)

  ByteTrack: 가려짐 시 low-conf 탐지도 활용 → ID 유지, 중복 카운팅 방지
  LineZone : COUNT_LINE_START ~ COUNT_LINE_END 라인 교차 시 In/Out 집계
"""

from __future__ import annotations
import supervision as sv
from config import (
    BYTE_TRACK_FPS,
    BYTE_TRACK_BUFFER,
    COUNT_LINE_START,
    COUNT_LINE_END,
    VEHICLE_CLASSES,
)


class VehicleTracker:
    """
    supervision ByteTracker + LineZone을 래핑한다.
    매 프레임 update()를 호출하면 추적 결과와 누적 카운트를 반환한다.
    """

    def __init__(self):
        self._tracker = sv.ByteTracker(
            frame_rate=BYTE_TRACK_FPS,
            track_buffer=BYTE_TRACK_BUFFER,
        )

        count_line = sv.LineZone(
            start=sv.Point(*COUNT_LINE_START),
            end=sv.Point(*COUNT_LINE_END),
        )
        self._line_zone = count_line
        self._line_counter = sv.LineZoneAnnotator()   # 어노테이션용 (선택)

        self._in_count  = 0
        self._out_count = 0

    # ──────────────────────────────────────────────────────────────────
    def update(
        self, detections: sv.Detections, frame_wh: tuple[int, int]
    ) -> tuple[sv.Detections, int, int]:
        """
        Returns
        -------
        tracked : sv.Detections  — tracker_id 필드가 채워진 탐지 결과
        in_count  : int          — 누적 진입 대수
        out_count : int          — 누적 진출 대수
        """
        tracked = self._tracker.update_with_detections(detections)

        # LineZone 교차 판정
        crossed_in, crossed_out = self._line_zone.trigger(tracked)
        self._in_count  += int(crossed_in.sum())
        self._out_count += int(crossed_out.sum())

        return tracked, self._in_count, self._out_count

    # ──────────────────────────────────────────────────────────────────
    @staticmethod
    def class_name(class_id: int) -> str:
        return VEHICLE_CLASSES.get(int(class_id), "unknown")

    def reset_counts(self) -> None:
        self._in_count = 0
        self._out_count = 0
