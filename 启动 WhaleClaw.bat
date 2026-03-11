@echo off
setlocal

set "PORT=18666"
if not defined BIND set "BIND=127.0.0.1"
set "PYTHON_EXE=%~dp0python\python.exe"
set "CLAWHUB_NPM_BIN=D:\nodejs\npm.cmd"
set "CLAWHUB_BIN=%~dp0.local\npm-global\clawhub.cmd"
set "CLAWHUB_TOKEN="
set "PSH=powershell -NoProfile -ExecutionPolicy Bypass -Command"

for /f "tokens=5" %%A in ('netstat -ano ^| findstr /R /C:"127.0.0.1:%PORT% .*LISTENING"') do set "PID=%%A"

if defined PID (
    %PSH% "Write-Host (([regex]::Unescape('[WhaleClaw] \u68C0\u6D4B\u5230\u7AEF\u53E3 ')) + '%PORT%' + ([regex]::Unescape(' \u88AB\u5360\u7528, \u6B63\u5728\u91CA\u653E...')))"
    %PSH% "Write-Host (([regex]::Unescape('[WhaleClaw] \u5360\u7528\u8FDB\u7A0B PID: ')) + '%PID%')"
    taskkill /PID %PID% /F >nul 2>&1
    if errorlevel 1 (
        %PSH% "Write-Host (([regex]::Unescape('[WhaleClaw] \u91CA\u653E\u7AEF\u53E3\u5931\u8D25, \u8BF7\u624B\u52A8\u5173\u95ED PID ')) + '%PID%' + ([regex]::Unescape(' \u540E\u91CD\u8BD5.')))"
        %PSH% "Write-Host ([regex]::Unescape('[WhaleClaw] \u53EF\u80FD\u662F\u6743\u9650\u4E0D\u8DB3, \u8BF7\u53F3\u952E\u4EE5\u7BA1\u7406\u5458\u8EAB\u4EFD\u8FD0\u884C\u672C\u811A\u672C.'))"
        pause
        exit /b 1
    )
    %PSH% "Write-Host (([regex]::Unescape('[WhaleClaw] \u7AEF\u53E3 ')) + '%PORT%' + ([regex]::Unescape(' \u5DF2\u91CA\u653E.')))"
)

%PSH% "Write-Host ''; Write-Host ([regex]::Unescape('  \uD83D\uDC0B WhaleClaw Gateway \u6B63\u5728\u542F\u52A8...')); Write-Host ([regex]::Unescape('  \uD83C\uDF89 B\u7AD9\u98DE\u7FD4\u9CB8\u795D\u60A8\u9A6C\u5E74\u5927\u5409\uFF01\u8D22\u6E90\u5E7F\u8FDB\uFF01WhaleClaw \u514D\u8D39\u5F00\u6E90\uFF01')); Write-Host '  ---------------------------------'; Write-Host ''; Write-Host ('  ' + [regex]::Unescape('\uD83C\uDF10 WebChat:  http://') + '%BIND%' + ':' + '%PORT%'); Write-Host ('  ' + [regex]::Unescape('\uD83D\uDCE1 API:      http://') + '%BIND%' + ':' + '%PORT%' + '/api/status'); Write-Host ('  ' + [regex]::Unescape('\uD83D\uDD0C WS:       ws://') + '%BIND%' + ':' + '%PORT%' + '/ws'); Write-Host ''; Write-Host ([regex]::Unescape('  \u6309 Ctrl+C \u505C\u6B62\u670D\u52A1')); Write-Host '  ---------------------------------'; Write-Host ''"

"%PYTHON_EXE%" -X utf8 -m whaleclaw.entry
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    %PSH% "Write-Host (([regex]::Unescape('[WhaleClaw] \u542F\u52A8\u5931\u8D25, \u9000\u51FA\u7801: ')) + '%EXIT_CODE%')"
    pause
)

exit /b %EXIT_CODE%
