@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo.
echo  接続キーのファイル（secrets.toml）を点検します。
echo  ※ キーの中身は表示しません。
echo.
python secrets_check.py
if errorlevel 1 (
  echo.
  echo  python が見つかりませんでした。start.bat を一度実行してから、もう一度お試しください。
)
echo.
pause
