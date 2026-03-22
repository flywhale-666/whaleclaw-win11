"""Desktop capture tool with optional display wake-up (macOS + Windows)."""

from __future__ import annotations

import asyncio
import sys
import uuid
from typing import Any

from whaleclaw.config.paths import WHALECLAW_HOME
from whaleclaw.tools.base import Tool, ToolDefinition, ToolParameter, ToolResult

_SCREENSHOT_DIR = WHALECLAW_HOME / "screenshots"


class DesktopCaptureTool(Tool):
    """Capture desktop screenshot (macOS + Windows)."""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="desktop_capture",
            description="Capture desktop screenshot; can wake display first (macOS/Windows).",
            parameters=[
                ToolParameter(
                    name="wake",
                    type="boolean",
                    description="Wake display before screenshot (default true).",
                    required=False,
                ),
                ToolParameter(
                    name="delay_ms",
                    type="integer",
                    description="Delay after wake-up before capture (default 350ms).",
                    required=False,
                ),
                ToolParameter(
                    name="filename",
                    type="string",
                    description="Optional output filename (png).",
                    required=False,
                ),
            ],
        )

    async def _run(
        self,
        *args: str,
        timeout: float = 8.0,
    ) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return 124, "", "command timeout"
        return (
            proc.returncode or 0,
            out.decode(errors="replace"),
            err.decode(errors="replace"),
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        wake = bool(kwargs.get("wake", True))
        delay_ms = int(kwargs.get("delay_ms", 350))
        filename = str(kwargs.get("filename", "")).strip()
        if filename and not filename.lower().endswith(".png"):
            filename += ".png"
        if not filename:
            filename = f"desktop_{uuid.uuid4().hex[:8]}.png"

        _SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        output_path = _SCREENSHOT_DIR / filename

        if sys.platform == "darwin":
            return await self._capture_macos(output_path, wake=wake, delay_ms=delay_ms)
        if sys.platform == "win32":
            return await self._capture_windows(output_path)
        return ToolResult(success=False, output="", error=f"desktop_capture 仅支持 macOS 和 Windows，不支持当前平台: {sys.platform}")

    async def _capture_macos(self, output_path: "Path", *, wake: bool, delay_ms: int) -> ToolResult:
        if wake:
            _ = await self._run("/usr/bin/caffeinate", "-u", "-t", "2", timeout=3.0)
            if delay_ms > 0:
                await asyncio.sleep(min(delay_ms, 4000) / 1000)

        code, _out, err = await self._run(
            "/usr/sbin/screencapture",
            "-x",
            str(output_path),
            timeout=8.0,
        )
        if code != 0 or not output_path.is_file():
            return ToolResult(
                success=False,
                output="",
                error=f"桌面截图失败: {err.strip() or f'exit={code}'}",
            )
        return ToolResult(success=True, output=f"桌面截图已保存: {output_path}")

    async def _capture_windows(self, output_path: "Path") -> ToolResult:
        # Use PowerShell + .NET to capture the full physical screen (DPI-aware)
        ps_script = (
            "Add-Type @'\n"
            "using System; using System.Runtime.InteropServices;\n"
            "public class ScreenDpi {\n"
            "    [DllImport(\"user32.dll\")] public static extern bool SetProcessDPIAware();\n"
            "    [DllImport(\"gdi32.dll\")] public static extern int GetDeviceCaps(IntPtr hdc, int i);\n"
            "    [DllImport(\"user32.dll\")] public static extern IntPtr GetDC(IntPtr hwnd);\n"
            "    [DllImport(\"user32.dll\")] public static extern int ReleaseDC(IntPtr hwnd, IntPtr hdc);\n"
            "}\n"
            "'@\n"
            "[ScreenDpi]::SetProcessDPIAware() | Out-Null; "
            "$hdc = [ScreenDpi]::GetDC([IntPtr]::Zero); "
            "$w = [ScreenDpi]::GetDeviceCaps($hdc, 118); "  # DESKTOPHORZRES
            "$h = [ScreenDpi]::GetDeviceCaps($hdc, 117); "  # DESKTOPVERTRES
            "[ScreenDpi]::ReleaseDC([IntPtr]::Zero, $hdc) | Out-Null; "
            "Add-Type -AssemblyName System.Drawing; "
            "$bmp = New-Object System.Drawing.Bitmap($w, $h); "
            "$g = [System.Drawing.Graphics]::FromImage($bmp); "
            "$g.CopyFromScreen(0, 0, 0, 0, (New-Object System.Drawing.Size($w, $h))); "
            f"$bmp.Save('{output_path}'); "
            "$g.Dispose(); $bmp.Dispose()"
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                "powershell.exe", "-NoProfile", "-Command", ps_script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _out, err_bytes = await asyncio.wait_for(proc.communicate(), timeout=10.0)
            err = err_bytes.decode(errors="replace") if err_bytes else ""
        except TimeoutError:
            return ToolResult(success=False, output="", error="截图超时")
        except OSError as exc:
            return ToolResult(success=False, output="", error=str(exc))

        if proc.returncode != 0 or not output_path.is_file():
            return ToolResult(
                success=False,
                output="",
                error=f"桌面截图失败: {err.strip() or f'exit={proc.returncode}'}",
            )
        return ToolResult(success=True, output=f"桌面截图已保存: {output_path}")
