@echo off
rem Builds CamTuner.exe directly on a Windows PC (needs Python 3.12 installed).
rem The finished file appears in the dist folder.
cd /d "%~dp0"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt pyinstaller
python -m PyInstaller --noconfirm --onefile --windowed --name CamTuner --collect-all pyvirtualcam --collect-all pygrabber --collect-all pystray --hidden-import usage camtuner.py
echo.
echo Done. Your app is at dist\CamTuner.exe
pause
