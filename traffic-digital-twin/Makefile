# Traffic Digital Twin — 빠른 시작
#
# 사전 조건:
#   - Python 3.13 이상  (py --version 으로 확인)
#   - Node.js 18 이상
#   - NVIDIA GPU + CUDA 드라이버 (TensorRT 사용 시)
#   - Git Bash 또는 GNU make 환경
#
# 사용법:
#   make dev   설치 확인 후 백엔드 + 프론트엔드 동시 실행 (전부 자동)

# Python 3.13 우선 탐색 (py launcher → python3.13 → python 순)
PYTHON  := $(shell py -3.13 -c "import sys; print(sys.executable)" 2>nul || \
           python3.13 -c "import sys; print(sys.executable)" 2>/dev/null || \
           python -c "import sys; print(sys.executable)" 2>/dev/null)
VENV    = ../.venv
PIP     = $(VENV)/Scripts/pip
PY      = $(VENV)/Scripts/python
UVICORN = $(VENV)/Scripts/uvicorn
ENGINE  = backend/yolov8s.engine

.PHONY: dev _check-python _setup-venv _setup-torch _setup-pip _setup-nodelink _setup-engine _setup-npm

# ── 메인 진입점 ──────────────────────────────────────────────────────
dev: _check-python _setup-venv _setup-torch _setup-pip _setup-nodelink _setup-engine _setup-npm
	@echo ""
	@echo "모든 설치 완료. 서버를 시작합니다..."
	@echo ""
	start "Backend" cmd /k "cd backend && $(UVICORN) main:app --host 0.0.0.0 --port 8000 --reload"
	cd frontend && npm run dev

# ── Python 버전 체크 ─────────────────────────────────────────────────
_check-python:
	@$(PYTHON) -c "import sys; v=sys.version_info; assert (v.major,v.minor)>=(3,13), f'Python 3.13+ 필요, 현재 {v.major}.{v.minor}'" \
		&& echo [OK] Python $(shell $(PYTHON) --version) \
		|| (echo "[ERROR] Python 3.13 이상이 필요합니다. https://python.org 에서 설치하세요." && exit 1)

# ── 가상환경 (없을 때만 생성) ────────────────────────────────────────
_setup-venv:
	@if [ ! -f "$(VENV)/Scripts/pip.exe" ]; then \
		echo "[1/5] 가상환경 생성 중..." && \
		$(PYTHON) -m venv $(VENV); \
	else \
		echo "[1/5] 가상환경 확인 완료"; \
	fi

# ── CUDA PyTorch (CPU 빌드이면 재설치) ──────────────────────────────
_setup-torch:
	@$(PY) -c "import torch; assert '+cu' in torch.__version__, 'cpu build'" 2>nul && \
		echo [2/5] CUDA PyTorch 확인 완료 || ( \
		echo [2/5] CUDA PyTorch 설치 중 GPU 없으면 CPU 버전으로 대체... && \
		$(PIP) install torch torchvision --index-url https://download.pytorch.org/whl/cu124 -q || \
		$(PIP) install torch torchvision -q \
	)

# ── 백엔드 패키지 ────────────────────────────────────────────────────
_setup-pip:
	@echo [3/5] 백엔드 패키지 설치 중...
	@$(PIP) install -r backend/requirements.txt -q
	@$(PIP) install tensorrt -q 2>nul || echo "  TensorRT 설치 생략 (GPU 없는 환경)"

# ── 노드링크 SQLite DB (없을 때만 변환, 최초 1회) ───────────────────
_setup-nodelink:
	@if [ ! -f "../node-link-data/nodelink.sqlite" ]; then \
		if [ -f "../node-link-data/MOCT_NODE.shp" ] && [ -f "../node-link-data/MOCT_LINK.shp" ]; then \
			echo "[3.5/5] 노드링크 DB 생성 중 (최초 1회 ~2분 소요)..." && \
			$(PIP) install pyshp pyproj -q && \
			$(PY) ../scripts/build_nodelink_db.py; \
		else \
			echo "[3.5/5] 노드링크 shapefile 없음 — 도로 정보 기능 비활성화 (node-link-data/ 에 MOCT_NODE.shp, MOCT_LINK.shp 추가 후 재실행)"; \
		fi \
	else \
		echo "[3.5/5] 노드링크 DB 확인 완료"; \
	fi

# ── YOLO 모델 설정 (없을 때만, 최초 1회) ────────────────────────────
# model_setup.py: 하드웨어 자동 감지 → 모델 선택 → 다운로드 → TensorRT 변환(가능 시)
# 결과: backend/.yolo_model, backend/.runtime_profile.json 생성
_setup-engine:
	@if [ ! -f "backend/.yolo_model" ]; then \
		echo "[4/5] YOLO 모델 설정 중 (최초 1회)..." && \
		$(PY) backend/model_setup.py; \
	else \
		echo "[4/5] YOLO 모델 확인 완료 ($$(cat backend/.yolo_model | tr -d '\n'))"; \
	fi

# ── 프론트엔드 패키지 ────────────────────────────────────────────────
_setup-npm:
	@if [ ! -d "frontend/node_modules" ]; then \
		echo "[5/5] 프론트엔드 패키지 설치 중..." && \
		cd frontend && npm install -q; \
	else \
		echo "[5/5] 프론트엔드 패키지 확인 완료"; \
	fi
