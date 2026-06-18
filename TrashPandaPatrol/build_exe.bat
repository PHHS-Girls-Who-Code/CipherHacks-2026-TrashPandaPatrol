@echo off
echo Installing dependencies into venv...
python -m venv .venv
call .\.venv\Scripts\pip.exe install -r requirements.txt
echo Building standalone executable...
.\.venv\Scripts\python -m PyInstaller --onefile --noconsole --name "TrashPandaPatrol" --add-data "assets;assets" main.py
echo.
echo === DONE ===
echo Your installable app is ready: dist\TrashPandaPatrol.exe
pause
