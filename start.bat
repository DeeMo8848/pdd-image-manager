@echo off
chcp 65001 >nul
title 图片管理工具 v2
cd /d "%~dp0"
start http://localhost:8901
pip install -q -r requirements.txt 2>nul
python main.py
pause
