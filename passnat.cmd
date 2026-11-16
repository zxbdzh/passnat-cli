@echo off
setlocal
pushd "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
    python -m passnat %*
) else (
    py -3 -m passnat %*
)
set "PASSNAT_EXIT=%ERRORLEVEL%"
popd
exit /b %PASSNAT_EXIT%
