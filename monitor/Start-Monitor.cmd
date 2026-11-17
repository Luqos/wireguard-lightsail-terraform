@echo off
rem Double-click to start wg-monitor. Pass -Restart to replace a running instance.
pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
