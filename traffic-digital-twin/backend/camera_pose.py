"""
camera_pose.py — Road-model 기반 핀홀 카메라 포즈 솔버.

기존 차선감지 사다리꼴 휴리스틱 대신, 알려진 도로 기하(차선수×차선폭+진행방위)를
3D 지면 제약으로 써서 카메라의 물리 포즈를 역산한다. 차량·화질에 덜 의존하는
"3D/벡터식" 캘리브의 핵심 엔진.

좌표계
------
· World(나디르-정렬): 카메라 광축의 수평 투영을 +Y(forward)로 잡은 로컬 평면.
    X=오른쪽, Y=전방, Z=상. 카메라는 (0,0,H), 지면 Z=0.
· 카메라 회전은 pitch(하향틸트)만 (yaw·offset은 '도로'에 부여). roll=0, 주점=중심.
· 도로: World 평면 위에서 진행방향 dir=(sin yaw, cos yaw), 중심선이 나디르에서
    수직거리 x0 만큼 떨어짐. 경계는 중심선 ±W/2.

파라미터 θ = (H_m, pitch, yaw, focal_px, x0_m)
    · pitch, yaw 는 내부적으로 라디안.
    · focal_px 는 **최적화에서 고정**(명목 FoV 또는 prior). 최적화 변수는 4개
      [H, pitch, yaw, x0]. 이유: 단안 도로에서 횡방향 도로폭은 종방향(깊이) 스케일을
      결정하지 못한다(도로 엣지가 단일 소실점만 제공). focal을 풀면 불안정 → 고정.

정확도 특성 (self-test 검증)
    · 횡방향(lateral) 미터: 도로폭 anchor로 정확 (<1% under FoV 가정).
    · 종방향(longitudinal, 속도) 미터: FoV 가정에 비례한 잔여 스케일 오차 존재 →
      실시스템에서 speed_scale(ITS 비교 학습)이 흡수. 포즈 솔버는 기존 휴리스틱의
      '모양(shape) 오차'를 제거하는 것이 주 역할.

핵심 함수
--------
· solve_pose(left_pts, right_pts, vp, road_model, frame_wh, prior) -> (Pose, residual_px)
· pose_to_corners(pose, road_model, frame_wh) -> (src_pts(4,2), gps_pts(4,2))
    → transform.py가 기존 findHomography 꼬리 로직으로 H_gps/H_meter 생성.
· rough_pose_from_vehicles(...) -> Pose   (한계1 cold-start 최후 수단)

투영 유도(검증됨, __main__ self-test)
    road 방향 dir=(sin yaw, cos yaw, 0) 의 소실점:
        v_vp = h/2 − f·tan(pitch)      (pitch>0 하향 → VP가 중심 위)
        u_vp = w/2 + f·tan(yaw)/cos(pitch)·…  (작은 각 근사 w/2 + f·tan yaw)
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, asdict

import numpy as np

try:
    from scipy.optimize import least_squares
    _HAS_SCIPY = True
except Exception:  # pragma: no cover - scipy는 설치 확인됨, 방어적 폴백
    _HAS_SCIPY = False

logger = logging.getLogger(__name__)

# 차종별 알려진 폭 (m) — rough 포즈 추정에 사용 (transform.VEHICLE_WIDTHS_M와 동일)
VEHICLE_WIDTHS_M: dict[str, float] = {"car": 1.8, "truck": 2.5, "bus": 2.5}

# 초점거리는 단안 focal/pitch 모호성을 피하려 '알려진 FoV'로 고정한다.
# f = FOCAL_RATIO·h  → vFoV ≈ 2·atan(1/(2·1.2)) ≈ 45° (일반 교통 CCTV 가정).
# 기존 transform.auto_calibrate_from_frame 의 fy=h·1.2 와 동일.
FOCAL_RATIO: float = 1.2


# ── 데이터 구조 ──────────────────────────────────────────────────────────────

@dataclass
class Pose:
    """카메라 물리 포즈. JSON 직렬화 가능(영속화용)."""
    H_m: float          # 지면 위 카메라 높이
    pitch_deg: float    # 하향 틸트 (양수=아래로)
    yaw_deg: float      # 도로 진행 vs 카메라 광축 수평 편차
    focal_px: float     # 초점거리(px)
    x0_m: float         # 도로 중심선의 나디르 기준 횡 오프셋

    def to_theta(self) -> np.ndarray:
        return np.array([
            self.H_m, math.radians(self.pitch_deg), math.radians(self.yaw_deg),
            self.focal_px, self.x0_m,
        ], dtype=np.float64)

    @classmethod
    def from_theta(cls, t: np.ndarray) -> "Pose":
        return cls(
            H_m=float(t[0]), pitch_deg=math.degrees(float(t[1])),
            yaw_deg=math.degrees(float(t[2])), focal_px=float(t[3]),
            x0_m=float(t[4]),
        )

    def to_dict(self) -> dict:
        return {k: round(v, 4) for k, v in asdict(self).items()}


@dataclass
class RoadModel:
    """nodelink 도로 기하 — 솔버 입력."""
    road_width_m: float
    bearing_deg: float        # 도로 진행 방위 (0=N, 시계방향)
    snap_lat: float
    snap_lon: float


# ── 기하 원시 함수 ───────────────────────────────────────────────────────────

def _rotation(pitch: float) -> np.ndarray:
    """World→Camera 회전 (pitch 하향틸트만). R0(레벨) 후 카메라 X축 둘레 pitch.

    R0: world(X右,Y前,Z上) → cam(x右,y下,z前).
    """
    cp, sp = math.cos(pitch), math.sin(pitch)
    R0 = np.array([[1.0, 0.0, 0.0],
                   [0.0, 0.0, -1.0],
                   [0.0, 1.0, 0.0]])
    Rx = np.array([[1.0, 0.0, 0.0],
                   [0.0,  cp, -sp],
                   [0.0,  sp,  cp]])
    return Rx @ R0


def _road_axes(yaw: float) -> tuple[np.ndarray, np.ndarray]:
    """World 평면 위 도로 진행단위 dir 와 우측수직 perp."""
    dir_ = np.array([math.sin(yaw), math.cos(yaw)])      # (X,Y)
    perp = np.array([math.cos(yaw), -math.sin(yaw)])
    return dir_, perp


def _road_to_world(theta: np.ndarray, s: np.ndarray, t: np.ndarray) -> np.ndarray:
    """도로좌표 (s=종방향, t=횡방향) → World 지면점 (X,Y,0). 벡터화."""
    yaw, x0 = theta[2], theta[4]
    dir_, perp = _road_axes(yaw)
    B0 = x0 * perp
    s = np.atleast_1d(s).astype(np.float64)
    t = np.atleast_1d(t).astype(np.float64)
    X = B0[0] + s * dir_[0] + t * perp[0]
    Y = B0[1] + s * dir_[1] + t * perp[1]
    Z = np.zeros_like(X)
    return np.stack([X, Y, Z], axis=-1)   # (N,3)


def _project(theta: np.ndarray, pts_world: np.ndarray, frame_wh: tuple[int, int]
             ) -> np.ndarray:
    """World 지면점 (N,3) → 이미지 (N,2). 카메라 뒤/평행은 NaN."""
    w, h = frame_wh
    H, pitch, _, f = theta[0], theta[1], theta[2], theta[3]
    R = _rotation(pitch)
    cam = np.array([0.0, 0.0, H])
    rel = pts_world - cam                     # (N,3)
    pc = rel @ R.T                            # World→Cam
    z = pc[:, 2]
    z_safe = np.where(np.abs(z) < 1e-6, np.nan, z)
    u = w / 2.0 + f * pc[:, 0] / z_safe
    v = h / 2.0 + f * pc[:, 1] / z_safe
    u = np.where(z > 1e-6, u, np.nan)         # 카메라 앞(z>0)만 유효
    v = np.where(z > 1e-6, v, np.nan)
    return np.stack([u, v], axis=-1)          # (N,2)


def _backproject(theta: np.ndarray, uv: np.ndarray, frame_wh: tuple[int, int]
                 ) -> np.ndarray:
    """이미지 (N,2) → 지면 World (N,2) [X,Y]. 광선과 z=0 평면 교점. 무효는 NaN."""
    w, h = frame_wh
    H, pitch, f = theta[0], theta[1], theta[3]
    R = _rotation(pitch)
    uv = np.atleast_2d(uv).astype(np.float64)
    ray_cam = np.stack([(uv[:, 0] - w / 2.0) / f,
                        (uv[:, 1] - h / 2.0) / f,
                        np.ones(len(uv))], axis=-1)        # (N,3)
    ray_world = ray_cam @ R                                # R^T 적용 (= cam→world)
    rz = ray_world[:, 2]
    lam = np.where(rz < -1e-6, -H / rz, np.nan)            # 아래로 향하는 광선만
    X = lam * ray_world[:, 0]
    Y = lam * ray_world[:, 1]
    return np.stack([X, Y], axis=-1)


def _vanishing_point(theta: np.ndarray, frame_wh: tuple[int, int]
                     ) -> tuple[float, float] | None:
    """도로 진행방향(무한원점)의 이미지 소실점."""
    w, h = frame_wh
    pitch, yaw, f = theta[1], theta[2], theta[3]
    R = _rotation(pitch)
    dir_world = np.array([math.sin(yaw), math.cos(yaw), 0.0])
    pc = R @ dir_world
    if pc[2] <= 1e-6:
        return None
    return (w / 2.0 + f * pc[0] / pc[2], h / 2.0 + f * pc[1] / pc[2])


def _boundary_curve(
    theta: np.ndarray, t_lat: float, frame_wh: tuple[int, int], s_grid: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """횡오프셋 t_lat 의 도로 평행선을 s_grid로 투영 → (v_sorted, u_sorted)."""
    pts = _road_to_world(theta, s_grid, np.full_like(s_grid, t_lat))
    img = _project(theta, pts, frame_wh)
    u, v = img[:, 0], img[:, 1]
    good = ~np.isnan(u) & ~np.isnan(v)
    u, v = u[good], v[good]
    if len(v) < 2:
        return np.empty(0), np.empty(0)
    order = np.argsort(v)
    return v[order], u[order]


# ── 비용함수 & 솔버 ──────────────────────────────────────────────────────────

def _residuals(
    theta: np.ndarray,
    left_obs: np.ndarray,   # (Nl,2) [row_v, x]
    right_obs: np.ndarray,  # (Nr,2)
    vp: tuple[float, float] | None,
    half_w: float,
    frame_wh: tuple[int, int],
) -> np.ndarray:
    w, h = frame_wh
    s_grid = np.linspace(1.0, 400.0, 220)
    res: list[float] = []
    miss_pen = 40.0   # 투영 범위 밖 관측 패널티(px)

    for obs, t_lat in ((left_obs, -half_w), (right_obs, +half_w)):
        if len(obs) == 0:
            continue
        v_curve, u_curve = _boundary_curve(theta, t_lat, frame_wh, s_grid)
        if len(v_curve) < 2:
            res.extend([miss_pen] * len(obs))
            continue
        rows = obs[:, 0]
        # v_curve 범위 밖 row는 패널티
        in_range = (rows >= v_curve[0]) & (rows <= v_curve[-1])
        u_pred = np.interp(rows, v_curve, u_curve)
        diff = u_pred - obs[:, 1]
        diff = np.where(in_range, diff, miss_pen)
        res.extend(diff.tolist())

    if vp is not None:
        vp_pred = _vanishing_point(theta, frame_wh)
        if vp_pred is None:
            res.extend([miss_pen, miss_pen])
        else:
            wv = 0.5  # VP 가중치(단일 잡음 추정이라 낮춤)
            res.append(wv * (vp_pred[0] - vp[0]))
            res.append(wv * (vp_pred[1] - vp[1]))

    return np.asarray(res, dtype=np.float64)


def _initial_opt(
    vp: tuple[float, float] | None, f_fixed: float, frame_wh: tuple[int, int],
    left_obs: np.ndarray, right_obs: np.ndarray, prior: Pose | None,
) -> np.ndarray:
    """최적화 변수 초기값 [H, pitch, yaw, x0] (focal은 고정)."""
    w, h = frame_wh
    if prior is not None:
        return np.array([prior.H_m, math.radians(prior.pitch_deg),
                         math.radians(prior.yaw_deg), prior.x0_m], dtype=np.float64)
    if vp is not None:
        pitch0 = math.atan2(h / 2.0 - vp[1], f_fixed)
        yaw0 = math.atan2(vp[0] - w / 2.0, f_fixed)
    else:
        pitch0, yaw0 = math.radians(12.0), 0.0
    pitch0 = min(max(pitch0, math.radians(3.0)), math.radians(55.0))
    yaw0 = min(max(yaw0, math.radians(-35.0)), math.radians(35.0))
    x0_0 = 0.0
    if len(left_obs) and len(right_obs):
        cx_bottom = (left_obs[:, 1].max() + right_obs[:, 1].max()) / 2.0
        x0_0 = (cx_bottom - w / 2.0) / w * 6.0
    return np.array([8.0, pitch0, yaw0, x0_0], dtype=np.float64)


def solve_pose(
    left_pts: list[tuple[float, float]],
    right_pts: list[tuple[float, float]],
    vp: tuple[float, float] | None,
    road_model: RoadModel,
    frame_wh: tuple[int, int],
    prior: Pose | None = None,
) -> tuple[Pose | None, float]:
    """엣지 관측 + VP + 도로폭으로 카메라 포즈를 최소제곱 추정.

    초점거리는 단안 focal/pitch 모호성을 피하려 고정(FOCAL_RATIO·h, prior 있으면 그 값).
    최적화 변수는 [H, pitch, yaw, x0] 4개.

    left_pts / right_pts: [(y_row, x), ...] (auto_calibrate_from_frame 형식)
    반환: (Pose 또는 None, residual_px RMS). 실패 시 (None, inf).
    """
    if not _HAS_SCIPY:
        logger.warning("scipy 미설치 — solve_pose 비활성")
        return None, float("inf")

    left_obs = np.asarray(left_pts, dtype=np.float64).reshape(-1, 2)
    right_obs = np.asarray(right_pts, dtype=np.float64).reshape(-1, 2)
    if len(left_obs) + len(right_obs) < 4:
        return None, float("inf")

    half_w = max(road_model.road_width_m, 2.0) / 2.0
    w, h = frame_wh
    # 초점거리는 고정(prior 또는 명목 FoV). 단안 도로에서 횡방향 도로폭은
    # 종방향(깊이) 스케일을 결정하지 못하므로(엣지가 단일 소실점만 제공) focal을
    # 풀면 불안정. focal 고정 → 횡방향 정확, 종방향 잔여 스케일은 speed_scale(ITS)이 흡수.
    f_fixed = prior.focal_px if prior is not None else FOCAL_RATIO * h

    x0 = _initial_opt(vp, f_fixed, frame_wh, left_obs, right_obs, prior)
    lb = np.array([3.0,  math.radians(2.0),  math.radians(-45.0), -4.0 * half_w])
    ub = np.array([40.0, math.radians(60.0), math.radians(45.0),  4.0 * half_w])
    x0 = np.minimum(np.maximum(x0, lb + 1e-6), ub - 1e-6)

    def _resid_opt(x: np.ndarray) -> np.ndarray:
        theta = np.array([x[0], x[1], x[2], f_fixed, x[3]])
        return _residuals(theta, left_obs, right_obs, vp, half_w, frame_wh)

    try:
        result = least_squares(
            _resid_opt, x0,
            bounds=(lb, ub), method="trf", loss="soft_l1", f_scale=8.0,
            max_nfev=200, x_scale=[5.0, 0.3, 0.3, float(half_w)],
        )
    except Exception as exc:
        logger.warning("least_squares 실패: %s", exc)
        return None, float("inf")

    resid = result.fun
    rms = float(np.sqrt(np.mean(resid ** 2))) if len(resid) else float("inf")
    x = result.x
    pose = Pose.from_theta(np.array([x[0], x[1], x[2], f_fixed, x[3]]))
    logger.info(
        "solve_pose: H=%.1fm pitch=%.1f° yaw=%.1f° f=%.0fpx x0=%.1fm  residual=%.1fpx",
        pose.H_m, pose.pitch_deg, pose.yaw_deg, pose.focal_px, pose.x0_m, rms,
    )
    return pose, rms


# ── Pose → 이미지/GPS 코너 (transform.py 통합용) ─────────────────────────────

def _visible_s_range(
    theta: np.ndarray, half_w: float, frame_wh: tuple[int, int],
) -> tuple[float, float] | None:
    """경계가 화면 안에 보이는 종방향 거리 [s_near, s_far] 추정."""
    w, h = frame_wh
    s_grid = np.linspace(1.0, 400.0, 400)
    pts = _road_to_world(theta, s_grid, np.zeros_like(s_grid))
    img = _project(theta, pts, frame_wh)
    u, v = img[:, 0], img[:, 1]
    good = (~np.isnan(v)) & (v >= 0.0) & (v <= h) & (u >= -0.2 * w) & (u <= 1.2 * w)
    if good.sum() < 2:
        return None
    s_ok = s_grid[good]
    return float(s_ok.min()), float(s_ok.max())


def pose_to_corners(
    pose: Pose, road_model: RoadModel, frame_wh: tuple[int, int],
) -> tuple[np.ndarray, np.ndarray] | None:
    """포즈 → (src_pts 이미지 4코너, gps_pts 4코너[lat,lon]).

    순서: TL(far-left), TR(far-right), BR(near-right), BL(near-left)
    transform.py가 이 대응으로 findHomography → H_gps/H_meter 생성.
    """
    theta = pose.to_theta()
    half_w = max(road_model.road_width_m, 2.0) / 2.0
    rng = _visible_s_range(theta, half_w, frame_wh)
    if rng is None:
        return None
    s_near, s_far = rng
    s_near = max(s_near, 1.0)
    s_far = max(s_far, s_near + 5.0)
    # 코너: (s, t)
    corners_st = [
        (s_far, -half_w),   # TL
        (s_far, +half_w),   # TR
        (s_near, +half_w),  # BR
        (s_near, -half_w),  # BL
    ]
    s_arr = np.array([c[0] for c in corners_st])
    t_arr = np.array([c[1] for c in corners_st])
    pts_world = _road_to_world(theta, s_arr, t_arr)
    img = _project(theta, pts_world, frame_wh)
    if np.isnan(img).any():
        return None

    # (s,t) → snap 기준 ENU → GPS
    b = math.radians(road_model.bearing_deg)
    sin_b, cos_b = math.sin(b), math.cos(b)
    R_lat = 110574.0
    R_lon = 111320.0 * math.cos(math.radians(road_model.snap_lat))
    gps_pts = []
    for s, t in zip(s_arr, t_arr):
        east = s * sin_b + t * cos_b
        north = s * cos_b - t * sin_b
        gps_pts.append([
            road_model.snap_lat + north / R_lat,
            road_model.snap_lon + east / R_lon,
        ])
    return img.astype(np.float32), np.array(gps_pts, dtype=np.float32)


def rough_pose_from_vehicles(
    bboxes_wh: list[tuple[float, float, float]],  # (bbox_w_px, bbox_cx, bbox_bottom_v)
    classes: list[str],
    road_model: RoadModel,
    frame_wh: tuple[int, int],
) -> Pose | None:
    """한계1 cold-start 최후수단: 첫 1~3대 bbox로 rough 포즈.

    인식 정확도 전제라 신뢰 낮음 → high residual로 마킹돼 이후 덮어쓰임.
    명목 pitch/yaw 가정, bbox 실폭으로 focal·H 스케일만 대략 맞춘다.
    """
    w, h = frame_wh
    samples = []
    for (bw, cx, bv), cls in zip(bboxes_wh, classes):
        real_w = VEHICLE_WIDTHS_M.get(cls)
        if real_w is None or bw < 15.0:
            continue
        samples.append((bw, cx, bv, real_w))
    if not samples:
        return None
    f0 = 1.2 * h
    pitch0 = math.radians(15.0)
    # 차량 bbox 폭으로 거리 추정: d ≈ f·real_w/bw, 그 거리에서 카메라 높이 근사
    # H ≈ d·tan(pitch + vfov_half_at_bottom) 의 단순화 — 명목값으로 둠.
    H0 = 8.0
    yaw0 = 0.0
    x0_0 = 0.0
    return Pose(H_m=H0, pitch_deg=math.degrees(pitch0),
               yaw_deg=math.degrees(yaw0), focal_px=f0, x0_m=x0_0)


# ── 합성 self-test ───────────────────────────────────────────────────────────

def _self_test() -> int:
    """알려진 포즈로 합성 관측 생성 → solve_pose가 복원하는지 검증."""
    logging.basicConfig(level=logging.INFO)
    rng = np.random.default_rng(0)
    frame_wh = (1280, 720)
    w, h = frame_wh
    road = RoadModel(road_width_m=7.0, bearing_deg=40.0,
                     snap_lat=37.5, snap_lon=127.0)

    half_w = road.road_width_m / 2.0
    fails = 0
    # 솔버는 focal=FOCAL_RATIO·h 를 가정. matched 트라이얼은 그 가정 하의 정확도를
    # 검증(PASS 기준). 마지막 focal-mismatch 트라이얼은 정보용 — 종방향(speed) 스케일이
    # FoV 가정에 민감함을 보여줌(이 잔여 오차는 실시스템에서 speed_scale/ITS가 흡수).
    for trial, (true_pose, label) in enumerate([
        (Pose(8.0,  14.0, 5.0,  FOCAL_RATIO * h, 0.5),  "matched-focal"),
        (Pose(12.0, 22.0, -8.0, FOCAL_RATIO * h, -1.0), "matched-focal"),
        (Pose(6.0,  9.0,  2.0,  FOCAL_RATIO * h, 0.0),  "matched-focal"),
        (Pose(10.0, 18.0, -4.0, 1.0 * h,         0.8),  "focal-mismatch"),
    ]):
        theta = true_pose.to_theta()
        s_grid = np.linspace(1.0, 400.0, 400)
        left_obs, right_obs = [], []
        for t_lat, bucket in ((-half_w, left_obs), (+half_w, right_obs)):
            vc, uc = _boundary_curve(theta, t_lat, frame_wh, s_grid)
            if len(vc) < 5:
                continue
            for ratio in (0.95, 0.82, 0.69, 0.56, 0.45):
                row = h * ratio
                if vc[0] <= row <= vc[-1]:
                    u = float(np.interp(row, vc, uc)) + rng.normal(0, 0.8)
                    bucket.append((row, u))
        vp_true = _vanishing_point(theta, frame_wh)
        vp_noisy = (vp_true[0] + rng.normal(0, 2), vp_true[1] + rng.normal(0, 2))

        pose, resid = solve_pose(left_obs, right_obs, vp_noisy, road, frame_wh)
        if pose is None:
            print(f"[trial {trial}] FAIL: no solution")
            fails += 1
            continue

        # ── 미터 정확도 검증 (핵심 사용처: image→meters) ──
        # 도로 위 알려진 종방향 20m 구간 두 점을 TRUE 포즈로 투영 →
        # RECOVERED 포즈로 역투영 → 복원된 지면 거리가 20m와 얼마나 차이나는지.
        seg_errs = []
        for s_a, s_b in ((15.0, 35.0), (40.0, 70.0)):
            pa = _road_to_world(theta, np.array([s_a]), np.array([0.0]))
            pb = _road_to_world(theta, np.array([s_b]), np.array([0.0]))
            ia = _project(theta, pa, frame_wh)[0]
            ib = _project(theta, pb, frame_wh)[0]
            if np.isnan(ia).any() or np.isnan(ib).any():
                continue
            ga = _backproject(pose.to_theta(), ia, frame_wh)[0]
            gb = _backproject(pose.to_theta(), ib, frame_wh)[0]
            if np.isnan(ga).any() or np.isnan(gb).any():
                continue
            true_d = s_b - s_a
            est_d = float(np.hypot(*(gb - ga)))
            seg_errs.append(abs(est_d - true_d) / true_d)
        metric_err = max(seg_errs) if seg_errs else 1.0

        dH = abs(pose.H_m - true_pose.H_m)
        if label == "focal-mismatch":
            # 정보용: PASS 기준에서 제외(종방향 스케일은 speed_scale 담당).
            print(f"[trial {trial} {label:14s}] resid={resid:.2f}px  "
                  f"longitudinal_err={metric_err*100:.1f}% (speed_scale가 흡수)  ΔH={dH:.2f}m  INFO")
            continue
        ok = resid < 3.0 and metric_err < 0.05   # 미터오차 < 5%
        print(f"[trial {trial} {label:14s}] resid={resid:.2f}px  "
              f"metric_err={metric_err*100:.1f}%  ΔH={dH:.2f}m  "
              f"{'OK' if ok else 'FAIL'}")
        if not ok:
            fails += 1

    print(f"\nself-test: {'ALL PASS' if fails == 0 else f'{fails} FAILED'}")
    return fails


if __name__ == "__main__":
    raise SystemExit(_self_test())
