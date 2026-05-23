SHELL = cmd.exe
.SHELLFLAGS = /c

PYTHON_RUNNER = scripts\with_best_python.cmd
BACKEND_PYTHON_RUNNER = ..\..\scripts\with_best_python.cmd

.PHONY: dev kill backend frontend help

dev: kill nodelink
	call $(PYTHON_RUNNER) traffic-digital-twin\backend\model_setup.py
	start "Backend  :8000" /d "%CD%\traffic-digital-twin\backend" cmd /k "call $(BACKEND_PYTHON_RUNNER) main.py"
	start "Frontend :5173" /d "%CD%\traffic-digital-twin\frontend" cmd /k npm run dev

nodelink:
	@if not exist "node-link-data\nodelink.sqlite" ( \
		if exist "node-link-data\MOCT_NODE.shp" ( \
			echo [노드링크] DB 생성 중 (최초 1회 ~2분)... && \
			call $(PYTHON_RUNNER) -m pip install pyshp pyproj -q && \
			call $(PYTHON_RUNNER) scripts\build_nodelink_db.py \
		) else ( \
			echo [노드링크] shapefile 없음 - 도로 정보 기능 비활성화 \
		) \
	) else ( \
		echo [노드링크] DB 확인 완료 \
	)

kill:
	-powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $$_.OwningProcess -Force -ErrorAction SilentlyContinue }"

backend:
	call $(PYTHON_RUNNER) traffic-digital-twin\backend\model_setup.py
	cd traffic-digital-twin\backend && call $(BACKEND_PYTHON_RUNNER) main.py

frontend:
	cd traffic-digital-twin\frontend && npm run dev

help:
	@cmd /c echo.
	@cmd /c echo   make dev           backend + frontend with auto CUDA Python
	@cmd /c echo   make backend       backend only with auto CUDA Python
	@cmd /c echo   make frontend      frontend only
	@cmd /c echo   make kill          free port 8000
	@cmd /c echo.
