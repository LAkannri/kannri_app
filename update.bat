@echo off
chcp 932 > nul
cd /d "%~dp0"
title EnkanAI Update

REM ============================================================
REM  EnkanAI wo saishin ni suru (git pull + hitsuyou nara install)
REM  Apuri wo kaizen suru tabi ni, kono file wo double click suru dake.
REM ============================================================

echo.
echo ================================================
echo    EnkanAI wo saishin ni shimasu
echo ================================================
echo.

REM ----- git check -----
git --version > nul 2>&1
if errorlevel 1 (
    echo [ERROR] git ga haitte imasen.
    echo.
    echo Git wo install suru ka, GitHub kara ZIP wo dl shite
    echo folder wo joushogaki shite kudasai.
    echo   https://git-scm.com/download/win
    echo.
    pause
    exit /b
)

REM ----- .git check (ZIP de tenkai shita baai) -----
if not exist ".git" (
    echo [ERROR] Kono folder wa git de kanri sarete imasen.
    echo ZIP de tenkai shita baai wa, ZIP wo dl shinaoshite kudasai.
    echo.
    pause
    exit /b
)

echo [1/3] Saishin wo torikomi masu...
git pull
if errorlevel 1 (
    echo.
    echo [ERROR] git pull ni shippai shimashita.
    echo Local de file wo henkou shite iru kanousei ga arimasu.
    echo.
    pause
    exit /b
)
echo.

echo [2/3] Hitsuyou na buhin wo kakunin...
python -m pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo [WARN] install de mondai ga arimashita. Tsuzukemasu.
)
echo.

echo [3/3] Kanryou!
echo.
echo ================================================
echo  Saishin ni narimashita.
echo  start.bat kara apuri wo kidou shite kudasai.
echo ================================================
echo.
pause
