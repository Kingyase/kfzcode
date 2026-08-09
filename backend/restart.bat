@echo off
call "%~dp0stop.bat"
title KFZCode-Backend
python "%~dp0run.py" start
pause
