@echo off
rem Construit dist\SpeechToText.exe (un seul fichier, sans console, sans Python requis pour l'utiliser)
pip install -r requirements.txt || exit /b 1
pyinstaller --noconfirm --noconsole --onefile --name SpeechToText voice_claude.py || exit /b 1
echo.
echo Termine: dist\SpeechToText.exe
