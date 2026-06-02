@echo off
setlocal EnableExtensions

cd /d "%~dp0"

set "PYTHON_VERSION=3.11.9"
set "PYTHON_URL=https://www.nuget.org/api/v2/package/python/%PYTHON_VERSION%"
set "RUNTIME_DIR=%CD%\.runtime"
set "LOCAL_PYTHON_DIR=%RUNTIME_DIR%\python"
set "LOCAL_PYTHON_EXE=%LOCAL_PYTHON_DIR%\tools\python.exe"
set "PYTHON_PACKAGE=%TEMP%\python-%PYTHON_VERSION%.zip"
set "VENV_PYTHON=%CD%\.venv\Scripts\python.exe"
set "FRONTEND_DIST=%CD%\frontend\dist\index.html"
set "APP_URL=http://127.0.0.1:8000"

echo.
echo === music-freedom launcher ===
echo Project: %CD%
echo.

if not exist ".env" (
    if exist ".env.example" (
        echo Creating .env from .env.example
        copy ".env.example" ".env" >nul
    )
)

if exist "%LOCAL_PYTHON_EXE%" (
    set PYTHON_BOOT="%LOCAL_PYTHON_EXE%"
    goto :ensure_venv
)

call :find_local_python
if not errorlevel 1 (
    goto :ensure_venv
)

where py >nul 2>nul
if not errorlevel 1 (
    py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
    if not errorlevel 1 (
        set "PYTHON_BOOT=py -3"
        goto :ensure_venv
    )
)

where python >nul 2>nul
if not errorlevel 1 (
    python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
    if not errorlevel 1 (
        set "PYTHON_BOOT=python"
        goto :ensure_venv
    )
)

echo Python 3.10+ was not found. Downloading portable Python %PYTHON_VERSION% locally...
if not exist "%RUNTIME_DIR%" mkdir "%RUNTIME_DIR%"

powershell -NoProfile -ExecutionPolicy Bypass -Command "try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri '%PYTHON_URL%' -OutFile '%PYTHON_PACKAGE%' } catch { Write-Host $_; exit 1 }"
if errorlevel 1 (
    echo Failed to download portable Python package.
    pause
    exit /b 1
)

echo Extracting Python into %LOCAL_PYTHON_DIR%
if exist "%LOCAL_PYTHON_DIR%" rmdir /s /q "%LOCAL_PYTHON_DIR%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Expand-Archive -LiteralPath '%PYTHON_PACKAGE%' -DestinationPath '%LOCAL_PYTHON_DIR%' -Force } catch { Write-Host $_; exit 1 }"
if errorlevel 1 (
    echo Failed to extract portable Python package.
    pause
    exit /b 1
)

call :find_local_python
if errorlevel 1 (
    echo Portable Python was extracted, but python.exe was not found.
    echo Checked:
    echo %LOCAL_PYTHON_DIR%
    echo %RUNTIME_DIR%
    echo %LOCALAPPDATA%\Programs\Python
    echo %ProgramFiles%\Python311
    pause
    exit /b 1
)

:ensure_venv
if not exist "%VENV_PYTHON%" (
    echo Creating Python virtual environment...
    %PYTHON_BOOT% -m venv ".venv"
    if errorlevel 1 (
        echo Failed to create virtual environment.
        pause
        exit /b 1
    )
)

echo Installing/updating Python dependencies...
"%VENV_PYTHON%" -m pip install --upgrade pip
if errorlevel 1 (
    echo Failed to upgrade pip.
    pause
    exit /b 1
)

"%VENV_PYTHON%" -m pip install -r "requirements.txt"
if errorlevel 1 (
    echo Failed to install Python dependencies.
    pause
    exit /b 1
)

if not exist "%FRONTEND_DIST%" (
    echo Frontend build was not found.
    where npm >nul 2>nul
    if errorlevel 1 (
        echo Node.js/npm is not installed, and frontend/dist is missing.
        echo Build frontend/dist before moving the project, or install Node.js 18+.
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
)

echo.
echo Starting app at %APP_URL%
start "" "%APP_URL%"
"%VENV_PYTHON%" -m uvicorn web.app:app --host 127.0.0.1 --port 8000

echo.
echo Server stopped.
pause
exit /b 0

:find_local_python
if exist "%LOCAL_PYTHON_EXE%" (
    "%LOCAL_PYTHON_EXE%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
    if not errorlevel 1 (
        set PYTHON_BOOT="%LOCAL_PYTHON_EXE%"
        exit /b 0
    )
)

if exist "%RUNTIME_DIR%" (
    for /r "%RUNTIME_DIR%" %%P in (python.exe) do (
        "%%P" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
        if not errorlevel 1 (
            set PYTHON_BOOT="%%P"
            exit /b 0
        )
    )
)

if exist "%LOCALAPPDATA%\Programs\Python" (
    for /r "%LOCALAPPDATA%\Programs\Python" %%P in (python.exe) do (
        "%%P" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
        if not errorlevel 1 (
            set PYTHON_BOOT="%%P"
            exit /b 0
        )
    )
)

if exist "%ProgramFiles%\Python311" (
    for /r "%ProgramFiles%\Python311" %%P in (python.exe) do (
        "%%P" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
        if not errorlevel 1 (
            set PYTHON_BOOT="%%P"
            exit /b 0
        )
    )
)

exit /b 1
