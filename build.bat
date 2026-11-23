@echo off
rem Construit dist\Verbatim.exe (un seul fichier, sans console, sans Python requis pour l'utiliser)
pip install -r requirements.txt || exit /b 1
pyinstaller --noconfirm --noconsole --onefile --name Verbatim voice_claude.py || exit /b 1
echo.
echo Termine: dist\Verbatim.exe
