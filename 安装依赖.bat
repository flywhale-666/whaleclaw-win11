@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "PYTHON="
set "PYTHON=%~dp0python\python.exe"
if not exist "%PYTHON%" set "PYTHON="

:found
if not defined PYTHON (
  echo [ERROR] Portable Python not found at:
  echo         %~dp0python\python.exe
  echo Please extract Python embeddable package into .\python first.
  pause
  exit /b 1
)

echo [INFO] Using Python: %PYTHON%
"%PYTHON%" -c "import sys; print(sys.version)"
if errorlevel 1 (
  echo [ERROR] Python is not runnable.
  pause
  exit /b 1
)

echo [INFO] Upgrading pip/setuptools/wheel...
"%PYTHON%" -m pip --version >nul 2>nul
if errorlevel 1 (
  echo [INFO] pip not found. Bootstrapping pip for embeddable Python...
  call :bootstrap_pip
  if errorlevel 1 (
    echo [ERROR] Failed to bootstrap pip.
    pause
    exit /b 1
  )
)

"%PYTHON%" -m pip install -U pip setuptools wheel
if errorlevel 1 (
  echo [ERROR] Failed to upgrade packaging tools.
  pause
  exit /b 1
)

echo [INFO] Installing WhaleClaw dependencies ^(dev + office + vision^)...
"%PYTHON%" -m pip install -e ".[dev,office,vision]"
if errorlevel 1 (
  echo [ERROR] Dependency installation failed.
  pause
  exit /b 1
)

if /i "%WHALECLAW_INSTALL_CHROMIUM%"=="1" (
  echo [INFO] Installing Playwright Chromium...
  "%PYTHON%" -m playwright install chromium
  if errorlevel 1 (
    echo [WARN] Playwright browser install failed. You can retry later.
  )
) else (
  echo [INFO] Skip Playwright Chromium install by default.
  echo [INFO] To install it, run:
  echo        set WHALECLAW_INSTALL_CHROMIUM=1 ^&^& "%~f0"
)

echo [OK] Done.
pause
exit /b 0

:bootstrap_pip
if not exist "%~dp0python\python312._pth" goto :get_pip

set "HAS_SITEPKG="
set "HAS_IMPORT_SITE="
for /f "usebackq delims=" %%L in ("%~dp0python\python312._pth") do (
  if /i "%%L"=="Lib\site-packages" set "HAS_SITEPKG=1"
  if /i "%%L"=="import site" set "HAS_IMPORT_SITE=1"
)

if not defined HAS_SITEPKG echo Lib\site-packages>>"%~dp0python\python312._pth"
if not defined HAS_IMPORT_SITE echo import site>>"%~dp0python\python312._pth"

:get_pip
set "GETPIP=%TEMP%\get-pip.py"
where curl >nul 2>nul
if errorlevel 1 (
  echo [ERROR] curl not found. Please download manually:
  echo         https://bootstrap.pypa.io/get-pip.py
  echo Then run:
  echo         "%PYTHON%" get-pip.py
  exit /b 1
)

curl -L --fail "https://bootstrap.pypa.io/get-pip.py" -o "%GETPIP%"
if errorlevel 1 (
  echo [ERROR] Download get-pip.py failed.
  exit /b 1
)

"%PYTHON%" "%GETPIP%"
if errorlevel 1 exit /b 1
exit /b 0
