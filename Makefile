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
	@call scripts\check_nodelink.cmd

kill:
	-for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8000 " ^| findstr "LISTENING"') do taskkill /F /PID %%a

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
