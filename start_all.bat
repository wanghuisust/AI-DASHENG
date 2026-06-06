@echo off
chcp 65001 >nul 2>&1
title AI-DASHENG All-in-One

echo ==========================================
echo   AI-DASHENG 启动中...
echo ==========================================
echo.

cd /d G:\AI-DASHENG

echo [1/2] 启动 Web 服务 (端口 7860)...
start "DASHENG-Web" .venv\Scripts\python.exe src\web_server.py

timeout /t 2 /nobreak >nul

echo [2/2] 启动 Gateway (微信iLink + QQ OneBot)...
echo.
echo   微信：iLink Bot API — 首次运行需微信扫码
echo   QQ：  OneBot v11 正向WS
echo.
.venv\Scripts\python.exe -m gateway

pause
