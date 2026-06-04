@echo off
setlocal EnableExtensions

cd /d "%~dp0"

set "RELEASE_NAME=music-freedom-release"
set "RELEASE_DIR=%CD%\release\%RELEASE_NAME%"
set "RELEASE_ZIP=%CD%\release\%RELEASE_NAME%.zip"

echo.
echo === Preparing release package ===
echo.

where npm >nul 2>nul
if errorlevel 1 (
    echo Node.js/npm is required on this PC to build frontend/dist.
    pause
    exit /b 1
)

echo Installing frontend dependencies...
pushd "frontend"
call npm install
if errorlevel 1 (
    popd
    echo Failed to install frontend dependencies.
    pause
    exit /b 1
)

echo Building frontend...
call npm run build
if errorlevel 1 (
    popd
    echo Failed to build frontend.
    pause
    exit /b 1
)
popd

if exist "%RELEASE_DIR%" rmdir /s /q "%RELEASE_DIR%"
if exist "%RELEASE_ZIP%" del /q "%RELEASE_ZIP%"
mkdir "%RELEASE_DIR%"

echo Copying project files...
robocopy "%CD%" "%RELEASE_DIR%" /E ^
    /XD ".git" ".idea" ".venv" ".runtime" "__pycache__" "node_modules" "downloaded_music" "backups" "archive" "release" "Lib" "Scripts" "site-packages" ^
    /XF ".env" "*.pyc" "*.pyo" "*.pyd" "*.log" "pyvenv.cfg" >nul

if %ERRORLEVEL% GEQ 8 (
    echo Failed to copy project files.
    pause
    exit /b 1
)

echo Creating zip...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Compress-Archive -Path '%RELEASE_DIR%\*' -DestinationPath '%RELEASE_ZIP%' -Force"
if errorlevel 1 (
    echo Failed to create release zip.
    pause
    exit /b 1
)

echo.
echo Release is ready:
echo %RELEASE_ZIP%
echo.
pause
