@echo off
setlocal
cd /d "%~dp0"
if not defined BATTLE_SLOT set "BATTLE_SLOT=1"
if "%BATTLE_SLOT%"=="1" goto slot_ok
if "%BATTLE_SLOT%"=="2" goto slot_ok
if "%BATTLE_SLOT%"=="3" goto slot_ok
echo Invalid BATTLE_SLOT. Use Run_Player1.bat, Run_Player2.bat or Run_Player3.bat.
exit /b 2
:slot_ok
set "BOT_FILE="
set "BOT_COUNT=0"
for %%F in ("%~dp0Player%BATTLE_SLOT%_*.py") do if exist "%%~fF" (
  set "BOT_FILE=%%~fF"
  set /a BOT_COUNT+=1 >nul
)
if not "%BOT_COUNT%"=="1" (
  echo Expected exactly one Player%BATTLE_SLOT%_*.py in this folder.
  exit /b 2
)
if defined BATTLE_PYTHON goto explicit_python
if not exist ".venv\Scripts\python.exe" goto try_312
".venv\Scripts\python.exe" -c "import platform,sys; sys.exit(0 if platform.python_implementation()=='CPython' and sys.version_info >= (3,9) else 1)" >nul 2>nul
if errorlevel 1 goto try_312
".venv\Scripts\python.exe" "%BOT_FILE%" %*
exit /b %errorlevel%
:try_312
py -3.12 -c "import platform,sys; sys.exit(0 if platform.python_implementation()=='CPython' and sys.version_info >= (3,9) else 1)" >nul 2>nul
if errorlevel 1 goto try_py
py -3.12 "%BOT_FILE%" %*
exit /b %errorlevel%
:try_py
py -3 -c "import platform,sys; sys.exit(0 if platform.python_implementation()=='CPython' and sys.version_info >= (3,9) else 1)" >nul 2>nul
if errorlevel 1 goto try_python
py -3 "%BOT_FILE%" %*
exit /b %errorlevel%
:try_python
python -c "import platform,sys; sys.exit(0 if platform.python_implementation()=='CPython' and sys.version_info >= (3,9) else 1)" >nul 2>nul
if errorlevel 1 goto no_cpython
python "%BOT_FILE%" %*
exit /b %errorlevel%
:explicit_python
"%BATTLE_PYTHON%" -c "import platform,sys; sys.exit(0 if platform.python_implementation()=='CPython' and sys.version_info >= (3,9) else 1)" >nul 2>nul
if errorlevel 1 goto no_cpython
"%BATTLE_PYTHON%" "%BOT_FILE%" %*
exit /b %errorlevel%
:no_cpython
echo CPython 3.9+ is required. Python 3.12 from python.org is recommended.
echo This launcher will not run the bot with PyPy.
echo Install CPython, or set BATTLE_PYTHON to its python.exe path.
exit /b 2
