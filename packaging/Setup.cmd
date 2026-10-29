@echo off
setlocal
echo Agent Workspace setup
echo.
if not exist "%~dp0runtime\node.exe" (
  echo ERROR: runtime\node.exe was not found. Extract the complete ZIP first.
  pause
  exit /b 1
)
"%~dp0runtime\node.exe" "%~dp0app.mjs" setup %*
set "taskSetupCode=%ERRORLEVEL%"
echo.
if not "%taskSetupCode%"=="0" echo Setup failed. Copy the error above; exit code %taskSetupCode%.
pause
exit /b %taskSetupCode%
