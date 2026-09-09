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

echo [1/3] git no namae wo kakunin...
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

echo [2/3] gh (GitHub CLI) wo kakunin...
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

echo [3/3] GitHub he no login wo kakunin...
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

echo ================================================
echo  Junbi kanryou. PR wa kono command de tsukuremasu:
echo    gh pr create --fill
echo ================================================
echo.
pause
