@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul 2>&1
title WhaleClaw - 小红书自动化浏览器（关闭此窗口即停止）

echo ============================================
echo   WhaleClaw - 小红书自动化浏览器
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
    echo [错误] 未找到 Chrome 浏览器。
    pause
    exit /b 1
)

echo [信息] Chrome 路径: !CHROME_PATH!

REM --- CDP 专用用户数据目录（与默认 Chrome 完全隔离） ---
set "CDP_USER_DATA=%USERPROFILE%\.whaleclaw\chrome-cdp-profile"
if not exist "!CDP_USER_DATA!" mkdir "!CDP_USER_DATA!"
echo [信息] 用户数据目录: !CDP_USER_DATA!

REM --- 清理旧的调试 Chrome 进程（不动正常 Chrome） ---
set "KILLED_OLD=0"
for /f "tokens=2 delims=," %%a in ('wmic process where "Name='chrome.exe' and CommandLine like '%%chrome-cdp-profile%%'" get ProcessId /format:csv 2^>nul ^| findstr /r "[0-9]"') do (
    echo [信息] 正在关闭旧的调试 Chrome 进程 PID=%%a ...
    taskkill /F /PID %%a >nul 2>&1
    set "KILLED_OLD=1"
)
if "!KILLED_OLD!"=="1" (
    echo [信息] 旧进程已关闭，正在重新启动...
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
echo [信息] 正在启动调试模式 Chrome（端口 9222）...
echo [信息] 你的日常 Chrome 不受影响。
start "" "!CHROME_PATH!" --remote-debugging-port=9222 --user-data-dir="!CDP_USER_DATA!"

REM --- 轮询等待 CDP 端口就绪（最多 20 秒） ---
echo [信息] 等待 CDP 端口就绪...
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
    echo [失败] CDP 端口 9222 在 20 秒内没有响应。
    echo.
    echo 排查方法:
    echo   1. 关闭所有 Chrome 窗口后重新运行此脚本
    echo   2. 确保没有其他程序占用 9222 端口
    echo.
    pause
    exit /b 1
)

echo.
echo ============================================
echo   [OK] Chrome 调试模式已启动！
echo   [OK] CDP 地址: http://localhost:9222
echo ============================================
echo.
echo 提示: 这是一个独立的 Chrome 实例，有自己的配置文件。你的日常 Chrome 不受任何影响。
echo.
echo ****************************************************
echo *  关闭此窗口 = 自动停止调试 Chrome
echo *  之后你的日常 Chrome 可以正常使用
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
        echo [信息] 调试 Chrome 已被用户关闭。
        goto CLEANUP
    )
    timeout /t 5 /nobreak >nul
    goto WATCH_LOOP

:CLEANUP
echo.
echo [信息] 正在清理...

for /f "tokens=2 delims=," %%a in ('wmic process where "Name='chrome.exe' and CommandLine like '%%chrome-cdp-profile%%'" get ProcessId /format:csv 2^>nul ^| findstr /r "[0-9]"') do (
    echo [信息] 正在停止调试 Chrome PID=%%a ...
    taskkill /F /PID %%a >nul 2>&1
)

if exist "!PY_EXE!" (
    if exist "!WC_CONFIG!" (
        "!PY_EXE!" -c "import json,pathlib;p=pathlib.Path(r'!WC_CONFIG!');d=json.loads(p.read_text('utf-8'));b=d.get('plugins',{}).get('browser',{});changed=b.pop('cdp_url',None);p.write_text(json.dumps(d,indent=2,ensure_ascii=False),'utf-8') if changed else None;print('[OK] cdp_url removed' if changed else '[INFO] cdp_url not set')"
    )
)

echo [OK] 调试 Chrome 已停止，配置已清理。你可以正常使用 Chrome 了。
echo.
timeout /t 3 /nobreak >nul
endlocal
exit /b 0
