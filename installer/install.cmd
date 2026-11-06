@echo off
rem Installs this dev-toolkit release for the current user. No admin needed.
rem Options go straight to dtk.exe, e.g.  install.cmd --groups rust,python
setlocal
"%~dp0dtk.exe" install %*
set rc=%errorlevel%
if %rc% geq 1000 goto blocked
if %rc% equ 9009 goto blocked
goto end
:blocked
echo.
echo dtk.exe could not start (error %rc%). If Windows said it was blocked, this machine
echo doesn't let programs run from this folder. Run check.cmd for details.
:end
if "%DTK_NO_PAUSE%"=="" pause
exit /b %rc%
