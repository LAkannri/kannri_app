@echo off
cd /d "%~dp0.."
echo ==============================================================
echo   PC-FAX no gamen wo sagashite, gamen no ichiban mae ni dashimasu
echo   (FAX wa okurimasen)
echo ==============================================================
echo.
python tools\fax_test.py --show
echo.
pause
