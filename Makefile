SHELL = cmd.exe
.SHELLFLAGS = /c

PYTHON_RUNNER = scripts\with_best_python.cmd
BACKEND_PYTHON_RUNNER = ..\..\scripts\with_best_python.cmd

.PHONY: dev kill backend frontend help

dev: kill
	call $(PYTHON_RUNNER) traffic-digital-twin\backend\model_setup.py
	start "Backend  :8000" /d "%CD%\traffic-digital-twin\backend" cmd /k "call $(BACKEND_PYTHON_RUNNER) main.py"
	start "Frontend :5173" /d "%CD%\traffic-digital-twin\frontend" cmd /k npm run dev

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
