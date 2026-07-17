@echo off
chcp 932 > nul
cd /d "%~dp0"
title EnkanAI

echo.
echo ================================================
echo    EnkanAI wo kidou shimasu
echo ================================================
echo.

REM ----- First-run: open manual -----
if not exist ".setup_done" (
    echo [Hajimete no goriyou] Manual wo hirakimasu...
    if exist "manual.html" (
        start "" "manual.html"
    )
    timeout /t 2 > nul
    echo.
)

REM ----- Python check -----
echo [1/4] Python check...
python --version
if errorlevel 1 (
    echo.
    echo [ERROR] Python ga mitsukarimasen.
    pause
    exit /b
)
echo.

REM ----- First-time setup -----
if not exist ".setup_done" (
    echo [2/4] Shokai setup chuu...
    
    python -m pip install --upgrade pip
    if errorlevel 1 (
        echo [ERROR] pip update ni shippai.
        pause
        exit /b
    )
    
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] requirements install ni shippai.
        pause
        exit /b
    )
    
    python -m playwright install chromium
    if errorlevel 1 (
        echo [ERROR] playwright install ni shippai.
        pause
        exit /b
    )
    
    echo done > .setup_done
    echo [OK] Setup kanryou!
    echo.
) else (
    echo [2/4] Setup skip.
    echo.
)

REM ----- secrets.toml check -----
echo [3/4] secrets.toml check...
if not exist ".streamlit\secrets.toml" (
    echo [ERROR] secrets.toml ga arimasen.
    pause
    exit /b
)
echo OK
echo.

REM ----- Launch Streamlit -----
echo [4/4] Streamlit kidou chuu...
echo.
echo ================================================
echo CAUTION: Kono mado wo tojinaide kudasai!
echo ================================================
echo.

python -m streamlit run app.py

echo.
pause