@echo off
REM Quick-launch Claude Code (terminal) in the project root (parent of this tools\ folder).
REM Keep it in tools\ (it cd's one level up).
cd /d "%~dp0.."
"C:\Users\Evan\.local\bin\claude.exe" %*
