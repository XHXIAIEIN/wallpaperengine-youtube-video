@echo off
REM Register start.vbs into HKCU Run so the helper launches at login
set "VBS=%~dp0start.vbs"
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "WallpaperStreamHelper" /t REG_SZ /d "wscript.exe \"%VBS%\"" /f
echo Registered: WallpaperStreamHelper -^> %VBS%
echo Starting helper now...
wscript.exe "%VBS%"
echo Done.
pause
