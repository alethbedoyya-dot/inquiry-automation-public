@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ================================
echo   询价单自动化
echo ================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Python，请联系 IT 安装
    pause
    exit /b 1
)
echo [OK] Python 已就绪

echo.
echo 正在检查必要组件（首次运行需等待约30秒）...
pip install -r requirements.txt -q
echo [OK] 组件就绪

echo.
echo 启动程序...
echo.
python launcher.py
pause
