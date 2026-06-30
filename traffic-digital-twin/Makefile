SHELL = cmd.exe
.SHELLFLAGS = /c

VENV     = ..\.venv
PIP_EXE  = $(VENV)\Scripts\pip.exe
PY_EXE   = $(VENV)\Scripts\python.exe
UVICORN  = $(VENV)\Scripts\uvicorn.exe

.PHONY: dev report bench _check-python _setup-venv _setup-torch _setup-pip _setup-nodelink _setup-engine _setup-npm

# bench 기본값 (덮어쓰기: make bench NAME="국도3호선 성남 대원IC" SECONDS=60 MODE=both)
NAME    ?=
SECONDS ?= 60
MODE    ?= both

# ── 메인 진입점 ──────────────────────────────────────────────────────
dev: _check-python _setup-venv _setup-torch _setup-pip _setup-nodelink _setup-engine _setup-npm
	@echo.
	@echo 모든 설치 완료. 서버를 시작합니다...
	@echo.
	start "Backend" cmd /k "cd /d "%CD%\backend" && "$(UVICORN)" main:app --host 0.0.0.0 --port 8000 --reload"
	cd frontend && npm run dev

# ── 리포트 PDF 빌드 ──────────────────────────────────────────────────
report:
	@if not exist "..\report\img" mkdir "..\report\img"
	cd ..\report && for %%f in (report_base report_contribution) do (echo [report] building %%f.pdf ... && pdflatex -interaction=nonstopmode -halt-on-error %%f.tex >nul && bibtex %%f >nul && pdflatex -interaction=nonstopmode %%f.tex >nul && pdflatex -interaction=nonstopmode %%f.tex >nul || echo [report] FAILED: %%f -- pdflatex 가 PATH에 있는지 확인하세요.)
	@echo [report] done: ..\report\report_base.pdf, ..\report\report_contribution.pdf

# ── 벤치마크: 카메라 이름으로 backend×tracker 비교 측정 ────────────────
# 사용법: make bench NAME="국도3호선 성남 대원IC" [SECONDS=60] [MODE=both|latency|tracker]
# 결과: backend\logs\bench\ 에 CSV/JSON + 콘솔에 Markdown/LaTeX 표.
# 서버(make dev) 없이도 동작 — ITS API로 카메라를 직접 해석합니다.
bench:
	@if "$(NAME)"=="" (echo 사용법: make bench NAME="카메라이름" [SECONDS=60] [MODE=both^|latency^|tracker] & exit /b 1)
	"$(PY_EXE)" backend\benchmark.py --name "$(NAME)" --seconds $(SECONDS) --mode $(MODE)

# ── Python 3.13 체크 ─────────────────────────────────────────────────
_check-python:
	@py -3.13 -c "import sys" 2>nul && echo [OK] Python 3.13 확인 완료 || (echo [ERROR] Python 3.13 이상이 필요합니다. https://python.org 에서 설치하세요. && exit 1)

# ── 가상환경 (없을 때만 생성) ────────────────────────────────────────
_setup-venv:
	@if not exist "$(VENV)\Scripts\pip.exe" ( \
		echo [1/5] 가상환경 생성 중... && \
		py -3.13 -m venv "$(VENV)" \
	) else ( \
		echo [1/5] 가상환경 확인 완료 \
	)

# ── CUDA PyTorch (CPU 빌드이면 재설치) ──────────────────────────────
_setup-torch:
	@"$(PY_EXE)" -c "import torch; assert '+cu' in torch.__version__" 2>nul && echo [2/5] CUDA PyTorch 확인 완료 || (echo [2/5] CUDA PyTorch 설치 중... && "$(PIP_EXE)" install torch torchvision --index-url https://download.pytorch.org/whl/cu124 -q || "$(PIP_EXE)" install torch torchvision -q)

# ── 백엔드 패키지 ────────────────────────────────────────────────────
_setup-pip:
	@echo [3/5] 백엔드 패키지 설치 중...
	@"$(PIP_EXE)" install -r backend\requirements.txt -q
	@"$(PIP_EXE)" install tensorrt -q 2>nul || echo   TensorRT 설치 생략 ^(GPU 없는 환경^)

# ── 노드링크 SQLite DB (없을 때만 변환, 최초 1회) ───────────────────
_setup-nodelink:
	@if not exist "..\node-link-data\nodelink.sqlite" ( \
		if exist "..\node-link-data\MOCT_NODE.shp" ( \
			echo [3.5/5] 노드링크 DB 생성 중 ^(최초 1회 ~2분 소요^)... && \
			"$(PIP_EXE)" install pyshp pyproj -q && \
			"$(PY_EXE)" ..\scripts\build_nodelink_db.py \
		) else ( \
			echo [3.5/5] 노드링크 shapefile 없음 -- 도로 정보 기능 비활성화 \
		) \
	) else ( \
		echo [3.5/5] 노드링크 DB 확인 완료 \
	)

# ── YOLO 모델 설정 (없을 때만, 최초 1회) ────────────────────────────
_setup-engine:
	@if not exist "backend\.yolo_model" ( \
		echo [4/5] YOLO 모델 설정 중 ^(최초 1회^)... && \
		"$(PY_EXE)" backend\model_setup.py \
	) else ( \
		echo [4/5] YOLO 모델 확인 완료 \
	)

# ── 프론트엔드 패키지 ────────────────────────────────────────────────
_setup-npm:
	@if not exist "frontend\node_modules" ( \
		echo [5/5] 프론트엔드 패키지 설치 중... && \
		cd frontend && npm install -q \
	) else ( \
		echo [5/5] 프론트엔드 패키지 확인 완료 \
	)
