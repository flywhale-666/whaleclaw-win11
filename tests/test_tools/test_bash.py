"""Tests for the bash tool."""

from __future__ import annotations

from pathlib import Path

import pytest

from whaleclaw.tools import bash as bash_mod
from whaleclaw.tools.bash import BashTool


@pytest.fixture()
def tool() -> BashTool:
    return BashTool()


class _FakeProc:
    def __init__(self) -> None:
        self.pid = 123
        self.returncode = 0

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"ok", b""


@pytest.mark.asyncio
async def test_echo(tool: BashTool) -> None:
    result = await tool.execute(command="echo hello")
    assert result.success
    assert "hello" in result.output


@pytest.mark.asyncio
async def test_exit_code(tool: BashTool) -> None:
    result = await tool.execute(command="exit 1")
    assert not result.success
    assert "exit_code: 1" in result.output


@pytest.mark.asyncio
async def test_empty_command(tool: BashTool) -> None:
    result = await tool.execute(command="")
    assert not result.success
    assert result.error == "命令为空"


@pytest.mark.asyncio
async def test_dangerous_command(tool: BashTool) -> None:
    result = await tool.execute(command="rm -rf /")
    assert not result.success
    assert "危险命令" in (result.error or "")


@pytest.mark.asyncio
async def test_timeout(tool: BashTool) -> None:
    result = await tool.execute(command="sleep 10", timeout=1)
    assert not result.success
    assert "超时" in (result.error or "")


@pytest.mark.asyncio
async def test_nano_banana_timeout_floor_is_300(
    tool: BashTool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, float] = {}

    async def fake_spawn_process(
        command: str,
        *,
        env: dict[str, str],  # noqa: ARG001
    ) -> _FakeProc:
        assert "test_nano_banana_2.py" in command
        return _FakeProc()

    async def fake_wait_for(awaitable, timeout):  # type: ignore[no-untyped-def]
        captured["timeout"] = timeout
        return await awaitable

    monkeypatch.setattr(BashTool, "_spawn_process", staticmethod(fake_spawn_process))
    monkeypatch.setattr(bash_mod.asyncio, "wait_for", fake_wait_for)

    result = await tool.execute(command="/tmp/test_nano_banana_2.py --mode text", timeout=120)

    assert result.success is True
    assert captured["timeout"] == 300


@pytest.mark.asyncio
async def test_background_returns_session_id(tool: BashTool) -> None:
    result = await tool.execute(command="sleep 1", background=True)
    assert result.success
    assert "session_id:" in result.output


@pytest.mark.asyncio
async def test_control_chars_are_stripped(tool: BashTool) -> None:
    result = await tool.execute(command="\x18echo hello\x18")
    assert result.success
    assert "hello" in result.output


@pytest.mark.asyncio
async def test_only_control_chars_becomes_empty(tool: BashTool) -> None:
    result = await tool.execute(command="\x10\x18\x00")
    assert not result.success
    assert result.error == "命令为空"


def test_prefer_project_python_rewrites_bare_python(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_python = tmp_path / "python3.12"
    fake_python.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(bash_mod, "_PROJECT_PYTHON", fake_python)

    rewritten = bash_mod._prefer_project_python("python3 /tmp/a.py && python -V")
    expected = f"{fake_python} /tmp/a.py && {fake_python} -V"
    assert rewritten == expected


def test_prefer_project_python_rewrites_direct_python_script(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_python = tmp_path / "python3.12"
    fake_python.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(bash_mod, "_PROJECT_PYTHON", fake_python)

    rewritten = bash_mod._prefer_project_python_for_direct_script(
        "/tmp/test_nano_banana_2.py --mode edit"
    )

    assert rewritten == f"{fake_python} /tmp/test_nano_banana_2.py --mode edit"


def test_prefer_project_python_rewrites_env_prefixed_direct_python_script(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_python = tmp_path / "python3.12"
    fake_python.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(bash_mod, "_PROJECT_PYTHON", fake_python)

    rewritten = bash_mod._prefer_project_python_for_direct_script(
        "NANO_BANANA_API_KEY='x' /tmp/test_nano_banana_2.py --mode edit"
    )

    assert rewritten == (
        f"NANO_BANANA_API_KEY='x' {fake_python} /tmp/test_nano_banana_2.py --mode edit"
    )


def test_decode_output_bytes_supports_gbk() -> None:
    text = "缺少 API key，请通过 --api-key 提供"
    decoded = bash_mod._decode_output_bytes(text.encode("gbk"))
    assert decoded == text





def test_rewrite_windows_command_translates_ls_head_probe() -> None:
    rewritten = bash_mod._rewrite_windows_command(
        r"ls -la C:\Users\Administrator\.whaleclaw\downloads\ 2>/dev/null | head -30"
    )

    assert rewritten == (
        "Get-ChildItem -Force -LiteralPath "
        r"C:\Users\Administrator\.whaleclaw\downloads\ -ErrorAction SilentlyContinue | Select-Object -First 30"
    )


def test_rewrite_windows_command_translates_fallback_chain() -> None:
    rewritten = bash_mod._rewrite_windows_command(
        r"ls -la ~/.whaleclaw/downloads/ 2>/dev/null || ls -la $HOME/.whaleclaw/downloads/ 2>/dev/null || dir $HOME\\.whaleclaw\\downloads\\ 2>$null"
    )

    assert "if (-not $__wc_done) {" in rewritten
    assert "Get-ChildItem -Force -LiteralPath ~/.whaleclaw/downloads/ -ErrorAction SilentlyContinue" in rewritten
    assert "Get-ChildItem -Force -LiteralPath $HOME/.whaleclaw/downloads/ -ErrorAction SilentlyContinue" in rewritten
    assert r"Get-ChildItem -Force -LiteralPath $HOME\\.whaleclaw\\downloads\\ -ErrorAction SilentlyContinue" in rewritten


def test_rewrite_windows_command_translates_dir_findstr() -> None:
    rewritten = bash_mod._rewrite_windows_command(
        r'dir /b "C:\Users\Administrator\.whaleclaw\downloads" 2>nul | findstr /i "quanzhou kaiyuan temple ancient street food"'
    )

    assert rewritten == (
        'Get-ChildItem -Name -LiteralPath "C:\\Users\\Administrator\\.whaleclaw\\downloads" -ErrorAction SilentlyContinue '
        "| Select-String -SimpleMatch -CaseSensitive:$false -Pattern 'quanzhou kaiyuan temple ancient street food'"
    )


def test_rewrite_windows_command_translates_dir_glob() -> None:
    rewritten = bash_mod._rewrite_windows_command(
        r"dir C:\Users\Administrator\.whaleclaw\downloads\*.jpg 2>nul"
    )

    assert rewritten == (
        r"Get-ChildItem -Path C:\Users\Administrator\.whaleclaw\downloads\*.jpg -ErrorAction SilentlyContinue"
    )



def test_normalize_project_python_aliases_rewrites_windows_embedded_python(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_python = tmp_path / "python.exe"
    fake_python.write_text("", encoding="utf-8")
    monkeypatch.setattr(bash_mod, "_PROJECT_PYTHON", fake_python)

    rewritten = bash_mod._normalize_project_python_aliases(
        r".\python\python.exe gen_ppt.py"
    )
    rewritten_doubled = bash_mod._normalize_project_python_aliases(
        r".\\python\\python.exe gen_ppt.py"
    )

    assert rewritten == f"{fake_python} gen_ppt.py"
    assert rewritten_doubled == f"{fake_python} gen_ppt.py"


def test_rewrite_windows_command_translates_dir_findstr_flag_after_path() -> None:
    rewritten = bash_mod._rewrite_windows_command(
        r'dir "C:\Users\Administrator\.whaleclaw\downloads" /b 2>nul | findstr /i "?? ?? ??"'
    )

    assert rewritten == (
        'Get-ChildItem -Name -LiteralPath "C:\\Users\\Administrator\\.whaleclaw\\downloads" -ErrorAction SilentlyContinue '
        "| Select-String -SimpleMatch -CaseSensitive:$false -Pattern '?? ?? ??'"
    )


def test_rewrite_windows_command_translates_cd_and_python() -> None:
    rewritten = bash_mod._rewrite_windows_command(
        r'cd C:\Users\Administrator\.whaleclaw\workspace\tmp && C:\repo\python.exe gen_ppt_final.py'
    )

    assert rewritten == (
        r'Set-Location -LiteralPath C:\Users\Administrator\.whaleclaw\workspace\tmp; C:\repo\python.exe gen_ppt_final.py'
    )


def test_rewrite_windows_command_translates_cd_and_dir_glob() -> None:
    rewritten = bash_mod._rewrite_windows_command(
        r'cd C:\Users\Administrator\.whaleclaw\workspace\tmp && dir gen_ppt*.py'
    )

    assert rewritten == (
        r'Set-Location -LiteralPath C:\Users\Administrator\.whaleclaw\workspace\tmp; Get-ChildItem -Path gen_ppt*.py -ErrorAction SilentlyContinue'
    )



def test_build_permission_retry_command_rewrites_locked_output(tmp_path: Path) -> None:
    script = tmp_path / "gen_ppt_final.py"
    script.write_text(
        'output_path = r"C:\\Users\\Administrator\\Desktop\\locked.pptx"\nprint(output_path)\n',
        encoding="utf-8",
    )
    command = f'cd {tmp_path} && C:\\repo\\python.exe gen_ppt_final.py'
    combined_output = "PermissionError: [Errno 13] Permission denied: 'C:\\Users\\Administrator\\Desktop\\locked.pptx'"

    before = {p.name for p in tmp_path.glob("gen_ppt_final_retry_unlock_*.py")}

    rewritten = bash_mod._build_permission_retry_command(command, combined_output)

    assert rewritten is not None
    assert "gen_ppt_final_retry_unlock_" in rewritten
    created = [p for p in tmp_path.glob("gen_ppt_final_retry_unlock_*.py") if p.name not in before]
    assert created
    retry_script = created[0]
    retry_text = retry_script.read_text(encoding="utf-8")
    assert "locked.pptx" not in retry_text
    assert str(Path.home() / ".whaleclaw" / "workspace" / "tmp") in retry_text
