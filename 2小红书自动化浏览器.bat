@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1
title WhaleClaw - Chrome Debug Mode (Close this window to stop debug Chrome)

echo ============================================
echo   WhaleClaw - Chrome CDP Debug Launcher
echo ============================================
echo.

REM --- 检测 Chrome 安装路径 ---
set "CHROME_PATH="

if exist "C:\Program Files\Google\Chrome\Application\chrome.exe" (
    set "CHROME_PATH=C:\Program Files\Google\Chrome\Application\chrome.exe"
)
if exist "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" (
    set "CHROME_PATH=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
)
if exist "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe" (
    set "CHROME_PATH=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
)

if "!CHROME_PATH!"=="" (
    echo [ERROR] Chrome not found.
    pause
    exit /b 1
)

echo [INFO] Chrome: !CHROME_PATH!

REM --- CDP 专用用户数据目录（与默认 Chrome 完全隔离） ---
set "CDP_USER_DATA=%USERPROFILE%\.whaleclaw\chrome-cdp-profile"
if not exist "!CDP_USER_DATA!" mkdir "!CDP_USER_DATA!"
echo [INFO] User data dir: !CDP_USER_DATA!

REM --- 清理旧的调试 Chrome 进程（不动正常 Chrome） ---
set "KILLED_OLD=0"
for /f "tokens=2 delims=," %%a in ('wmic process where "Name='chrome.exe' and CommandLine like '%%chrome-cdp-profile%%'" get ProcessId /format:csv 2^>nul ^| findstr /r "[0-9]"') do (
    echo [INFO] Killing old debug Chrome process PID=%%a ...
    taskkill /F /PID %%a >nul 2>&1
    set "KILLED_OLD=1"
)
if "!KILLED_OLD!"=="1" (
    echo [INFO] Old debug Chrome killed. Restarting fresh...
    timeout /t 2 /nobreak >nul
)

REM --- 写入 WhaleClaw 配置 ---
set "WC_CONFIG=%USERPROFILE%\.whaleclaw\whaleclaw.json"
if not exist "%USERPROFILE%\.whaleclaw" mkdir "%USERPROFILE%\.whaleclaw"
set "PY_EXE=%~dp0python\python.exe"
if exist "!PY_EXE!" (
    "!PY_EXE!" -c "import json,pathlib;p=pathlib.Path(r'!WC_CONFIG!');d=json.loads(p.read_text('utf-8')) if p.is_file() else {};d.setdefault('plugins',{}).setdefault('browser',{})['cdp_url']='http://localhost:9222';p.write_text(json.dumps(d,indent=2,ensure_ascii=False),'utf-8');print('[OK] cdp_url written to',p)"
)

REM --- 启动调试模式 Chrome（独立实例，与正常 Chrome 并行） ---
echo [INFO] Starting debug Chrome with --remote-debugging-port=9222 ...
echo [INFO] Your normal Chrome will NOT be affected.
start "" "!CHROME_PATH!" --remote-debugging-port=9222 --user-data-dir="!CDP_USER_DATA!"

REM --- 轮询等待 CDP 端口就绪（最多 20 秒） ---
echo [INFO] Waiting for CDP port to be ready...
set "READY=0"
for /L %%i in (1,1,20) do (
    if "!READY!"=="0" (
        timeout /t 1 /nobreak >nul
        curl -s http://localhost:9222/json >nul 2>&1
        if !errorlevel!==0 set "READY=1"
    )
)

if "!READY!"=="0" (
    echo.
    echo [FAIL] CDP port 9222 is not responding after 20 seconds.
    echo.
    echo Troubleshooting:
    echo   1. Close ALL Chrome windows and run this script again
    echo   2. Make sure no other program is using port 9222
    echo.
    pause
    exit /b 1
)

echo.
echo ============================================
echo   [OK] Chrome debug mode is running!
echo   [OK] CDP endpoint: http://localhost:9222
echo ============================================
echo.
echo NOTE: This is a SEPARATE Chrome instance with its own profile.
echo       Your normal Chrome is NOT affected.
echo.
echo ****************************************************
echo *  Close this window = debug Chrome auto-stops
echo *  Then your normal Chrome can be used as usual
echo ****************************************************
echo.

REM ============================================================
REM   Keep alive + cleanup on exit.
REM   When user closes this window, cmd.exe dies, and the
REM   WATCH_LOOP stops. Next launch will kill leftovers.
REM   If Chrome exits first (user closed all debug windows),
REM   we detect it and clean up properly.
REM ============================================================

:WATCH_LOOP
    wmic process where "Name='chrome.exe' and CommandLine like '%%chrome-cdp-profile%%'" get ProcessId /format:csv 2>nul | findstr /r "[0-9]" >nul 2>&1
    if !errorlevel! neq 0 (
        echo.
        echo [INFO] Debug Chrome has been closed by user.
        goto CLEANUP
    )
    timeout /t 5 /nobreak >nul
    goto WATCH_LOOP

:CLEANUP
echo.
echo [INFO] Cleaning up...

for /f "tokens=2 delims=," %%a in ('wmic process where "Name='chrome.exe' and CommandLine like '%%chrome-cdp-profile%%'" get ProcessId /format:csv 2^>nul ^| findstr /r "[0-9]"') do (
    echo [INFO] Stopping debug Chrome PID=%%a ...
    taskkill /F /PID %%a >nul 2>&1
)

if exist "!PY_EXE!" (
    if exist "!WC_CONFIG!" (
        "!PY_EXE!" -c "import json,pathlib;p=pathlib.Path(r'!WC_CONFIG!');d=json.loads(p.read_text('utf-8'));b=d.get('plugins',{}).get('browser',{});changed=b.pop('cdp_url',None);p.write_text(json.dumps(d,indent=2,ensure_ascii=False),'utf-8') if changed else None;print('[OK] cdp_url removed' if changed else '[INFO] cdp_url not set')"
    )
)

echo [OK] Debug Chrome stopped. Config cleaned. You can use normal Chrome now.
echo.
timeout /t 3 /nobreak >nul
endlocal
exit /b 0
