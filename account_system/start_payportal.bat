@echo off
rem H5 收银台独立入口（0.0.0.0:8010，仅 /pay/* 公开路由；管理 API 仍在 127.0.0.1:8000）
cd /d %~dp0server
..\..\.venv_verify\Scripts\python.exe -m uvicorn pay_portal:app --host 0.0.0.0 --port 8010
