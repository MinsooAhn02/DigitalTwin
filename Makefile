SHELL = cmd.exe
.SHELLFLAGS = /c

.PHONY: dev kill backend frontend help

# 기본 타겟
dev: kill
	start "Backend  :8000" /d "%CD%\traffic-digital-twin\backend" cmd /k python main.py
	start "Frontend :5173" /d "%CD%\traffic-digital-twin\frontend" cmd /k npm run dev

# 포트 8000 점유 프로세스 종료
kill:
	-powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $$_.OwningProcess -Force -ErrorAction SilentlyContinue }"

# 백엔드만
backend:
	cd traffic-digital-twin\backend && python main.py

# 프론트엔드만
frontend:
	cd traffic-digital-twin\frontend && npm run dev

help:
	@echo.
	@echo   make dev       백엔드 + 프론트엔드 동시 실행 (포트 자동 정리)
	@echo   make backend   백엔드만 실행
	@echo   make frontend  프론트엔드만 실행
	@echo   make kill      포트 8000 강제 해제
	@echo.
