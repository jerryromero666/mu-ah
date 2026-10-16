@echo off
REM Double-click to run Ableton Bridge from source on Windows (needs Python 3.10+ from python.org).
cd /d "%~dp0"
start "" pythonw AbletonBridge.py %*
