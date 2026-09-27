@echo off
chcp 65001 >nul
title 多账号管理系统 - 前端生产构建
cd /d "%~dp0web"
call pnpm run build
echo 构建完成：web/dist 由后端 (start_server.bat) 直接托管
pause
