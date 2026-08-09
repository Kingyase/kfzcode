@echo off
REM 关闭旧窗口 + 清端口
for /f "tokens=2" %%a in ('tasklist /v /fi "WINDOWTITLE eq KFZCode-Backend" /fo list 2^>nul ^| findstr "PID:"') do (
    taskkill /F /T /PID %%a >nul 2>&1
)
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8765.*LISTENING"') do (
    taskkill /F /T /PID %%a >nul 2>&1
)

title KFZCode-Backend
python "%~dp0run.py" start
pause
