@echo off
rem Checks what this machine allows before you install. Changes nothing.
setlocal
"%~dp0dtk.exe" check %*
set rc=%errorlevel%
if %rc% geq 1000 goto blocked
if %rc% equ 9009 goto blocked
goto end
:blocked
echo.
echo dtk.exe could not start (error %rc%). If Windows said it was blocked, this machine
echo doesn't let programs run from this folder, and the kit's tools would be blocked too.
echo Try copying the release folder somewhere under your user folder and run this again.
:end
if "%DTK_NO_PAUSE%"=="" pause
exit /b %rc%
