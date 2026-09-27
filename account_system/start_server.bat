@echo off
chcp 65001 >nul
title 霸王茶姬多账号管理系统 - 后端服务
cd /d "%~dp0server"
set PY=..\..\.venv_verify\Scripts\python.exe
if not exist "%PY%" set PY=python
echo [启动] 后端 API + 前端静态托管  http://127.0.0.1:8000
echo 默认管理员：admin / Admin@123
"%PY%" -m uvicorn app:app --host 127.0.0.1 --port 8000
pause
