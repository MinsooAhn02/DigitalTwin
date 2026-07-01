"""
generate_validation_outputs.py
==============================
위성 지물의 픽셀 좌표를 여러분의 실제 transform.py로 flat/curved 두 방식으로
변환해, validate_localization.py가 채점할 입력 JSON을 완성한다.

배경
----
위치 검증은 같은 지물을 두 방식으로 변환해 비교한다:
  flat   : 평면 호모그래피만 (곡선 매핑 OFF)
  curved : 도로 중심선 곡선 매핑 (곡선 매핑 ON)

이 스크립트는 여러분의 PerspectiveTransformer를 그 카메라 상태로 복원한 뒤,
입력한 지물 픽셀을 두 방식으로 변환해 flat_gps / curved_gps를 채워준다.

사용 순서
--------
1. 위성사진 + CCTV 스틸로 지물 픽셀(u,v)과 GPS(lat,lon)를 측정해
   landmarks_input.json 작성 (아래 SCHEMA 참고).
2. 그 카메라의 캘리브 상태를 복원하는 데 필요한 값을 채운다:
   - calibration_data.json 경로 또는 4-point (pixel_pts, gps_pts)
   - 또는 자동 캘리브 재현에 필요한 cam_lat/lon, bearing, road_width
   - 도로 중심선 road_pts + snap_along_m (set_road_corridor 입력)
3. python generate_validation_outputs.py landmarks_input.json
   → my_cameras.json 생성 (flat_gps, curved_gps 채워짐)
4. python validate_localization.py my_cameras.json
   → 위치 오차 + flat/curved ablation 출력

주의
----
이 스크립트는 backend 디렉터리에서 실행해야 transform/config import가 된다:
   cd backend && python /path/to/generate_validation_outputs.py landmarks_input.json
"""

from __future__ import annotations
import json
import sys
import copy


def build_transformer_for_camera(cam: dict):
    """카메라 한 대의 캘리브 상태로 PerspectiveTransformer를 복원.

    cam dict가 제공하는 것에 따라 세 경로 중 하나로 복원한다:
      (a) manual 4-point: cam["pixel_pts"], cam["gps_pts"]
      (b) gps_center 근사: cam["cam_lat"], cam["cam_lon"], cam["bearing_deg"]
      (c) 이미 저장된 calibration_data.json을 로드하는 경우는 호출측에서 처리
    그 뒤 road_pts/snap_along_m이 있으면 set_road_corridor로 곡선 매핑 활성화.
    """
    from transform import PerspectiveTransformer

    tf = PerspectiveTransformer()

    # ── 캘리브 상태 복원 ──
    if "pixel_pts" in cam and "gps_pts" in cam:
        # (a) 수동 4-point — 단, 이 경우 _is_calibrated=True가 되어
        #     curved 경로가 비활성된다. 검증에서는 곡선 효과를 보려면
        #     아래에서 _is_calibrated를 강제로 False로 되돌린다(곡선 비교용).
        tf.update_from_calibration(cam["pixel_pts"], cam["gps_pts"])
    elif "cam_lat" in cam and "cam_lon" in cam:
        # (b) GPS 근사 캘리브 (자동 캘리브와 동일 출발점)
        tf.update_gps_center(
            cam["cam_lat"], cam["cam_lon"], cam.get("bearing_deg", 0.0)
        )
    else:
        raise ValueError(
            f"카메라 {cam.get('cam_id')}: 캘리브 입력 부족 "
            "(pixel_pts+gps_pts 또는 cam_lat+cam_lon 필요)"
        )

    return tf


def transform_landmarks(cam: dict) -> dict:
    """한 카메라의 모든 지물을 flat/curved로 변환해 cam dict를 채운다."""
    road_pts = cam.get("road_pts")
    snap_along_m = cam.get("snap_along_m")

    out = copy.deepcopy(cam)

    for c in out["correspondences"]:
        u, v = c["pixel"]

        # ── flat: 곡선 매핑 OFF ──
        tf_flat = build_transformer_for_camera(cam)
        tf_flat.set_road_corridor(None, None)      # 곡선 비활성
        tf_flat._is_calibrated = False             # H_gps 경로 강제
        # 단, manual 4-point였다면 H_gps가 이미 정확하므로 그대로 사용
        flat_lat, flat_lon = tf_flat.pixel_to_gps(u, v)
        c["flat_gps"] = [flat_lat, flat_lon]

        # ── curved: 곡선 매핑 ON ──
        tf_curved = build_transformer_for_camera(cam)
        if road_pts and snap_along_m is not None:
            tf_curved.set_road_corridor(road_pts, snap_along_m)
            tf_curved._is_calibrated = False       # 곡선 경로 활성 조건
            cur_lat, cur_lon = tf_curved.pixel_to_gps(u, v)
            c["curved_gps"] = [cur_lat, cur_lon]
        else:
            # 도로 중심선이 없으면 curved=flat (곡선 비교 불가)
            c["curved_gps"] = [flat_lat, flat_lon]

    return out


def main(input_path: str):
    with open(input_path) as f:
        data = json.load(f)

    cameras = data["cameras"] if "cameras" in data else [data]
    out_cameras = []
    for cam in cameras:
        try:
            filled = transform_landmarks(cam)
            n = len(filled["correspondences"])
            print(f"  {cam.get('cam_id','?'):>30}: {n}개 지물 변환 완료")
            out_cameras.append(filled)
        except Exception as exc:
            print(f"  {cam.get('cam_id','?'):>30}: 실패 — {exc}")

    out = {"cameras": out_cameras}
    out_path = "my_cameras.json"
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"\n→ {out_path} 생성 완료. 다음 실행:")
    print(f"    python validate_localization.py {out_path}")


# ──────────────────────────────────────────────────────────────
# SCHEMA (landmarks_input.json 예시) — 인자 없이 실행하면 템플릿 생성
# ──────────────────────────────────────────────────────────────
TEMPLATE = {
    "cameras": [
        {
            "cam_id": "예시_곡선카메라_A",
            "_캘리브_입력": "아래 둘 중 하나. (a) 수동 4-point 또는 (b) GPS 근사",
            "cam_lat": 37.5731,
            "cam_lon": 127.2247,
            "bearing_deg": 134.8,
            "_또는_수동": "pixel_pts/gps_pts를 쓰면 위 cam_lat/lon 대신 사용",
            "road_pts": [
                [37.5730, 127.2245],
                [37.5732, 127.2248],
                [37.5735, 127.2252]
            ],
            "snap_along_m": 250.0,
            "_correspondences_설명": (
                "위성에서 측정한 지물들. pixel=CCTV 픽셀(u,v), "
                "gps=위성에서 읽은 실제 (lat,lon). flat_gps/curved_gps는 "
                "이 스크립트가 자동으로 채운다."
            ),
            "correspondences": [
                {"pixel": [512, 360], "gps": [37.5731, 127.2247]},
                {"pixel": [330, 280], "gps": [37.5733, 127.2249]},
                {"pixel": [700, 300], "gps": [37.5732, 127.2250]}
            ]
        }
    ]
}

if __name__ == "__main__":
    if len(sys.argv) < 2:
        with open("landmarks_input.json", "w") as f:
            json.dump(TEMPLATE, f, indent=2, ensure_ascii=False)
        print("인자 없음 → landmarks_input.json 템플릿 생성.")
        print("채운 뒤: python generate_validation_outputs.py landmarks_input.json")
        print("\n주의: 이 스크립트는 backend 디렉터리에서 실행해야 transform import가 됩니다.")
    else:
        main(sys.argv[1])
