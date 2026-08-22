@echo off
REM Quick-launch Claude Code (terminal) in this bat file's own folder.
REM Copy this bat into any project folder to launch Claude Code there.
cd /d "%~dp0"
"C:\Users\Evan\.local\bin\claude.exe" %*
