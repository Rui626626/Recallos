@echo off
rem RecallOS launcher (Windows).
rem
rem Why this file exists: the Skill can only tell an AI *where* RecallOS lives if
rem something stable exists to call. `%~dp0` resolves to this script's own
rem directory, so this wrapper works from any cwd and survives the repo moving.
rem Put this bin\ directory on PATH and `recall search ...` works everywhere.
setlocal
set PYTHONIOENCODING=utf-8
python "%~dp0recall.py" %*
exit /b %ERRORLEVEL%
