# Traffic Digital Twin — 빠른 시작
#
# 사전 조건:
#   - Python 3.11+ (권장: 3.13)
#   - Node.js 18+
#   - NVIDIA GPU + CUDA 드라이버 (TensorRT 사용 시)
#
# 사용법:
#   make dev   설치 확인 후 백엔드 + 프론트엔드 동시 실행 (전부 자동)

PYTHON  = python
VENV    = ../.venv
PIP     = $(VENV)/Scripts/pip
PY      = $(VENV)/Scripts/python
UVICORN = $(VENV)/Scripts/uvicorn
ENGINE  = backend/yolov8x.engine

.PHONY: dev _setup-venv _setup-torch _setup-pip _setup-engine _setup-npm

# ── 메인 진입점 ──────────────────────────────────────────────────────
dev: _setup-venv _setup-torch _setup-pip _setup-engine _setup-npm
	@echo ""
	@echo "모든 설치 완료. 서버를 시작합니다..."
	@echo ""
	start "Backend" cmd /k "cd backend && $(UVICORN) main:app --host 0.0.0.0 --port 8000 --reload"
	cd frontend && npm run dev

# ── 가상환경 (없을 때만 생성) ────────────────────────────────────────
_setup-venv:
	@if not exist "$(VENV)/Scripts/pip.exe" ( \
		echo [1/5] 가상환경 생성 중... && \
		$(PYTHON) -m venv $(VENV) \
	) else ( \
		echo [1/5] 가상환경 확인 완료 \
	)

# ── CUDA PyTorch (CPU 빌드이면 재설치) ──────────────────────────────
_setup-torch:
	@$(PY) -c "import torch; assert '+cu' in torch.__version__, 'cpu build'" 2>nul && \
		echo [2/5] CUDA PyTorch 확인 완료 || ( \
		echo [2/5] CUDA PyTorch 설치 중... && \
		$(PIP) install torch torchvision --index-url https://download.pytorch.org/whl/cu124 -q || \
		$(PIP) install torch torchvision -q \
	)

# ── 백엔드 패키지 ────────────────────────────────────────────────────
_setup-pip:
	@echo [3/5] 백엔드 패키지 설치 중...
	@$(PIP) install -r backend/requirements.txt -q

# ── TensorRT 엔진 (없을 때만 변환, 3~5분) ───────────────────────────
_setup-engine:
	@if not exist "$(ENGINE)" ( \
		echo [4/5] TensorRT 엔진 생성 중 최초 1회 3~5분 소요... && \
		$(PY) -c "from ultralytics import YOLO; import shutil,os; m=YOLO('yolov8x.pt'); m.export(format='engine',half=True,device=0,imgsz=640); shutil.move('yolov8x.engine','backend/yolov8x.engine') if os.path.exists('yolov8x.engine') else None; print('엔진 생성 완료')" \
	) else ( \
		echo [4/5] TensorRT 엔진 확인 완료 \
	)

# ── 프론트엔드 패키지 ────────────────────────────────────────────────
_setup-npm:
	@if not exist "frontend/node_modules" ( \
		echo [5/5] 프론트엔드 패키지 설치 중... && \
		cd frontend && npm install -q \
	) else ( \
		echo [5/5] 프론트엔드 패키지 확인 완료 \
	)
