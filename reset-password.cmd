@echo off
rem Reset a password from the project folder, e.g.:  reset-password.cmd owner@bina.example   or   reset-password.cmd --demo
rem (see backend\abteilo\reset_password.py). The new password is typed at the prompt, never passed as an argument.
pushd "%~dp0backend"
".venv\Scripts\python.exe" -m abteilo.reset_password %*
set RC=%ERRORLEVEL%
popd
exit /b %RC%
