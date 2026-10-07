@echo off
cd /d "%~dp0.."
echo ==============================================================
echo   FAX test : dummy 1-page wo tsukutte, okureru ka tameshimasu
echo ==============================================================
echo.
echo   * --submit nashi dewa OKURIMASEN (soushin button no temae de cancel).
echo   * Bangou wa code ni kakimasen. Maikai kokode iremasu.
echo.
set "NUM="
set /p NUM=Okurisaki no FAX bangou (rei 0312345678) : 
if "%NUM%"=="" goto :end
set "NAME="
set /p NAME=Atesaki no namae (Enter de shoryaku) : 
set "NAMEARG="
if not "%NAME%"=="" set NAMEARG=--name "%NAME%"
set "PRN="
set /p PRN=Tsukau FAX printer (Enter de jidou) : 
set "PRNARG="
if not "%PRN%"=="" set PRNARG=--printer "%PRN%"
echo.
echo --- 1. Otameshi (okurimasen) -----------------------------------
python tools\fax_test.py --to %NUM% %NAMEARG% %PRNARG%
if errorlevel 1 goto :end
echo.
echo --- 2. Honban ------------------------------------------------
echo   Otameshi ga tootta node, kondo wa HONTO NI OKURIMASU.
set "ANS="
set /p ANS=Okurimasu ka? (y = okuru / sonota = yameru) : 
if /i not "%ANS%"=="y" goto :end
python tools\fax_test.py --to %NUM% %NAMEARG% %PRNARG% --submit
:end
echo.
pause
