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

REM ----- git check (nakereba sono ba de install suru) -----
git --version > nul 2>&1
if not errorlevel 1 goto :git_ok

echo [1/3] Git ga haitte imasen. Ima kara install shimasu...
echo.
winget --version > nul 2>&1
if errorlevel 1 goto :git_manual

echo  ******************************************************
echo  * Kono ato "kono apuri ga henkou wo kanou ni suruka?" *
echo  * to kikare masu. Kanarazu [Hai] wo oshite kudasai.   *
echo  ******************************************************
echo.
winget install --id Git.Git -e --source winget --accept-package-agreements --accept-source-agreements

REM winget de ireta chokugo wa PATH ga furui node, chokusetsu path mo tameru
set "PATH=%PATH%;%ProgramFiles%\Git\cmd;%LOCALAPPDATA%\Programs\Git\cmd"
git --version > nul 2>&1
if not errorlevel 1 (
    echo.
    echo [OK] Git ga haitte, sugu tsukae masu.
    echo.
    goto :git_ok
)

if exist "%ProgramFiles%\Git\cmd\git.exe" goto :git_reopen
if exist "%LOCALAPPDATA%\Programs\Git\cmd\git.exe" goto :git_reopen

REM Kanri-sha no kyoka ga tsukaenai PC muke: jibun no user dake ni ireru
echo.
echo  Kanri-sha no kyoka ga tsukae nakatta you desu.
echo  Jibun no user dake ni ireru houhou de yari naoshimasu (kyoka fuyou).
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\install-git-user.ps1"
set "PATH=%PATH%;%LOCALAPPDATA%\Programs\Git\cmd;%LOCALAPPDATA%\Programs\PortableGit\cmd"
git --version > nul 2>&1
if not errorlevel 1 (
    echo.
    echo  [OK] Git ga haitte, sugu tsukae masu.
    echo.
    goto :git_ok
)
if exist "%LOCALAPPDATA%\Programs\Git\cmd\git.exe" goto :git_reopen
if exist "%LOCALAPPDATA%\Programs\PortableGit\cmd\git.exe" goto :git_reopen
goto :git_manual

:git_reopen
echo.
echo [OK] Git wo iremashita.
echo      Kono mado wo tojite, mou ichido update.bat wo double click shite kudasai.
pause
exit /b

:git_manual
echo.
echo [ERROR] Git wo ireru koto ga deki masen deshita.
echo.
echo  Genin to shite ooi no wa, tsugi no 2tsu desu:
echo   1. "Hai" (kanri-sha no kyoka) wo oshite inai
echo      -^> kono file wo mou ichido jikkou shite, [Hai] wo oshite kudasai.
echo   2. Kaisha no PC de install ga kinshi sarete iru
echo      -^> browser kara install suru ka, ZIP de tsukatte kudasai.
echo.
echo  Browser wo hirakimasu. Install go, mou ichido kono file wo jikkou shite kudasai.
start "" "https://git-scm.com/download/win"
pause
exit /b

:git_ok

REM ----- .git check (ZIP de tenkai shita baai wa, koko de git ni kirikaeru) -----
if not exist ".git" (
    echo [1/3] Kono folder wo git ni kirikae masu ^(ZIP de tenkai shita folder^)...
    git init
    git remote add origin https://github.com/LAkannri/kannri_app.git
    git fetch origin main
    if errorlevel 1 (
        echo.
        echo [ERROR] GitHub kara torikome masen deshita.
        echo Login gamen ga deta baai wa, kaisha no account de login shite kudasai.
        pause
        exit /b
    )
    REM ZIP no naka mi wa GitHub to onaji hazu nanode, saishin ni awaseru
    git reset --hard origin/main
    git branch -M main
    git branch --set-upstream-to=origin/main main
    echo [OK] Kongo wa kono file dake de saishin ni deki masu.
    echo.
)

REM ------------------------------------------------------------
REM  Onaji gamen no file ga 2tsu nokotte iru to Streamlit ga kidou shinai.
REM  ^(ZIP de joushogaki suru to, kesareta file ga nokoru tame^)
REM  Nihongo no filename wa batch dato kowareru node, python ni makaseru.
REM ------------------------------------------------------------
python cleanup_pages.py 2>nul

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
