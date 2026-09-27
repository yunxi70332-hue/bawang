@echo off
chcp 65001 >nul
title 多账号管理系统 - 前端开发模式（Vite）
cd /d "%~dp0web"
echo [启动] 前端开发服务器 http://localhost:5173 （/api 代理至 127.0.0.1:8000，需先启动后端）
call pnpm run dev
pause
