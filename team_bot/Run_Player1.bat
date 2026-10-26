@echo off
setlocal
set "BATTLE_SLOT=1"
call "%~dp0run_player.bat" %*
set "BATTLE_EXIT=%errorlevel%"
pause
exit /b %BATTLE_EXIT%
