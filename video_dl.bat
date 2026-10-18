@echo off
rem Usage: video_dl.bat <url> [-o folder]   (or double-click and paste a URL)
python "%~dp0video_dl.py" %*
if "%~1"=="" pause
