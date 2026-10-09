@echo off
rem Builds dist\PDFEditor.exe (single file, no console window)
cd /d "%~dp0"
python -m pip install -r requirements.txt pyinstaller || goto :err
python -m PyInstaller --noconfirm --clean --onefile --windowed --name PDFEditor pdf_editor.py || goto :err
echo.
echo Done: %~dp0dist\PDFEditor.exe
pause
exit /b 0
:err
echo Build failed.
pause
exit /b 1
