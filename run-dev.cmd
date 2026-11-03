@echo off
rem Double-click to start the local Abteilo stack (see scripts\dev.ps1). Extra arguments pass through, e.g. -Seed.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\dev.ps1" %*
