@echo off
chcp 65001 >nul 2>&1
echo ========================================
echo   安装 mcporter CLI 工具
echo ========================================
echo.

where node >nul 2>&1
if %errorlevel% neq 0 (
    echo [提示] 未检测到 Node.js，正在自动安装...
    echo.
    where winget >nul 2>&1
    if %errorlevel% neq 0 (
        echo [错误] 当前系统不支持 winget，请手动安装 Node.js
        echo 下载地址: https://nodejs.org/
        echo.
        pause
        exit /b 1
    )
    winget install OpenJS.NodeJS.LTS --accept-source-agreements --accept-package-agreements
    if %errorlevel% neq 0 (
        echo [错误] Node.js 安装失败，请手动安装
        echo 下载地址: https://nodejs.org/
        echo.
        pause
        exit /b 1
    )
    echo.
    echo [提示] Node.js 安装完成，需要刷新环境变量...
    echo 请关闭此窗口，重新双击本脚本完成 mcporter 安装。
    echo.
    pause
    exit /b 0
)

echo [1/3] 检测 Node.js 版本...
node --version
echo.

echo [2/3] 正在安装 mcporter（全局）...
call npm install -g mcporter
echo.

if %errorlevel% neq 0 (
    echo [错误] 安装失败，请检查网络连接
    echo.
    pause
    exit /b 1
)

echo [3/3] 验证安装...
call npx mcporter --version
echo.

echo ========================================
echo   mcporter 安装完成！
echo ========================================
echo.
echo 下一步：
echo   1. 打开 https://mcp.dingtalk.com/#/detail?mcpId=9555
echo   2. 点击「获取 MCP Server 配置」复制 URL
echo   3. 在 WhaleClaw 对话中告诉 Agent 配置该 URL
echo.
pause
