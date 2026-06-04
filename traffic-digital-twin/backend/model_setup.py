"""
Pick and prepare the YOLO model before the backend starts.

The script detects the local machine, shows a small model picker, then ensures the
selected model is ready in the best available format:

1. Existing TensorRT .engine: use it immediately.
2. Existing .pt weights: export to TensorRT when possible.
3. Missing weights: download via Ultralytics, then export when possible.
"""

from __future__ import annotations

from dataclasses import dataclass
import ctypes
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys


BACKEND_DIR = Path(__file__).resolve().parent
CHOICE_FILE = BACKEND_DIR / ".yolo_model"
PROFILE_FILE = BACKEND_DIR / ".runtime_profile.json"
IMG_SIZE = int(os.getenv("YOLO_IMGSZ", "640"))


@dataclass(frozen=True)
class HardwareInfo:
    cpu_name: str
    cpu_cores: int
    ram_gb: float
    cuda_available: bool
    gpu_name: str
    gpu_memory_gb: float
    tensorrt_available: bool


# 기본 추천 패밀리 — yolo26(최신, end-to-end NMS-free). yolov8도 picker에서 선택 가능.
DEFAULT_FAMILY = "yolo26"


@dataclass(frozen=True)
class ModelOption:
    variant:    str
    name:       str
    label:      str
    note:       str
    note_en:    str
    min_gpu_gb: float
    profile:    str
    size_mb:    int    # .pt 다운로드 크기 (MB)
    fps_gpu:    int    # TensorRT 기준 예상 FPS
    fps_cpu:    int    # CPU only 예상 FPS
    export_min: int    # TensorRT 변환 소요 시간 (분)
    family:     str = "yolov8"   # "yolov8" | "yolo26"

    @property
    def stem(self) -> str:
        # ultralytics 명명: yolov8m / yolo26m (yolo26는 'v' 없음)
        return f"{self.family}{self.variant}"

    @property
    def pt_path(self) -> Path:
        return BACKEND_DIR / f"{self.stem}.pt"

    @property
    def engine_path(self) -> Path:
        return BACKEND_DIR / f"{self.stem}.engine"


# yolo26 먼저(기본 추천 패밀리) → yolov8. 같은 variant letter가 두 패밀리에 존재하므로
# 선택 토큰은 stem(예: "yolo26m")을 사용한다.
MODEL_OPTIONS = [
    ModelOption("n", "YOLO26n", "Fastest",      "CPU 친화적 · end-to-end",          "CPU-friendly · end-to-end",                     0.0, "fast",      6,  60,  9, 1, "yolo26"),
    ModelOption("s", "YOLO26s", "Balanced",     "중급 GPU · 속도·정확도 균형",       "Mid-range GPU · balanced",                      4.0, "balanced", 26,  52,  6, 2, "yolo26"),
    ModelOption("m", "YOLO26m", "Accurate",     "높은 정확도 · 6 GB+ VRAM",          "High accuracy · 6 GB+ VRAM",                    6.0, "quality",  55,  34,  3, 5, "yolo26"),
    ModelOption("x", "YOLO26x", "Best accuracy","최고 정확도 · 고사양 GPU",          "Best accuracy · high-end GPU",                  8.0, "quality", 135,  27,  1, 8, "yolo26"),
    ModelOption("n", "YOLOv8n", "Fastest",      "CPU 친화적 · 가벼운 데모용",         "CPU-friendly · lightweight demo",               0.0, "fast",      6,  50,  6, 1, "yolov8"),
    ModelOption("s", "YOLOv8s", "Balanced",     "중급 GPU 추천 · 속도·정확도 균형",   "Mid-range GPU · balanced speed & accuracy",     4.0, "balanced", 22,  45,  4, 2, "yolov8"),
    ModelOption("m", "YOLOv8m", "Accurate",     "높은 정확도 · 6 GB+ VRAM 필요",     "High accuracy · 6 GB+ VRAM required",           6.0, "quality",  52,  30,  2, 5, "yolov8"),
    ModelOption("x", "YOLOv8x", "Best accuracy","최고 정확도 · 고사양 GPU 전용",      "Best accuracy · high-end GPU only",             8.0, "quality", 131,  25,  1, 8, "yolov8"),
]

PROFILE_SETTINGS = {
    "fast": {
        "profile": "fast",
        "backend_fps": 12,
        "jpeg_quality": 70,
        "capture_interval_ms": 120,
        "capture_width": 416,
        "capture_quality": 0.72,
        "max_in_flight": 1,
    },
    "balanced": {
        "profile": "balanced",
        "backend_fps": 20,
        "jpeg_quality": 78,
        "capture_interval_ms": 66,
        "capture_width": 512,
        "capture_quality": 0.82,
        "max_in_flight": 1,
    },
    "quality": {
        "profile": "quality",
        "backend_fps": 30,
        "jpeg_quality": 85,
        "capture_interval_ms": 33,
        "capture_width": 640,
        "capture_quality": 0.92,
        "max_in_flight": 2,
    },
}


STRINGS: dict[str, dict[str, str]] = {
    "ko": {
        "title":             "YOLO 모델 설정",
        "lang_switch":       "🌐 English",
        "hw_cpu":            "CPU  {cores}코어   RAM  {ram:.0f} GB",
        "trt_ok":            "TensorRT ✓  가속 가능",
        "trt_missing":       "TensorRT 미설치 — 선택 시 자동 설치 시도",
        "trt_none":          "TensorRT 사용 불가 (GPU 없음) — CPU 모드",
        "cuda_chk":          "CUDA / TensorRT 가속 사용",
        "no_gpu_hint":       " (호환 GPU 없음)",
        "model_lbl":         "모델 선택:",
        "rec_btn":           "⭐  추천 모델 사용: {name} ({label})  —  내 GPU ({vram:.1f} GB) 기준 최적",
        "rec_hint":          "GPU 메모리 용량을 기준으로 자동 선택됩니다",
        "badge_engine":      "✓  ENGINE READY",
        "badge_pt":          "✓  WEIGHTS READY",
        "badge_dl":          "⬇  ~{mb} MB",
        "fps_cpu_only":      "CPU 전용: ~{fps} fps  (GPU 없음)",
        "fps_trt":           "TRT: ~{fps_gpu} fps  ·  PyTorch GPU: ~{fps_pt} fps  ·  CPU: ~{fps_cpu} fps",
        "fps_cpu_mode":      "CPU: ~{fps} fps  (CUDA 비활성)",
        "setup_ready":       "▶ 즉시 시작 가능",
        "setup_ready_notrt": "▶ 즉시 시작 가능 (TensorRT 없음 — CPU 모드)",
        "setup_trt":         "⏱ TensorRT 변환: 약 {min}분 (최초 1회)",
        "setup_dl_trt":      "⏱ 다운로드 ~{mb} MB + TensorRT 변환: 총 약 {total}분",
        "setup_dl":          "⬇ ~{mb} MB 다운로드 후 즉시 시작",
    },
    "en": {
        "title":             "YOLO Model Setup",
        "lang_switch":       "🌐 한국어",
        "hw_cpu":            "CPU  {cores} cores   RAM  {ram:.0f} GB",
        "trt_ok":            "TensorRT ✓  acceleration available",
        "trt_missing":       "TensorRT not installed — will auto-install on selection",
        "trt_none":          "TensorRT unavailable (no GPU) — CPU mode",
        "cuda_chk":          "Use CUDA / TensorRT acceleration",
        "no_gpu_hint":       " (no compatible GPU)",
        "model_lbl":         "Select model:",
        "rec_btn":           "⭐  Use recommended: {name} ({label})  —  optimal for your GPU ({vram:.1f} GB)",
        "rec_hint":          "Auto-selected based on GPU memory capacity",
        "badge_engine":      "✓  ENGINE READY",
        "badge_pt":          "✓  WEIGHTS READY",
        "badge_dl":          "⬇  ~{mb} MB",
        "fps_cpu_only":      "CPU only: ~{fps} fps  (no GPU)",
        "fps_trt":           "TRT: ~{fps_gpu} fps  ·  PyTorch GPU: ~{fps_pt} fps  ·  CPU: ~{fps_cpu} fps",
        "fps_cpu_mode":      "CPU: ~{fps} fps  (CUDA disabled)",
        "setup_ready":       "▶ Ready to start",
        "setup_ready_notrt": "▶ Ready (no TensorRT — CPU mode)",
        "setup_trt":         "⏱ TensorRT export: ~{min} min (one-time)",
        "setup_dl_trt":      "⏱ Download ~{mb} MB + TensorRT export: ~{total} min total",
        "setup_dl":          "⬇ Download ~{mb} MB then start",
    },
}


def _ram_gb() -> float:
    class MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatusEx()
    status.dwLength = ctypes.sizeof(MemoryStatusEx)
    if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return status.ullTotalPhys / (1024**3)
    return 0.0


def detect_hardware() -> HardwareInfo:
    cuda_available = False
    gpu_name = "None"
    gpu_memory_gb = 0.0
    try:
        import torch

        cuda_available = torch.cuda.is_available()
        if cuda_available:
            gpu_name = torch.cuda.get_device_name(0)
            props = torch.cuda.get_device_properties(0)
            gpu_memory_gb = props.total_memory / (1024**3)
    except Exception:
        pass

    tensorrt_available = (
        cuda_available and importlib.util.find_spec("tensorrt") is not None
    )
    return HardwareInfo(
        cpu_name=platform.processor() or platform.machine() or "Unknown CPU",
        cpu_cores=os.cpu_count() or 0,
        ram_gb=_ram_gb(),
        cuda_available=cuda_available,
        gpu_name=gpu_name,
        gpu_memory_gb=gpu_memory_gb,
        tensorrt_available=tensorrt_available,
    )


def recommend_variant(hw: HardwareInfo) -> str:
    if not hw.cuda_available:
        return "n"
    if hw.gpu_memory_gb >= 8:
        return "x"
    if hw.gpu_memory_gb >= 6:
        return "m"
    if hw.gpu_memory_gb >= 4:
        return "s"
    return "n"


def option_status(option: ModelOption) -> str:
    if option.engine_path.exists():
        return "TensorRT engine ready"
    if option.pt_path.exists():
        return "YOLO weights ready"
    return "Will download weights"


def _fps_line(option: ModelOption, hw: HardwareInfo, use_cuda: bool | None = None, lang: str = "ko") -> str:
    S = STRINGS[lang]
    if not hw.cuda_available:
        return S["fps_cpu_only"].format(fps=option.fps_cpu)
    cuda_on = use_cuda if use_cuda is not None else True
    fps_pt = max(1, round(option.fps_gpu * 0.45))
    if cuda_on:
        return S["fps_trt"].format(fps_gpu=option.fps_gpu, fps_pt=fps_pt, fps_cpu=option.fps_cpu)
    return S["fps_cpu_mode"].format(fps=option.fps_cpu)


def _setup_line(option: ModelOption, hw: HardwareInfo, lang: str = "ko") -> tuple[str, str]:
    """(text, color_hint) — color_hint: 'green' | 'yellow' | 'orange'"""
    S = STRINGS[lang]
    has_engine = option.engine_path.exists()
    has_pt     = option.pt_path.exists()
    trt_usable = hw.cuda_available

    if has_engine:
        return S["setup_ready"], "green"
    if has_pt and not trt_usable:
        return S["setup_ready_notrt"], "yellow"
    if has_pt:
        return S["setup_trt"].format(min=option.export_min), "yellow"
    if trt_usable:
        dl_min = max(1, option.size_mb // 20)
        total  = option.export_min + dl_min
        return S["setup_dl_trt"].format(mb=option.size_mb, total=total), "orange"
    return S["setup_dl"].format(mb=option.size_mb), "orange"


def choose_with_gui(hw: HardwareInfo, recommended: str) -> str | None:
    try:
        import tkinter as tk
    except Exception:
        return None

    # ── 색상 팔레트 ────────────────────────────────────────────────────
    BG           = "#0f0f1a"
    CARD_DEFAULT = "#1c1c2e"
    CARD_INST    = "#0d2318"
    CARD_REC     = "#0d1f35"
    CARD_BOTH    = "#0d2025"
    C_PRIMARY    = "#e8e8f4"
    C_SECONDARY  = "#8888aa"
    C_GREEN      = "#4ade80"
    C_BLUE       = "#60a5fa"
    C_YELLOW     = "#fbbf24"
    C_ORANGE     = "#fb923c"
    BORDER_INST  = "#2a5c3a"
    BORDER_REC   = "#2a4a8c"
    BORDER_DEF   = "#2a2a3e"

    selected  = {"stem": None, "use_cuda": None}
    lang      = ["ko"]                             # mutable language cell
    updatables: list[tuple] = []                   # 정적 크롬 (StringVar, fn)
    card_updatables: list[tuple] = []              # 현재 family 카드 (family 전환 시 교체)

    def _s(key: str, **kw) -> str:
        tmpl = STRINGS[lang[0]][key]
        return tmpl.format(**kw) if kw else tmpl

    def _sv(fn, target: list | None = None) -> "tk.StringVar":
        sv = tk.StringVar(value=fn())
        (target if target is not None else updatables).append((sv, fn))
        return sv

    def refresh(*_) -> None:
        for sv, fn in (*updatables, *card_updatables):
            sv.set(fn())

    root = tk.Tk()
    root.configure(bg=BG)
    root.resizable(False, False)
    root.title(_s("title"))

    use_cuda_var = tk.BooleanVar(value=hw.cuda_available)
    use_cuda_var.trace_add("write", refresh)

    outer = tk.Frame(root, bg=BG, padx=22, pady=18)
    outer.pack(fill="both", expand=True)

    # ── 제목 + 언어 토글 버튼 ─────────────────────────────────────────
    title_row = tk.Frame(outer, bg=BG)
    title_row.pack(fill="x", pady=(0, 0))

    tk.Label(title_row, text="YOLO Model Setup",
             bg=BG, fg=C_PRIMARY, font=("Segoe UI", 15, "bold")).pack(side="left", anchor="w")

    def toggle_lang() -> None:
        lang[0] = "en" if lang[0] == "ko" else "ko"
        root.title(_s("title"))
        refresh()

    lang_sv = _sv(lambda: _s("lang_switch"))
    tk.Button(
        title_row, textvariable=lang_sv,
        command=toggle_lang,
        bg="#1e1e30", fg=C_SECONDARY,
        activebackground="#2a2a40", activeforeground=C_PRIMARY,
        font=("Segoe UI", 9), relief="flat", bd=0,
        padx=10, pady=4, cursor="hand2",
    ).pack(side="right", anchor="e")

    tk.Frame(outer, bg="#2a2a40", height=1).pack(fill="x", pady=(6, 12))

    # ── 하드웨어 요약 ──────────────────────────────────────────────────
    hw_card = tk.Frame(outer, bg="#14142a", padx=14, pady=10)
    hw_card.pack(fill="x", pady=(0, 12))

    hw_cpu_sv = _sv(lambda: _s("hw_cpu", cores=hw.cpu_cores, ram=hw.ram_gb))
    tk.Label(hw_card, textvariable=hw_cpu_sv,
             bg="#14142a", fg=C_SECONDARY, font=("Segoe UI", 9)).pack(anchor="w")

    tk.Label(hw_card,
             text=f"GPU  {hw.gpu_name}  ({hw.gpu_memory_gb:.1f} GB VRAM)",
             bg="#14142a", fg=C_PRIMARY, font=("Segoe UI", 9, "bold")).pack(anchor="w")

    if hw.tensorrt_available:
        trt_key, trt_fg = "trt_ok", C_GREEN
    elif hw.cuda_available:
        trt_key, trt_fg = "trt_missing", C_YELLOW
    else:
        trt_key, trt_fg = "trt_none", "#f87171"
    trt_sv = _sv(lambda k=trt_key: _s(k))
    tk.Label(hw_card, textvariable=trt_sv, bg="#14142a", fg=trt_fg,
             font=("Segoe UI", 9)).pack(anchor="w")

    # ── CUDA 체크박스 ──────────────────────────────────────────────────
    cuda_row = tk.Frame(outer, bg=BG)
    cuda_row.pack(anchor="w", pady=(0, 10))

    cuda_sv = _sv(lambda: _s("cuda_chk"))
    tk.Checkbutton(
        cuda_row, textvariable=cuda_sv,
        variable=use_cuda_var, bg=BG, fg=C_PRIMARY,
        selectcolor="#2a2a3e", activebackground=BG, activeforeground=C_PRIMARY,
        state="normal" if hw.cuda_available else "disabled",
        font=("Segoe UI", 10),
    ).pack(side="left")
    if not hw.cuda_available:
        no_gpu_sv = _sv(lambda: _s("no_gpu_hint"))
        tk.Label(cuda_row, textvariable=no_gpu_sv, bg=BG, fg=C_SECONDARY,
                 font=("Segoe UI", 9)).pack(side="left")

    model_lbl_sv = _sv(lambda: _s("model_lbl"))
    tk.Label(outer, textvariable=model_lbl_sv, bg=BG, fg=C_SECONDARY,
             font=("Segoe UI", 9)).pack(anchor="w", pady=(0, 5))

    color_map = {"green": C_GREEN, "yellow": C_YELLOW, "orange": C_ORANGE}

    def pick(stem: str) -> None:
        selected["stem"] = stem
        selected["use_cuda"] = "true" if use_cuda_var.get() else "false"
        root.destroy()

    def _bind_click(widget: "tk.Widget", stem: str) -> None:
        widget.bind("<Button-1>", lambda _e, v=stem: pick(v))
        for child in widget.winfo_children():
            _bind_click(child, stem)

    # ── 모델 패밀리 탭 (먼저 family 선택 → 해당 family 변형만 표시) ────────
    # 8개 카드를 한 화면에 쌓으면 넘치므로, family를 탭으로 먼저 고르고
    # 그 family의 4개 변형만 보여준다.
    FAMILIES = [f for f in ("yolo26", "yolov8")
                if any(o.family == f for o in MODEL_OPTIONS)]
    FAMILY_LABELS = {"yolo26": "YOLO26  (latest)", "yolov8": "YOLOv8"}
    active_family = [select_option(recommended).family]

    tab_row = tk.Frame(outer, bg=BG)
    tab_row.pack(fill="x", pady=(0, 8))
    tab_btns: dict[str, "tk.Button"] = {}

    cards_container = tk.Frame(outer, bg=BG)
    cards_container.pack(fill="x")

    def _build_one_card(option) -> None:
        has_engine = option.engine_path.exists()
        has_pt     = option.pt_path.exists()
        is_inst    = has_engine or has_pt
        is_rec     = option.stem == recommended

        if is_inst and is_rec:
            card_bg, border = CARD_BOTH,    BORDER_REC
        elif is_inst:
            card_bg, border = CARD_INST,    BORDER_INST
        elif is_rec:
            card_bg, border = CARD_REC,     BORDER_REC
        else:
            card_bg, border = CARD_DEFAULT, BORDER_DEF

        border_f = tk.Frame(cards_container, bg=border, padx=1, pady=1)
        border_f.pack(fill="x", pady=3)
        card = tk.Frame(border_f, bg=card_bg, padx=13, pady=9, cursor="hand2")
        card.pack(fill="both")

        row_top = tk.Frame(card, bg=card_bg)
        row_top.pack(fill="x")

        star   = "⭐ " if is_rec else "    "
        name_c = C_BLUE if is_rec else C_PRIMARY
        tk.Label(row_top, text=f"{star}{option.name}  ·  {option.label}",
                 bg=card_bg, fg=name_c,
                 font=("Segoe UI", 11, "bold")).pack(side="left")

        if has_engine:
            badge_key, badge_c = "badge_engine", C_GREEN
        elif has_pt:
            badge_key, badge_c = "badge_pt",     C_YELLOW
        else:
            badge_key, badge_c = "badge_dl",     C_ORANGE
        badge_sv = _sv(lambda k=badge_key, o=option: _s(k, mb=o.size_mb), card_updatables)
        tk.Label(row_top, textvariable=badge_sv, bg=card_bg, fg=badge_c,
                 font=("Segoe UI", 9, "bold")).pack(side="right")

        note_sv = _sv(lambda o=option: o.note if lang[0] == "ko" else o.note_en, card_updatables)
        tk.Label(card, textvariable=note_sv, bg=card_bg, fg=C_SECONDARY,
                 font=("Segoe UI", 9)).pack(anchor="w", pady=(1, 0))

        row_bot = tk.Frame(card, bg=card_bg)
        row_bot.pack(fill="x", pady=(5, 0))

        fps_sv = _sv(lambda o=option: _fps_line(o, hw, use_cuda_var.get(), lang[0]), card_updatables)
        tk.Label(row_bot, textvariable=fps_sv,
                 bg=card_bg, fg=C_PRIMARY, font=("Segoe UI", 9)).pack(side="left")

        _, setup_hint = _setup_line(option, hw, lang[0])
        setup_sv = _sv(lambda o=option: _setup_line(o, hw, lang[0])[0], card_updatables)
        tk.Label(row_bot, textvariable=setup_sv,
                 bg=card_bg, fg=color_map[setup_hint],
                 font=("Segoe UI", 9)).pack(side="right")

        _bind_click(card, option.stem)

    def _build_cards() -> None:
        for ch in cards_container.winfo_children():
            ch.destroy()
        card_updatables.clear()
        for option in MODEL_OPTIONS:
            if option.family == active_family[0]:
                _build_one_card(option)
        refresh()                     # 새 카드 텍스트를 현재 lang/cuda로 동기화
        root.update_idletasks()       # 창 높이 재계산

    def _style_tabs() -> None:
        for fam, btn in tab_btns.items():
            on = fam == active_family[0]
            btn.configure(bg="#1c3a5c" if on else "#16162a",
                          fg=C_BLUE if on else C_SECONDARY)

    def switch_family(fam: str) -> None:
        active_family[0] = fam
        _style_tabs()
        _build_cards()

    for fam in FAMILIES:
        b = tk.Button(tab_row, text=FAMILY_LABELS.get(fam, fam),
                      command=lambda f=fam: switch_family(f),
                      relief="flat", bd=0, padx=18, pady=6, cursor="hand2",
                      font=("Segoe UI", 10, "bold"),
                      activebackground="#2a4a70", activeforeground=C_PRIMARY)
        b.pack(side="left", padx=(0, 6))
        tab_btns[fam] = b
    _style_tabs()
    _build_cards()

    # ── 추천 버튼 ─────────────────────────────────────────────────────
    rec_option = select_option(recommended)
    tk.Frame(outer, bg="#2a2a40", height=1).pack(fill="x", pady=(14, 0))

    rec_btn_frame = tk.Frame(outer, bg="#122040", padx=1, pady=1)
    rec_btn_frame.pack(fill="x", pady=(8, 0))

    rec_sv = _sv(lambda: _s("rec_btn", name=rec_option.name, label=rec_option.label,
                             vram=hw.gpu_memory_gb))
    tk.Button(
        rec_btn_frame, textvariable=rec_sv,
        bg="#1c3a5c", fg=C_BLUE,
        activebackground="#2a4a70", activeforeground=C_PRIMARY,
        font=("Segoe UI", 10, "bold"),
        relief="flat", bd=0, pady=10, cursor="hand2",
        command=lambda: pick(recommended),
    ).pack(fill="x")

    rec_hint_sv = _sv(lambda: _s("rec_hint"))
    tk.Label(outer, textvariable=rec_hint_sv,
             bg=BG, fg=C_SECONDARY, font=("Segoe UI", 8)).pack(anchor="e", pady=(3, 0))

    # ── 창 중앙 배치 ──────────────────────────────────────────────────
    root.update_idletasks()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    ww, wh = root.winfo_width(), root.winfo_height()
    root.geometry(f"+{(sw - ww) // 2}+{(sh - wh) // 2}")
    root.mainloop()

    if selected["use_cuda"] is not None:
        os.environ["YOLO_USE_CUDA"] = selected["use_cuda"]
    return selected["stem"]


def choose_with_console(hw: HardwareInfo, recommended: str) -> str:
    # ANSI 색상
    R  = "\033[0m"
    B  = "\033[1m"
    GR = "\033[92m"
    YL = "\033[93m"
    BL = "\033[94m"
    CY = "\033[96m"
    GY = "\033[90m"
    OR = "\033[33m"
    RD = "\033[91m"

    W = 58
    print(f"\n{B}{'─' * W}{R}")
    print(f"{B}  YOLO Model Setup{R}")
    print(f"{'─' * W}")
    print(f"  CPU   {hw.cpu_cores}코어   RAM  {hw.ram_gb:.0f} GB")
    print(f"  GPU   {B}{hw.gpu_name}{R} ({hw.gpu_memory_gb:.1f} GB VRAM)")
    if hw.tensorrt_available:
        trt_str = f"{GR}TensorRT ✓ 가속 가능{R}"
    elif hw.cuda_available:
        trt_str = f"{YL}TensorRT 미설치 — 선택 시 자동 설치{R}"
    else:
        trt_str = f"{RD}TensorRT 불가 (GPU 없음) — CPU 모드{R}"
    print(f"  {trt_str}")
    print(f"{'─' * W}\n")

    for idx, option in enumerate(MODEL_OPTIONS, 1):
        has_engine = option.engine_path.exists()
        has_pt     = option.pt_path.exists()
        is_rec     = option.stem == recommended

        if has_engine:
            status = f"{GR}✓ ENGINE READY{R}"
        elif has_pt:
            status = f"{YL}✓ WEIGHTS READY{R}"
        else:
            status = f"{OR}⬇ ~{option.size_mb} MB{R}"

        rec_tag = f"  {BL}★ 추천{R}" if is_rec else ""
        print(f"  {B}[{idx}]  {option.name}{R}  —  {option.label}{rec_tag}    {status}")
        print(f"        {GY}{option.note}{R}")
        print(f"        {CY}{_fps_line(option, hw)}{R}")
        setup_text, setup_hint = _setup_line(option, hw)
        hint_c = GR if setup_hint == "green" else (YL if setup_hint == "yellow" else OR)
        print(f"        {hint_c}{setup_text}{R}")
        print()

    print(f"{'─' * W}")
    rec_option = select_option(recommended)
    print(f"  {BL}{B}⭐ GPU({hw.gpu_memory_gb:.1f} GB) 기준 추천 모델: {rec_option.name}{R}")
    print(f"{'─' * W}\n")

    if hw.cuda_available:
        ans = input("  CUDA/TensorRT 가속 사용? [Y/n]: ").strip().lower()
        os.environ["YOLO_USE_CUDA"] = "false" if ans == "n" else "true"
    else:
        os.environ["YOLO_USE_CUDA"] = "false"

    choice = input(f"\n  선택 [1-{len(MODEL_OPTIONS)}]  또는 Enter (추천: {recommended}): ").strip()
    if not choice:
        return recommended
    if choice.isdigit():
        idx = int(choice) - 1
        if 0 <= idx < len(MODEL_OPTIONS):
            return MODEL_OPTIONS[idx].stem
    stems = {o.stem for o in MODEL_OPTIONS}
    return choice.lower() if choice.lower() in stems else recommended


def recommend_stem(hw: HardwareInfo) -> str:
    """GPU 기준 변형 추천 + 기본 패밀리(yolo26) → 선택 토큰 stem (예: yolo26m)."""
    return f"{DEFAULT_FAMILY}{recommend_variant(hw)}"


def select_option(token: str) -> ModelOption:
    """stem(예: 'yolo26m') 우선 매칭, variant letter만 주면 기본 패밀리 우선."""
    for option in MODEL_OPTIONS:
        if option.stem == token:
            return option
    for option in MODEL_OPTIONS:
        if option.variant == token and option.family == DEFAULT_FAMILY:
            return option
    for option in MODEL_OPTIONS:
        if option.variant == token:
            return option
    raise ValueError(f"Unknown YOLO model: {token}")


def download_weights(option: ModelOption) -> None:
    if option.pt_path.exists():
        return

    print(f"Downloading {option.stem}.pt...")
    from ultralytics import YOLO

    model = YOLO(f"{option.stem}.pt")
    downloaded = Path.cwd() / f"{option.stem}.pt"
    if downloaded.exists() and downloaded.resolve() != option.pt_path.resolve():
        shutil.move(str(downloaded), str(option.pt_path))
    model_path = getattr(model, "ckpt_path", "")
    if model_path:
        downloaded = Path(model_path)
        if downloaded.exists() and downloaded.resolve() != option.pt_path.resolve():
            shutil.copy2(str(downloaded), str(option.pt_path))
    if not option.pt_path.exists():
        raise FileNotFoundError(f"Failed to prepare {option.pt_path.name}")


def ensure_tensorrt(hw: HardwareInfo) -> bool:
    if os.getenv("YOLO_USE_CUDA", "true").lower() == "false":
        return False
    if not hw.cuda_available:
        return False
    if importlib.util.find_spec("tensorrt") is not None:
        return True

    print("TensorRT Python package is missing; trying to install it...")
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "tensorrt", "-q"]
        )
    except Exception as exc:
        print(f"TensorRT install failed: {exc}")
        return False

    importlib.invalidate_caches()
    return importlib.util.find_spec("tensorrt") is not None


def export_engine(option: ModelOption, hw: HardwareInfo) -> Path:
    if option.engine_path.exists():
        print(f"Using existing TensorRT engine: {option.engine_path.name}")
        return option.engine_path

    download_weights(option)

    if not ensure_tensorrt(hw):
        print("TensorRT is not available; using PyTorch weights.")
        return option.pt_path

    print(f"Building TensorRT engine from {option.pt_path.name}...")
    from ultralytics import YOLO

    model = YOLO(str(option.pt_path), task="detect")
    exported = model.export(
        format="engine",
        half=True,
        device=0,
        imgsz=IMG_SIZE,
        verbose=False,
    )
    exported_path = Path(str(exported))
    if not exported_path.is_absolute():
        exported_path = (Path.cwd() / exported_path).resolve()
    if exported_path.exists() and exported_path.resolve() != option.engine_path.resolve():
        shutil.move(str(exported_path), str(option.engine_path))

    if option.engine_path.exists():
        return option.engine_path
    return option.pt_path


def write_choice(model_path: Path) -> None:
    CHOICE_FILE.write_text(model_path.name + "\n", encoding="utf-8")


def _auto_tracker_tier(hw: HardwareInfo) -> str:
    if not hw.cuda_available:
        return "cpu"
    if hw.gpu_memory_gb >= 10:
        return "high"
    if hw.gpu_memory_gb >= 6:
        return "medium"
    if hw.gpu_memory_gb >= 4:
        return "low"
    return "cpu"


_TRACKER_NAMES = {
    "cpu":    "ByteTrack  (빠름, ReID 없음)",
    "low":    "OcSort     (가림 강함, ReID 없음)",
    "medium": "BotSort    (ReID 포함, 6 GB+ VRAM)",
    "high":   "DeepOcSort (ReID 포함, 8 GB+ VRAM)",
}


def write_profile(option: ModelOption, model_path: Path, hw: HardwareInfo) -> None:
    profile = dict(PROFILE_SETTINGS[option.profile])
    tracker_tier = os.getenv("TRACKER_TIER", "auto").strip().lower()
    if tracker_tier == "auto":
        tracker_tier = _auto_tracker_tier(hw)

    # inference backend
    if model_path.suffix == ".engine":
        inference_backend = "tensorrt"
    elif model_path.suffix == ".onnx":
        inference_backend = "onnx"
    else:
        inference_backend = "pytorch"

    profile.update(
        {
            "model": model_path.name,
            "variant": option.variant,
            "family": option.family,
            "cuda_enabled": os.getenv("YOLO_USE_CUDA", "true").lower() == "true"
            and hw.cuda_available,
            "tensorrt_enabled": model_path.suffix == ".engine",
            "inference_backend": inference_backend,
            "tracker_tier": tracker_tier,
        }
    )
    PROFILE_FILE.write_text(json.dumps(profile, indent=2), encoding="utf-8")


def main() -> int:
    hw = detect_hardware()
    recommended = recommend_stem(hw)
    selected = choose_with_gui(hw, recommended) or choose_with_console(hw, recommended)
    option = select_option(selected)
    model_path = export_engine(option, hw)
    write_choice(model_path)
    write_profile(option, model_path, hw)
    print(f"Selected YOLO model: {model_path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
