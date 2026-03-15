"""Tests for tool execution output summarization and nano-banana retry guards.

覆盖真实场景：
- bash: 短输出透传、长 pip install、长脚本执行、含进度条噪声、无关键词保底、超 300 字符截断
- file_write / file_edit: 短透传、长输出只取首行
- browser: 短透传、search_images 多路径、无路径保底
- 通用工具: 带路径行提取
- 失败路径: 不受影响
- 边界: 空输出、纯进度条行
"""

from __future__ import annotations

import asyncio
import time

import pytest

from whaleclaw.agent.helpers import tool_execution as tool_exec_mod
from whaleclaw.agent.helpers.tool_execution import (
    format_tool_output,
    is_transient_cli_usage_error,
)
from whaleclaw.providers.base import ToolCall
from whaleclaw.tools.base import Tool, ToolDefinition, ToolParameter, ToolResult
from whaleclaw.tools.registry import ToolRegistry


# ──────────────────────────────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────────────────────────────

def _ok(output: str, tool: str = "") -> str:
    return format_tool_output(ToolResult(success=True, output=output), tool)


def _err(error: str, output: str = "", tool: str = "bash") -> str:
    return format_tool_output(ToolResult(success=False, error=error, output=output), tool)


class _DummyBashTool(Tool):
    def __init__(self, result: ToolResult) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="bash",
            description="dummy bash",
            parameters=[ToolParameter(name="command", type="string", description="command")],
        )

    async def execute(self, **kwargs: object) -> ToolResult:
        self.calls.append(dict(kwargs))
        return self.result


class _SleepingBashTool(Tool):
    def __init__(self, *, sleep_seconds: float = 0.05) -> None:
        self.sleep_seconds = sleep_seconds
        self.starts: list[float] = []
        self.commands: list[str] = []

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="bash",
            description="sleeping bash",
            parameters=[ToolParameter(name="command", type="string", description="command")],
        )

    async def execute(self, **kwargs: object) -> ToolResult:
        self.starts.append(time.monotonic())
        self.commands.append(str(kwargs.get("command", "")))
        await asyncio.sleep(self.sleep_seconds)
        return ToolResult(success=True, output=f"done:{kwargs.get('command', '')}")


# ──────────────────────────────────────────────────────────────────────────────
# 短输出直接透传（<= 240 字符，任意工具）
# ──────────────────────────────────────────────────────────────────────────────

class TestShortOutputPassthrough:
    def test_bash_short_passthrough(self) -> None:
        out = "hello world"
        assert _ok(out, "bash") == out

    def test_file_write_short_passthrough(self) -> None:
        out = "文件已写入: /tmp/a.py (88 字节)"
        assert _ok(out, "file_write") == out

    def test_browser_short_passthrough(self) -> None:
        out = "已打开: https://example.com"
        assert _ok(out, "browser") == out

    def test_exactly_240_chars_passthrough(self) -> None:
        out = "x" * 240
        assert _ok(out, "bash") == out

    def test_241_chars_triggers_compression(self) -> None:
        # 241 字符全是 'x'，无关键词 → 保底首行+尾行，结果不等于原文
        out = "x" * 241
        result = _ok(out, "bash")
        # 触发压缩后，结果长度应 <= 300
        assert len(result) <= 300

    def test_empty_output(self) -> None:
        assert _ok("", "bash") == "(empty output)"

    def test_whitespace_only_output(self) -> None:
        assert _ok("   \n  \n  ", "bash") == "(empty output)"


# ──────────────────────────────────────────────────────────────────────────────
# bash —— 真实长输出场景
# ──────────────────────────────────────────────────────────────────────────────

class TestBashLongOutput:
    _PIP_INSTALL = "\n".join([
        "Collecting httpx",
        "  Downloading httpx-0.27.0-py3-none-any.whl (75 kB)",
        "     " + "\u2501" * 39 + " 75.0/75.0 kB 1.2 MB/s eta 0:00:00",
        "Collecting certifi",
        "  Downloading certifi-2024.2.2-py3-none-any.whl (163 kB)",
        "     " + "\u2501" * 39 + " 163.1/163.1 kB 3.1 MB/s eta 0:00:00",
        "Collecting anyio<5,>=3.5.0",
        "  Downloading anyio-4.3.0-py3-none-any.whl (86 kB)",
        "     " + "\u2501" * 39 + " 86.5/86.5 kB 5.2 MB/s eta 0:00:00",
        "Collecting sniffio>=1.1",
        "  Downloading sniffio-1.3.1-py3-none-any.whl (10 kB)",
        "Collecting idna",
        "  Downloading idna-3.6-py3-none-any.whl (61 kB)",
        "Requirement already satisfied: h11<0.15,>=0.13 in site-packages",
        "Installing collected packages: sniffio, idna, certifi, anyio, httpx",
        "Successfully installed httpx-0.27.0 certifi-2024.2.2 sniffio-1.3.1 idna-3.6 anyio-4.3.0",
        "exit:0",
    ])

    def test_pip_install_result_is_short(self) -> None:
        result = _ok(self._PIP_INSTALL, "bash")
        assert len(result) <= 300

    def test_pip_install_contains_success_line(self) -> None:
        result = _ok(self._PIP_INSTALL, "bash")
        assert "Successfully installed" in result

    def test_pip_install_contains_exit_code(self) -> None:
        result = _ok(self._PIP_INSTALL, "bash")
        assert "exit:0" in result

    def test_pip_install_no_progress_bar(self) -> None:
        result = _ok(self._PIP_INSTALL, "bash")
        assert "eta" not in result
        assert "kB" not in result

    def test_pip_install_starts_with_checkmark(self) -> None:
        result = _ok(self._PIP_INSTALL, "bash")
        assert result.startswith("\u2713")  # ✓

    def test_script_execution_with_path(self) -> None:
        """bash 执行 Python 脚本，成功后返回文件路径"""
        long_output = "\n".join([
            "Running script...",
            "Processing slide 1 of 10",
            "Processing slide 2 of 10",
            "Processing slide 3 of 10",
            "Processing slide 4 of 10",
            "Processing slide 5 of 10",
            "Processing slide 6 of 10",
            "Processing slide 7 of 10",
            "Processing slide 8 of 10",
            "Processing slide 9 of 10",
            "Processing slide 10 of 10",
            "文件已生成: /home/user/.whaleclaw/workspace/tmp/report_V1.pptx",
            "exit:0",
        ])
        result = _ok(long_output, "bash")
        assert len(result) <= 300
        assert "/home/user/.whaleclaw/workspace/tmp/report_V1.pptx" in result
        assert result.startswith("\u2713")

    def test_bash_with_exit_code_1_still_success(self) -> None:
        """ToolResult.success=True 但 exit:1，不应被当成失败处理"""
        output = "\n".join([
            "Some warning output",
            "Another warning line",
            "More verbose output here that makes this longer than 240 chars total length",
            "Even more output",
            "And even more",
            "exit:1",
        ])
        result = _ok(output, "bash")
        # success=True 的输出绝不能含 [ERROR]，无论是否触发压缩
        assert "[ERROR]" not in result

    def test_bash_with_exit_code_1_long_triggers_compression(self) -> None:
        """长输出 + exit:1：触发压缩路径后也不能产生 [ERROR]"""
        output = "\n".join(
            [f"warning: something at line {i}" for i in range(30)] + ["exit:1"]
        )
        assert len(output) > 240
        result = _ok(output, "bash")
        assert "[ERROR]" not in result
        assert result.startswith("\u2713")

    def test_bash_no_success_keyword_falls_back_to_head_tail(self) -> None:
        """无任何成功关键词和路径 → 保底取首行+尾行"""
        lines = [f"output line {i}" for i in range(20)]
        long_output = "\n".join(lines)
        assert len(long_output) > 240
        result = _ok(long_output, "bash")
        assert len(result) <= 300
        # 应含首行内容
        assert "output line 0" in result

    def test_result_never_exceeds_300_chars(self) -> None:
        """无论多长的输出，结果都 <= 300 字符"""
        huge = "\n".join([
            "Successfully created /very/long/absolute/path/to/a/deeply/nested/directory/structure/file_V1.pptx",
            "Successfully created /very/long/absolute/path/to/a/deeply/nested/directory/structure/file_V2.pptx",
            "Successfully created /very/long/absolute/path/to/a/deeply/nested/directory/structure/file_V3.pptx",
            "exit:0",
        ])
        result = _ok(huge, "bash")
        assert len(result) <= 300

    def test_exit_code_without_success_keyword(self) -> None:
        """只有 exit:0，无 successfully 等词 → 保底但带 exit hint"""
        lines = [f"verbose info line {i}" for i in range(15)]
        lines.append("exit:0")
        output = "\n".join(lines)
        assert len(output) > 240
        result = _ok(output, "bash")
        assert "exit:0" in result
        assert len(result) <= 300

    def test_progress_bar_unicode_filtered(self) -> None:
        """━━━ 进度条行被过滤；确保输出够长以触发压缩路径"""
        # 加足够多的 Downloading 行让总长 > 240，保证进入压缩分支
        pkg_lines = []
        for i in range(8):
            pkg_lines.append(f"Collecting package-{i}")
            pkg_lines.append(f"  Downloading pkg{i}-1.0.whl (50 kB)")
            pkg_lines.append(f"     \u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501 50.0/50.0 kB eta 0:00:00")
        pkg_lines.append("Successfully installed " + " ".join(f"package-{i}-1.0" for i in range(8)))
        pkg_lines.append("exit:0")
        output = "\n".join(pkg_lines)
        assert len(output) > 240, "test setup: output must be long enough to trigger compression"
        result = _ok(output, "bash")
        # 压缩后进度条行不应出现
        assert "\u2501" not in result
        assert "eta" not in result
        assert "Successfully installed" in result

    def test_dash_separator_lines_filtered(self) -> None:
        """--- 分隔线被过滤；确保输出够长以触发压缩路径"""
        output = "\n".join([
            "Build started",
            "-------------------------------",
            "Compiling module A",
            "Compiling module B",
            "Compiling module C",
            "Compiling module D",
            "-------------------------------",
            "Build successful: /tmp/build/output.so",
            "Running tests: 42 passed, 0 failed",
            "Generating report: /tmp/build/report.html",
            "exit:0",
        ] + [f"note: processed file_{i}.cpp" for i in range(10)])
        assert len(output) > 240
        result = _ok(output, "bash")
        assert "---" not in result
        assert "/tmp/build/output.so" in result


# ──────────────────────────────────────────────────────────────────────────────
# file_write / file_edit
# ──────────────────────────────────────────────────────────────────────────────

class TestFileWriteOutput:
    def test_long_file_write_only_first_line(self) -> None:
        """file_write 长输出只取首行，通常是摘要行"""
        first_line = "文件已写入: /home/user/.whaleclaw/workspace/tmp/script.py (4567 字节)"
        output = first_line + "\n" + "x" * 500
        result = _ok(output, "file_write")
        assert first_line in result
        assert len(result) <= 245 + 3  # "✓ " + 240 + possible truncation

    def test_file_edit_long_output_only_first_line(self) -> None:
        first_line = "文件编辑成功: /tmp/config.json"
        output = first_line + "\n" + "\n".join([f"diff line {i}" for i in range(50)])
        result = _ok(output, "file_edit")
        assert first_line in result
        assert result.startswith("\u2713")

    def test_file_write_result_always_short(self) -> None:
        """无论写了多大的文件报告多长，结果都短"""
        # 超长首行（> 240 字符）也被截断
        very_long_first = "文件已写入: " + "/deep/path/" * 30 + "file.py (99999 字节)"
        result = _ok(very_long_first + "\nextra info", "file_write")
        assert len(result) <= 245


# ──────────────────────────────────────────────────────────────────────────────
# browser
# ──────────────────────────────────────────────────────────────────────────────

class TestBrowserOutput:
    def test_search_images_extracts_paths(self) -> None:
        output = "\n".join([
            "搜索完成，共找到 3 张图片",
            "已下载: /home/user/.whaleclaw/downloads/city_skyline.jpg",
            "已下载: /home/user/.whaleclaw/downloads/tech_abstract.jpg",
            "已下载: /home/user/.whaleclaw/downloads/business_meeting.jpg",
            "图片尺寸信息: 1920x1080, 1280x720, 800x600",
        ])
        result = _ok(output, "browser")
        # 路径应保留（无论是否触发压缩）
        assert "/home/user/.whaleclaw/downloads/city_skyline.jpg" in result

    def test_search_images_max_3_paths(self) -> None:
        """最多取 3 条路径行"""
        lines = [f"已下载: /downloads/img_{i:02d}.jpg" for i in range(10)]
        output = "\n".join(["搜索完成"] + lines)
        result = _ok(output, "browser")
        # 只保留前 3 条路径
        assert result.count("/downloads/img_") <= 3

    def test_browser_navigate_with_url(self) -> None:
        output = "\n".join([
            "页面加载完成",
            "URL: https://example.com/page",
            "标题: Example Domain",
            "内容长度: 1234 字符",
            "截图已保存: /tmp/screenshot_001.png",
        ] + [f"DOM节点 {i}: div.class-{i}" for i in range(30)])
        result = _ok(output, "browser")
        assert len(result) <= 300
        assert result.startswith("\u2713")

    def test_browser_no_path_falls_back_to_first_line(self) -> None:
        """无路径/URL 时保底取首行"""
        output = "\n".join([f"status info line {i}" for i in range(20)])
        assert len(output) > 240
        result = _ok(output, "browser")
        assert "status info line 0" in result
        assert len(result) <= 300


# ──────────────────────────────────────────────────────────────────────────────
# 通用工具（非 bash/file_write/browser）
# ──────────────────────────────────────────────────────────────────────────────

class TestGenericToolOutput:
    def test_generic_with_path_lines(self) -> None:
        output = "\n".join([
            "操作完成",
            "/home/user/.whaleclaw/workspace/output.xlsx",
            "/home/user/.whaleclaw/workspace/output_backup.xlsx",
            "附加信息行" * 20,
        ])
        result = _ok(output, "xlsx_edit")
        assert "/home/user/.whaleclaw/workspace/output.xlsx" in result
        assert len(result) <= 300

    def test_generic_max_2_path_lines(self) -> None:
        lines = ["操作完成"] + [f"/path/to/file_{i}.txt" for i in range(5)]
        output = "\n".join(lines + ["extra info" * 30])
        result = _ok(output, "docx_edit")
        # 最多保留 2 条路径
        path_count = result.count("/path/to/file_")
        assert path_count <= 2

    def test_generic_no_path_head_only(self) -> None:
        lines = [f"verbose result info {i}" for i in range(20)]
        output = "\n".join(lines)
        assert len(output) > 240
        result = _ok(output, "some_tool")
        assert "verbose result info 0" in result
        assert len(result) <= 300


# ──────────────────────────────────────────────────────────────────────────────
# 失败路径不受影响
# ──────────────────────────────────────────────────────────────────────────────

class TestFailureOutputUnchanged:
    def test_module_not_found_has_error_prefix(self) -> None:
        result = _err("ModuleNotFoundError: No module named 'pptx'")
        assert result.startswith("[ERROR]")
        assert "ModuleNotFoundError" in result

    def test_module_not_found_has_diagnosis(self) -> None:
        result = _err("ModuleNotFoundError: No module named 'pptx'")
        assert "[DIAGNOSIS]" in result

    def test_short_timeout_error_passthrough(self) -> None:
        result = _err("[ERROR] command timeout (30s)")
        assert "[ERROR]" in result
        assert "timeout" in result

    def test_permission_denied_diagnosis(self) -> None:
        result = _err("Permission denied: /etc/hosts")
        assert "[DIAGNOSIS]" in result
        assert "[ERROR]" in result

    def test_traceback_error_extracts_last_line(self) -> None:
        tb = "\n".join([
            "Traceback (most recent call last):",
            "  File '/tmp/script.py', line 42, in <module>",
            "    import missing_lib",
            "ImportError: No module named missing_lib",
        ])
        result = _err("", tb)
        assert "ImportError" in result
        assert len(result) <= 400

    def test_success_true_never_has_error_prefix(self) -> None:
        """success=True 的结果绝不能含 [ERROR]"""
        for tool in ("bash", "file_write", "browser", "xlsx_edit"):
            output = "some " * 60 + "output"
            result = _ok(output, tool)
            assert "[ERROR]" not in result, f"tool={tool} produced [ERROR] for success=True"


class TestNanoBananaArgparseRetryBoundary:
    def test_transient_cli_usage_error_matches_nano_banana_argparse_error(self) -> None:
        result = ToolResult(
            success=False,
            output="[stderr]\nusage: test_nano_banana_2.py [-h]\nerror: unrecognized arguments: --bad",
            error="usage: test_nano_banana_2.py [-h]\nerror: unrecognized arguments: --bad",
        )

        assert is_transient_cli_usage_error(result) is True

    def test_transient_cli_usage_error_rejects_non_argparse_runtime_failure(self) -> None:
        result = ToolResult(
            success=False,
            output="httpx.ReadTimeout: request timed out",
            error="httpx.ReadTimeout: request timed out",
        )

        assert is_transient_cli_usage_error(result) is False

    def test_transient_cli_usage_error_rejects_other_script_usage_banner(self) -> None:
        result = ToolResult(
            success=False,
            output="usage: other_script.py [-h]\nerror: unrecognized arguments: --bad",
            error="usage: other_script.py [-h]\nerror: unrecognized arguments: --bad",
        )

        assert is_transient_cli_usage_error(result) is False

    @pytest.mark.asyncio
    async def test_nano_banana_direct_script_mismatch_retries_once_with_project_python(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake_python = tmp_path / "python.exe"
        fake_python.write_text("", encoding="utf-8")
        monkeypatch.setattr(tool_exec_mod, "_PROJECT_PYTHON", fake_python)

        first_result = ToolResult(
            success=False,
            output="[stderr]\nfrom: command not found\nimport: command not found\n[exit_code: 127]",
            error="from: command not found\nimport: command not found",
        )
        retry_result = ToolResult(success=True, output="fixed and rerun")
        bash_tool = _DummyBashTool(result=retry_result)
        registry = ToolRegistry()
        registry.register(bash_tool)
        tc = ToolCall(
            id="tc_1",
            name="bash",
            arguments={"command": "/tmp/test_nano_banana_2.py --mode edit"},
        )

        retried = await tool_exec_mod._maybe_retry_python_script_invocation(  # noqa: SLF001
            registry,
            tc,
            first_result,
        )

        assert retried is retry_result
        assert len(bash_tool.calls) == 1
        assert bash_tool.calls[0]["command"] == f"{fake_python} /tmp/test_nano_banana_2.py --mode edit"

    def test_normalize_nano_banana_command_autofixes_known_bad_flags(self) -> None:
        command = (
            "/tmp/test_nano_banana_2.py --mode text2image --api-base https://example.com "
            "--size 4:3 --prompt 'hello world'"
        )

        normalized = tool_exec_mod._normalize_nano_banana_command(command)  # noqa: SLF001

        assert "--mode text" in normalized
        assert "--base-url https://example.com" in normalized
        assert "--aspect-ratio 4:3" in normalized
        assert "--api-base" not in normalized
        assert "--size 4:3" not in normalized
        assert "text2image" not in normalized

    @pytest.mark.asyncio
    async def test_execute_registered_tool_splits_multi_nano_bash_command_in_parallel(self) -> None:
        registry = ToolRegistry()
        bash_tool = _SleepingBashTool()
        registry.register(bash_tool)
        tc = ToolCall(
            id="tc_parallel",
            name="bash",
            arguments={
                "command": (
                    "/tmp/test_nano_banana_2.py --mode text --prompt a && "
                    "/tmp/test_nano_banana_2.py --mode text --prompt b && "
                    "/tmp/test_nano_banana_2.py --mode text --prompt c"
                ),
                "timeout": 120,
            },
        )

        result = await tool_exec_mod._execute_registered_tool(registry, tc)  # noqa: SLF001

        assert result.success is True
        assert len(bash_tool.commands) == 3
        assert max(bash_tool.starts) - min(bash_tool.starts) < 0.05
        assert "[并发生图 1]" in result.output
        assert "[并发生图 3]" in result.output


# ──────────────────────────────────────────────────────────────────────────────
# 结果长度上限保证（所有工具、各类输出）
# ──────────────────────────────────────────────────────────────────────────────

class TestOutputLengthGuarantee:
    @pytest.mark.parametrize("tool", ["bash", "file_write", "file_edit", "browser", "xlsx_edit", "docx_edit", ""])
    def test_max_300_chars_for_all_tools(self, tool: str) -> None:
        huge = "\n".join([
            "Successfully created and saved the file",
            "/very/long/path/" + "subdir/" * 10 + "file.pptx",
            "/another/very/long/path/" + "nested/" * 10 + "output.docx",
            "exit:0",
        ] + [f"extra verbose line {i} with lots of content here" for i in range(30)])
        result = _ok(huge, tool)
        assert len(result) <= 300, f"tool={tool!r}: result length {len(result)} > 300"
