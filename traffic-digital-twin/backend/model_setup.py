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


@dataclass(frozen=True)
class ModelOption:
    variant: str
    name: str
    label: str
    note: str
    min_gpu_gb: float
    profile: str

    @property
    def stem(self) -> str:
        return f"yolov8{self.variant}"

    @property
    def pt_path(self) -> Path:
        return BACKEND_DIR / f"{self.stem}.pt"

    @property
    def engine_path(self) -> Path:
        return BACKEND_DIR / f"{self.stem}.engine"


MODEL_OPTIONS = [
    ModelOption("n", "YOLOv8n", "Fastest", "Low spec / quick demo", 0.0, "fast"),
    ModelOption("s", "YOLOv8s", "Balanced", "Recommended for mid GPUs", 4.0, "balanced"),
    ModelOption("m", "YOLOv8m", "Accurate", "Good accuracy with enough VRAM", 6.0, "quality"),
    ModelOption("x", "YOLOv8x", "Best accuracy", "High-end GPU / best detection", 8.0, "quality"),
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


def _button_text(option: ModelOption, recommended: str) -> str:
    prefix = "[Recommended] " if option.variant == recommended else ""
    return (
        f"{prefix}{option.name} ({option.label})\n"
        f"{option.note}\n"
        f"{option_status(option)}"
    )


def choose_with_gui(hw: HardwareInfo, recommended: str) -> str | None:
    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception:
        return None

    selected: dict[str, str | None] = {"variant": None, "use_cuda": None}
    root = tk.Tk()
    root.title("YOLO Model Setup")
    root.resizable(False, False)

    frame = ttk.Frame(root, padding=18)
    frame.grid(row=0, column=0, sticky="nsew")

    title = ttk.Label(frame, text="Choose YOLO model", font=("", 14, "bold"))
    title.grid(row=0, column=0, sticky="w", pady=(0, 8))

    if hw.tensorrt_available:
        trt = "available"
    elif hw.cuda_available:
        trt = "not installed; setup will try to install it"
    else:
        trt = "not available"
    summary = (
        f"CPU: {hw.cpu_cores} cores, RAM: {hw.ram_gb:.1f} GB\n"
        f"GPU: {hw.gpu_name} ({hw.gpu_memory_gb:.1f} GB), TensorRT: {trt}"
    )
    ttk.Label(frame, text=summary).grid(row=1, column=0, sticky="w", pady=(0, 14))
    use_cuda = tk.BooleanVar(value=hw.cuda_available)
    cuda_check = ttk.Checkbutton(
        frame,
        text="Use CUDA/TensorRT acceleration when available",
        variable=use_cuda,
        state="normal" if hw.cuda_available else "disabled",
    )
    cuda_check.grid(row=2, column=0, sticky="w", pady=(0, 10))

    def pick(variant: str) -> None:
        selected["variant"] = variant
        selected["use_cuda"] = "true" if use_cuda.get() else "false"
        root.destroy()

    for index, option in enumerate(MODEL_OPTIONS, start=3):
        button = ttk.Button(
            frame,
            text=_button_text(option, recommended),
            command=lambda value=option.variant: pick(value),
            width=54,
        )
        button.grid(row=index, column=0, sticky="ew", pady=4)

    ttk.Button(
        frame,
        text=f"Use recommended ({recommended})",
        command=lambda: pick(recommended),
    ).grid(row=7, column=0, sticky="ew", pady=(12, 0))

    root.update_idletasks()
    x = (root.winfo_screenwidth() - root.winfo_width()) // 2
    y = (root.winfo_screenheight() - root.winfo_height()) // 2
    root.geometry(f"+{x}+{y}")
    root.mainloop()
    if selected["use_cuda"] is not None:
        os.environ["YOLO_USE_CUDA"] = selected["use_cuda"]
    return selected["variant"]


def choose_with_console(hw: HardwareInfo, recommended: str) -> str:
    print("YOLO model setup")
    print(f"CPU: {hw.cpu_cores} cores, RAM: {hw.ram_gb:.1f} GB")
    print(f"GPU: {hw.gpu_name} ({hw.gpu_memory_gb:.1f} GB)")
    if hw.tensorrt_available:
        trt = "available"
    elif hw.cuda_available:
        trt = "not installed; setup will try to install it"
    else:
        trt = "not available"
    print(f"TensorRT: {trt}")
    if hw.cuda_available:
        use_cuda = input("Use CUDA/TensorRT acceleration? [Y/n]: ").strip().lower()
        os.environ["YOLO_USE_CUDA"] = "false" if use_cuda == "n" else "true"
    else:
        os.environ["YOLO_USE_CUDA"] = "false"
    print()
    for index, option in enumerate(MODEL_OPTIONS, start=1):
        marker = " recommended" if option.variant == recommended else ""
        print(
            f"{index}. {option.name} - {option.label}{marker} "
            f"[{option_status(option)}]"
        )

    choice = input(f"Select model [recommended {recommended}]: ").strip()
    if not choice:
        return recommended
    if choice.isdigit():
        index = int(choice) - 1
        if 0 <= index < len(MODEL_OPTIONS):
            return MODEL_OPTIONS[index].variant
    variants = {option.variant for option in MODEL_OPTIONS}
    return choice.lower() if choice.lower() in variants else recommended


def select_option(variant: str) -> ModelOption:
    for option in MODEL_OPTIONS:
        if option.variant == variant:
            return option
    raise ValueError(f"Unknown YOLO model variant: {variant}")


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


def write_profile(option: ModelOption, model_path: Path, hw: HardwareInfo) -> None:
    profile = dict(PROFILE_SETTINGS[option.profile])
    profile.update(
        {
            "model": model_path.name,
            "variant": option.variant,
            "cuda_enabled": os.getenv("YOLO_USE_CUDA", "true").lower() == "true"
            and hw.cuda_available,
            "tensorrt_enabled": model_path.suffix == ".engine",
        }
    )
    PROFILE_FILE.write_text(json.dumps(profile, indent=2), encoding="utf-8")


def main() -> int:
    hw = detect_hardware()
    recommended = recommend_variant(hw)
    selected = choose_with_gui(hw, recommended) or choose_with_console(hw, recommended)
    option = select_option(selected)
    model_path = export_engine(option, hw)
    write_choice(model_path)
    write_profile(option, model_path, hw)
    print(f"Selected YOLO model: {model_path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
