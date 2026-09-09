@echo off
chcp 932 > nul
cd /d "%~dp0\.."
title EnkanAI Dev Setup

REM ============================================================
REM  Kaihatsu you no junbi (tantousha no PC ni wa fuyou)
REM   - git no namae wo Claude ni suru (CLAUDE.md no kimari)
REM   - gh (GitHub CLI) ga nakereba ireru -> PR wo koko kara tsukureru
REM  Betsu no PC de kaihatsu wo hajimeru toki ni, 1 kai dake jikkou suru.
REM ============================================================

echo.
echo ================================================
echo    EnkanAI kaihatsu no junbi
echo ================================================
echo.

echo [1/5] git no namae wo kakunin...
git --version > nul 2>&1
if errorlevel 1 (
    echo  [NG] git ga arimasen. saki ni update.bat wo jikkou shite kudasai.
    pause
    exit /b 1
)
set "GNAME="
for /f "delims=" %%n in ('git config user.name 2^>nul') do set "GNAME=%%n"
if not "%GNAME%"=="Claude" (
    git config user.name "Claude"
    git config user.email "noreply@anthropic.com"
    echo  git no namae wo Claude ni shimashita.
) else (
    echo  [OK] Claude ni natte imasu.
)
echo.

echo [2/5] gh (GitHub CLI) wo kakunin...
gh --version > nul 2>&1
if not errorlevel 1 goto :gh_ok
echo  gh ga arimasen. Ima kara iremasu...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-gh-user.ps1"
REM  ps1 ga jibun no PATH ni touroku shita basho wo yominaosu
for /f "delims=" %%p in ('powershell -NoProfile -Command "[Environment]::GetEnvironmentVariable(''PATH'',''User'')"') do set "PATH=%PATH%;%%p"
gh --version > nul 2>&1
if not errorlevel 1 goto :gh_ok
echo.
echo  [OK] gh wo iremashita ga, PATH ga mada tsutawatte imasen.
echo       Kono mado wo tojite, mou ichido kono file wo double click shite kudasai.
pause
exit /b

:gh_ok
gh --version
echo.

echo [3/5] GitHub he no login wo kakunin...
gh auth status > nul 2>&1
if errorlevel 1 (
    echo  Mada login shite imasen. Ima kara login shimasu.
    echo  ^(Browser ga hiraku node, GitHub ni login shite kudasai^)
    echo.
    gh auth login
) else (
    echo  [OK] login zumi desu.
)
echo.

echo [4/5] Apuri no buhin to browser wo kakunin...
python -m pip install -r requirements.txt --quiet
if errorlevel 1 (
    echo  [WARN] pip install de mondai ga arimashita. Tsuzukemasu.
)
python -m playwright install chromium
echo.

REM  Kagi (ENKAN_SECRET_KEY) ga hoka no PC to onaji ka wo tashikameru.
REM  Chigau to, hozon shita password ya GAS no kyoka ga yomenaku naru.
echo [5/5] Kagi (ENKAN_SECRET_KEY) wo kakunin...
if not exist ".streamlit\secrets.toml" (
    echo  [NG] .streamlit\secrets.toml ga arimasen.
    echo       Ugoite iru PC no file wo, sono mama copy shite kudasai.
    echo       ^(te de uchi naosu to, hozon shita password ga yomenaku narimasu^)
) else (
    python tools\key_check.py
)
echo.

echo ================================================
echo  Junbi kanryou. PR wa kono command de tsukuremasu:
echo    gh pr create --fill
echo ================================================
echo.
pause
