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

REM ----- Python check (nakereba sono ba de install suru) -----
echo [1/4] Python check...
python --version > nul 2>&1
if not errorlevel 1 goto :py_ok

echo  Python ga haitte imasen. Ima kara install shimasu...
echo.
winget --version > nul 2>&1
if errorlevel 1 goto :py_manual

echo  ******************************************************
echo  * Kono ato "kono apuri ga henkou wo kanou ni suruka?" *
echo  * to kikare masu. Kanarazu [Hai] wo oshite kudasai.   *
echo  ******************************************************
echo.
winget install --id Python.Python.3.12 -e --source winget --accept-package-agreements --accept-source-agreements

REM ireta chokugo wa PATH ga furui node, chokusetsu path mo tameru
set "PATH=%PATH%;%LOCALAPPDATA%\Programs\Python\Python312;%LOCALAPPDATA%\Programs\Python\Python312\Scripts"
python --version > nul 2>&1
if not errorlevel 1 (
    echo.
    echo  [OK] Python ga haitte, sugu tsukae masu.
    echo.
    goto :py_ok
)
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
    echo.
    echo  [OK] Python wo iremashita.
    echo       Kono mado wo tojite, mou ichido start.bat wo double click shite kudasai.
    pause
    exit /b
)

REM Kanri-sha no kyoka ga tsukaenai PC muke: jibun no user dake ni ireru
echo.
echo  Kanri-sha no kyoka ga tsukae nakatta you desu.
echo  Jibun no user dake ni ireru houhou de yari naoshimasu (kyoka fuyou).
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\install-python-user.ps1"
set "PATH=%PATH%;%LOCALAPPDATA%\Programs\Python\Python312;%LOCALAPPDATA%\Programs\Python\Python312\Scripts"
python --version > nul 2>&1
if not errorlevel 1 (
    echo.
    echo  [OK] Python ga haitte, sugu tsukae masu.
    echo.
    goto :py_ok
)
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
    echo.
    echo  [OK] Python wo iremashita.
    echo       Kono mado wo tojite, mou ichido start.bat wo double click shite kudasai.
    pause
    exit /b
)

:py_manual
echo.
echo  [ERROR] Python wo ireru koto ga deki masen deshita.
echo.
echo   Genin to shite ooi no wa, tsugi no 2tsu desu:
echo    1. "Hai" (kanri-sha no kyoka) wo oshite inai
echo       -^> kono file wo mou ichido jikkou shite, [Hai] wo oshite kudasai.
echo    2. Kaisha no PC de install ga kinshi sarete iru
echo       -^> browser kara install shite kudasai.
echo          *** "Add python.exe to PATH" ni kanarazu check wo irete kudasai ***
echo.
start "" "https://www.python.org/downloads/"
pause
exit /b

:py_ok
python --version
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
    REM Hitsuyou na buhin ga fuete iru koto ga aru node, tarinai toki dake ireru.
    REM (Kore ga nai to "XX ga hitsuyou desu" to iwarete, sagyou ga tomaru)
    echo [2/4] Buhin check...
    python -c "import msoffcrypto, pyzipper, simple_salesforce, googleapiclient, cryptography" 2>nul
    if errorlevel 1 (
        echo    Tarinai buhin wo ire naoshimasu...
        python -m pip install -q -r requirements.txt
    )
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

REM ------------------------------------------------------------
REM  Mae ni kidou shita apuri ga nokotte iru to, port 8501 ga
REM  fusagatte ite, atarashii apuri wa 8502 de tachiagaru.
REM  Sono kekka "furui (error no) gamen" to "atarashii gamen" no
REM  ryouhou ga hiraite shimau. Nokotte ireba tojite kara hajimeru.
REM ------------------------------------------------------------
REM  ^(%%P wo sono ba de tsukau. Block no naka de %VAR% wa tenkai sarenai tame^)
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:":8501 .*LISTENING" 2^>nul') do (
    echo  Mae no apuri ga nokotte imasu. Tojite kara hajime masu... ^(PID=%%P^)
    taskkill /PID %%P /F > nul 2>&1
    timeout /t 2 /nobreak > nul
)

REM Browser ga jidou de hirakanai kankyou mo aru node, koko de hiraku.
REM Server ga tachiagaru made sukoshi matte kara hiraku (hayasugiru to error gamen ni naru).
start "" /min powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 7; Start-Process 'http://localhost:8501'"

REM Port wo kotei suru. Kotei shinai to 8502 ni nagare, ue no URL to zureru.
python -m streamlit run app.py --server.port 8501

echo.
pause