@echo off
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "WallpaperStreamHelper" /f
taskkill /F /IM pythonw.exe 2>nul
echo Removed autostart and killed pythonw helpers.
pause
