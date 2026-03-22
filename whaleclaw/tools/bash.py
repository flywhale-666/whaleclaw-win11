"""Bash command execution tool."""

from __future__ import annotations

import asyncio
import locale
import os
import re
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from whaleclaw.tools.base import Tool, ToolDefinition, ToolParameter, ToolResult
from whaleclaw.tools.process_registry import register_background_process

_DANGEROUS_PATTERNS = [
    re.compile(r"\brm\s+-rf\s+/\s*$"),
    re.compile(r"\bmkfs\b"),
    re.compile(r"\bdd\s+if=/dev/zero\b"),
    re.compile(r":\(\)\s*\{\s*:\|:&\s*\}\s*;"),
]

_MAX_OUTPUT = 50_000

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_PROJECT_PYTHON_CANDIDATES = (
    _PROJECT_ROOT / "python" / "python.exe",
    _PROJECT_ROOT / "python" / "bin" / "python3.12",
    _PROJECT_ROOT / "python" / "bin" / "python3",
)
_PROJECT_PYTHON = next((path for path in _PROJECT_PYTHON_CANDIDATES if path.is_file()), None)
_PROJECT_PYTHON_BIN = _PROJECT_PYTHON.parent if _PROJECT_PYTHON is not None else None
_NANO_BANANA_SCRIPT_RE = re.compile(r"test_nano_banana(?:_\d+)?\.py", re.IGNORECASE)
_NANO_BANANA_SUCCESS_RE = re.compile(r"(?:文生图|图生图)成功:\s*\S+\.png", re.IGNORECASE)
_PYTHON_CMD_RE = re.compile(r"(?<![\w./-])(python3|python)(?=\s|$)")
# LLM 有时拼出 /path/to/./python/bin/python3.12 这类含 ./ 的错误路径
_BROKEN_PROJECT_PYTHON_RE = re.compile(
    r"(?P<prefix>[\"']?)(?P<root>[^\s\"']*?)"
    r"[\\/]\.[\\/]python[\\/](?:bin[\\/])?python(?:3(?:\.12)?)?"
    r"(?:\.exe)?(?P=prefix)",
    re.IGNORECASE,
)


def _fix_broken_python_path(command: str) -> str:
    """将 LLM 拼错的 ./python/bin/python3.12 路径修正为内嵌 Python 绝对路径。"""
    if _PROJECT_PYTHON is None:
        return command
    if "/./python/" not in command and "\\.\\python\\" not in command:
        return command
    return _BROKEN_PROJECT_PYTHON_RE.sub(lambda _m: str(_PROJECT_PYTHON), command)
_DIRECT_PY_SCRIPT_RE = re.compile(
    r"^"
    r"(?P<prefix>(?:[A-Za-z_][A-Za-z0-9_]*=(?:'[^']*'|\"[^\"]*\"|[^\s]+)\s+)*)"
    r"(?P<script>(?:~|/|\./|\.\./|[A-Za-z]:[\\/]|\\\\)[^\s;&|]+\.py)"
    r"(?P<suffix>(?:\s+.*)?)$"
)
_TMP_ALIAS_ROOT = (Path.home() / ".whaleclaw" / "workspace" / "tmp").resolve()
_HOME_ALIAS_ROOT = _TMP_ALIAS_ROOT.parent.parent
_WINDOWS_ROOT_HOME_RE = re.compile(
    r"(?i)(?P<prefix>(?:^|[\s'\"=;]))(?P<path>[A-Z]:\\root\\\.whaleclaw(?:\\[^\s'\";|&]*)?)"
)

_CREDENTIALS_DIR = Path.home() / ".whaleclaw" / "credentials"
_CREDENTIAL_ENV_MAP: dict[str, str] = {
    "nano_banana_api_key.txt": "NANO_BANANA_API_KEY",
    "tavily_api_key.txt": "TAVILY_API_KEY",
}


def _inject_saved_credentials(env: dict[str, str]) -> None:
    """Auto-inject saved credential files as environment variables.

    Ensures scripts can always find API keys regardless of which skill
    context the LLM is operating in.
    """
    if not _CREDENTIALS_DIR.is_dir():
        return
    for filename, env_var in _CREDENTIAL_ENV_MAP.items():
        if env.get(env_var):
            continue
        path = _CREDENTIALS_DIR / filename
        try:
            if path.is_file():
                value = path.read_text(encoding="utf-8").strip()
                if value:
                    env[env_var] = value
        except Exception:
            pass


class _CompatStreamReader:
    def __init__(self, stream: Any) -> None:
        self._stream = stream

    async def read(self, size: int = -1) -> bytes:
        return await asyncio.to_thread(self._stream.read, size)


class _CompatStreamWriter:
    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._closed = False

    def is_closing(self) -> bool:
        return self._closed or self._stream.closed

    def write(self, data: bytes) -> None:
        self._stream.write(data)

    async def drain(self) -> None:
        await asyncio.to_thread(self._stream.flush)

    def close(self) -> None:
        self._closed = True
        self._stream.close()


class _CompatProcess:
    def __init__(self, proc: subprocess.Popen[bytes]) -> None:
        self._proc = proc
        self.stdin = (
            _CompatStreamWriter(proc.stdin) if proc.stdin is not None else None
        )
        self.stdout = (
            _CompatStreamReader(proc.stdout) if proc.stdout is not None else None
        )
        self.stderr = (
            _CompatStreamReader(proc.stderr) if proc.stderr is not None else None
        )

    @property
    def pid(self) -> int:
        return self._proc.pid

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        stdout, stderr = await asyncio.to_thread(self._proc.communicate)
        return stdout or b"", stderr or b""

    async def wait(self) -> int:
        return await asyncio.to_thread(self._proc.wait)

    def terminate(self) -> None:
        self._proc.terminate()

    def kill(self) -> None:
        self._proc.kill()


def _strip_control_chars(text: str) -> str:
    """Remove ASCII control characters except LF/TAB/CR."""
    return "".join(
        ch for ch in text
        if ch in ("\n", "\t", "\r") or (ord(ch) >= 32 and ord(ch) != 127)
    )


def _prefer_project_python(command: str) -> str:
    """Rewrite bare python/python3 to project-embedded python when available."""
    if _PROJECT_PYTHON is None:
        return command
    return _PYTHON_CMD_RE.sub(lambda _match: str(_PROJECT_PYTHON), command)


def _normalize_project_python_aliases(command: str) -> str:
    if _PROJECT_PYTHON is None:
        return command
    patterns = (
        re.compile(r"(?<!\S)(?:\.?(?:[\\/]{1,2})?)?python(?:[\\/]{1,2})python\.exe(?=\s|$)"),
        re.compile(r"(?<!\S)(?:\.?(?:[\\/]{1,2})?)?python(?:[\\/]{1,2})bin(?:[\\/]{1,2})python3\.12(?=\s|$)"),
        re.compile(r"(?<!\S)(?:\.?(?:[\\/]{1,2})?)?python(?:[\\/]{1,2})bin(?:[\\/]{1,2})python3(?=\s|$)"),
        re.compile(r"(?<!\S)(?:\.?(?:[\\/]{1,2})?)?python(?:[\\/]{1,2})bin(?:[\\/]{1,2})python(?=\s|$)"),
    )
    normalized = command
    for pattern in patterns:
        normalized = pattern.sub(lambda _match: str(_PROJECT_PYTHON), normalized)
    return normalized


def _prefer_project_python_for_direct_script(command: str) -> str:
    """Rewrite a direct ``script.py`` invocation to use the embedded Python."""
    if _PROJECT_PYTHON is None:
        return command
    match = _DIRECT_PY_SCRIPT_RE.match(command.strip())
    if match is None:
        return command
    prefix = match.group("prefix")
    script = match.group("script")
    suffix = match.group("suffix") or ""
    return f"{prefix}{_PROJECT_PYTHON} {script}{suffix}"


def _resolve_command_timeout(command: str, requested_timeout: int) -> int:
    """Apply command-specific timeout floors.

    Checks active skill hooks first; falls back to built-in nano-banana pattern.
    """
    from whaleclaw.agent.helpers.tool_execution import _active_skill_hooks

    if _active_skill_hooks is not None:
        pattern = getattr(_active_skill_hooks, "long_running_script_pattern", None)
        if pattern is not None and pattern.search(command):
            floor = getattr(_active_skill_hooks, "long_running_timeout_seconds", 300)
            return max(requested_timeout, floor)
    if _NANO_BANANA_SCRIPT_RE.search(command):
        return max(requested_timeout, 300)
    return requested_timeout


def _normalize_tmp_aliases(command: str) -> str:
    replacements = {
        "/private/tmp/": str(_TMP_ALIAS_ROOT) + os.sep,
        "/tmp/": str(_TMP_ALIAS_ROOT) + os.sep,
    }
    normalized = command
    for source, target in replacements.items():
        normalized = normalized.replace(source, target)
    return normalized


def _normalize_home_aliases(command: str) -> str:
    home_str = str(Path.home())
    replacements = {
        "~/.whaleclaw/workspace/tmp": str(_TMP_ALIAS_ROOT),
        "~/.whaleclaw": str(_HOME_ALIAS_ROOT),
        "/root/.whaleclaw/workspace/tmp": str(_TMP_ALIAS_ROOT),
        "/root/.whaleclaw": str(_HOME_ALIAS_ROOT),
    }
    normalized = command
    for source, target in replacements.items():
        normalized = normalized.replace(source, target)

    if os.name == "nt":
        cmd_env_vars = {
            "%USERPROFILE%": home_str,
            "%HOME%": home_str,
            "%HOMEDRIVE%%HOMEPATH%": home_str,
            "%TEMP%": os.environ.get("TEMP", str(Path(home_str) / "AppData" / "Local" / "Temp")),
            "%TMP%": os.environ.get("TMP", os.environ.get("TEMP", "")),
        }
        for var, value in cmd_env_vars.items():
            if value and var in normalized:
                normalized = normalized.replace(var, value)

    def _replace_windows_root_home(match: re.Match[str]) -> str:
        prefix = match.group("prefix")
        raw_path = match.group("path")
        suffix = raw_path[len("C:\\root\\.whaleclaw") :].replace("\\", os.sep)
        return f"{prefix}{_HOME_ALIAS_ROOT}{suffix}"

    return _WINDOWS_ROOT_HOME_RE.sub(_replace_windows_root_home, normalized)


def _decode_output_bytes(data: bytes) -> str:
    encodings: list[str] = ["utf-8"]
    preferred = locale.getpreferredencoding(False)
    if preferred and preferred.lower() not in {enc.lower() for enc in encodings}:
        encodings.append(preferred)
    for fallback in ("gb18030", "gbk"):
        if fallback not in {enc.lower() for enc in encodings}:
            encodings.append(fallback)
    for encoding in encodings:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


_WINDOWS_NULL_RE = re.compile(r"\s+2>(?:/dev/null|nul|\$null)\s*$", re.IGNORECASE)
_WINDOWS_LS_HEAD_RE = re.compile(
    r"^ls(?:\s+-[A-Za-z]+)*\s+(?P<path>.+?)(?:\s+2>(?:/dev/null|nul|\$null))?(?:\s*\|\s*head\s+-(?P<count>\d+))?$",
    re.IGNORECASE,
)
_WINDOWS_DIR_FINDSTR_RE = re.compile(
    r'^dir\s+(?:(?P<flag1>/b)\s+)?"(?P<path>[^"]+)"(?:\s+(?P<flag2>/b))?(?:\s+2>(?:nul|\$null))?\s*\|\s*findstr\s+/i\s+"(?P<pattern>[^"]+)"$',
    re.IGNORECASE,
)
_WINDOWS_DIR_RE = re.compile(
    r"^dir(?:\s+/b)?\s+(?P<path>.+?)(?:\s+2>(?:nul|\$null))?$",
    re.IGNORECASE,
)


def _strip_windows_null_redirect(command: str) -> str:
    return _WINDOWS_NULL_RE.sub("", command).strip()


def _translate_windows_probe_segment(segment: str) -> str | None:
    clean = segment.strip()
    ls_match = _WINDOWS_LS_HEAD_RE.fullmatch(clean)
    if ls_match is not None:
        path_expr = ls_match.group("path").strip()
        count = ls_match.group("count")
        translated = (
            "Get-ChildItem -Force -LiteralPath "
            f"{path_expr} -ErrorAction SilentlyContinue"
        )
        if count:
            translated += f" | Select-Object -First {count}"
        return translated

    dir_findstr_match = _WINDOWS_DIR_FINDSTR_RE.fullmatch(clean)
    if dir_findstr_match is not None:
        path_expr = dir_findstr_match.group("path")
        pattern = dir_findstr_match.group("pattern").replace("'", "''")
        return (
            f'Get-ChildItem -Name -LiteralPath "{path_expr}" -ErrorAction SilentlyContinue '
            f"| Select-String -SimpleMatch -CaseSensitive:$false -Pattern '{pattern}'"
        )

    dir_match = _WINDOWS_DIR_RE.fullmatch(clean)
    if dir_match is not None:
        path_expr = _strip_windows_null_redirect(dir_match.group("path"))
        if any(ch in path_expr for ch in "*?"):
            return f"Get-ChildItem -Path {path_expr} -ErrorAction SilentlyContinue"
        return f"Get-ChildItem -Force -LiteralPath {path_expr} -ErrorAction SilentlyContinue"

    return None




def _translate_windows_and_chain(command: str) -> str | None:
    parts = [part.strip() for part in command.split("&&")]
    if len(parts) <= 1:
        return None
    first = parts[0]
    rest = " && ".join(parts[1:]).strip()
    cd_match = re.fullmatch(r"cd\s+(?P<path>.+)", first, re.IGNORECASE)
    if cd_match is None or not rest:
        return None
    target = cd_match.group("path").strip()
    translated_rest = _translate_windows_probe_segment(rest)
    if translated_rest is not None:
        return f"Set-Location -LiteralPath {target}; {translated_rest}"
    return f"Set-Location -LiteralPath {target}; {rest}"
def _translate_windows_probe_chain(command: str) -> str | None:
    parts = [part.strip() for part in command.split("||")]
    if len(parts) <= 1:
        return None
    translated_parts: list[str] = []
    for part in parts:
        translated = _translate_windows_probe_segment(part)
        if translated is None:
            return None
        translated_parts.append(translated)
    lines = ["$__wc_done = $false"]
    for translated in translated_parts:
        lines.append("if (-not $__wc_done) {")
        lines.append(f"  $__wc_out = & {{ {translated} }} | Out-String -Width 4096")
        lines.append("  if ($?) {")
        lines.append("    if ($__wc_out) { [Console]::Out.Write($__wc_out) }")
        lines.append("    $__wc_done = $true")
        lines.append("  }")
        lines.append("}")
    lines.append("if (-not $__wc_done) { exit 1 }")
    return "\n".join(lines)


_SCREENSHOT_DPI_FIX = (
    "Add-Type @'\n"
    "using System; using System.Runtime.InteropServices;\n"
    "public class __WcDpi {\n"
    '    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();\n'
    "}\n"
    "'@\n"
    "[__WcDpi]::SetProcessDPIAware() | Out-Null; "
)
_NEEDS_DPI_FIX_RE = re.compile(
    r"CopyFromScreen|PrimaryScreen|Screen\]::AllScreens|screencapture|screenshot",
    re.IGNORECASE,
)


_BASH_OR_RE = re.compile(r"(?<!\|)\|\|(?!\|)")

_DEV_NULL_RE = re.compile(r"(?:2>|>)\s*/dev/null", re.IGNORECASE)

_BASH_START_RE = re.compile(
    r'(?:^|;\s*)start\s+(?:""?\s+)?"?(?P<exe>[^";\n]+?)"?\s*$',
    re.IGNORECASE | re.MULTILINE,
)

_WHICH_RE = re.compile(r"\bwhich\s+", re.IGNORECASE)

_CMD_START_RE = re.compile(
    r'start\s+""?\s+"(?P<exe>[^"]+)"',
    re.IGNORECASE,
)


def _sanitize_bash_to_powershell(command: str) -> str:
    """Apply generic bash -> PowerShell 5.1 fixups for common incompatibilities."""
    cmd = command

    cmd = _DEV_NULL_RE.sub("2>$null", cmd)

    cmd = _WHICH_RE.sub("Get-Command ", cmd)

    cmd = _CMD_START_RE.sub(
        lambda m: f'Start-Process -FilePath "{m.group("exe")}"',
        cmd,
    )

    if _BASH_OR_RE.search(cmd):
        parts = _BASH_OR_RE.split(cmd)
        if len(parts) > 1:
            stmts: list[str] = []
            for i, part in enumerate(parts):
                part = part.strip()
                if not part:
                    continue
                if i == 0:
                    stmts.append(f"try {{ {part} }} catch {{ }}")
                else:
                    stmts.append(part)
            cmd = " ; ".join(stmts) if len(stmts) > 1 else (stmts[0] if stmts else cmd)

    return cmd


def _rewrite_windows_command(command: str) -> str:
    mkdir_match = re.fullmatch(r"\s*mkdir\s+-p\s+(.+?)\s*", command)
    if mkdir_match is not None:
        path_expr = mkdir_match.group(1)
        return (
            f"New-Item -ItemType Directory -Force -Path {path_expr} | Out-Null"
        )
    and_chain = _translate_windows_and_chain(command)
    if and_chain is not None:
        return and_chain
    probe_chain = _translate_windows_probe_chain(command)
    if probe_chain is not None:
        return probe_chain
    translated_probe = _translate_windows_probe_segment(command)
    if translated_probe is not None:
        return translated_probe

    command = _sanitize_bash_to_powershell(command)

    start_match = _BASH_START_RE.fullmatch(command.strip())
    if start_match is not None:
        exe_path = start_match.group("exe").strip().strip('"')
        return f'Start-Process -FilePath "{exe_path}"'

    printf_fn = (
        "function global:printf { "
        "param([string]$s) "
        "$decoded = $s.Replace('\\n', \"`n\").Replace('\\t', \"`t\").Replace('\\r', \"`r\"); "
        "[Console]::Out.Write($decoded) "
        "} ; "
    )
    # Auto-inject DPI awareness for screenshot commands on high-DPI displays
    dpi_prefix = _SCREENSHOT_DPI_FIX if _NEEDS_DPI_FIX_RE.search(command) else ""
    return dpi_prefix + printf_fn + command

_PERMISSION_DENIED_OUTPUT_RE = re.compile(
    r"PermissionError: \[Errno 13\] Permission denied: '([^']+\.(?:pptx|docx|xlsx|pdf|html?))'",
    re.IGNORECASE,
)
_REWRITABLE_OUTPUT_PATH_RE = re.compile(
    r"([rubfRUBF]*)(['\"])(?P<path>(?:[A-Za-z]:\\[^\n'\"]+|/[^\n'\"]+)\.(?:pptx|docx|xlsx|pdf|html?))\2",
    re.IGNORECASE,
)
_VERSION_SUFFIX_RE = re.compile(r"_V(\d+)$", re.IGNORECASE)
_PYTHON_SCRIPT_CMD_RE = re.compile(
    r'^(?P<python>(?:"[^"]+"|\S+?python(?:\.exe)?))\s+(?P<script>(?:"[^"]+\.py"|\S+\.py))(?P<suffix>(?:\s+.*)?)$',
    re.IGNORECASE,
)
_CD_AND_CHAIN_RE = re.compile(r'^cd\s+(?P<cwd>.+?)\s*&&\s*(?P<rest>.+)$', re.IGNORECASE)
_PS_SET_LOCATION_RE = re.compile(
    r'^Set-Location\s+-LiteralPath\s+(?P<cwd>.+?)\s*;\s*(?P<rest>.+)$',
    re.IGNORECASE,
)


def _strip_shell_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _extract_python_script_context(command: str) -> tuple[Path, str, str, str]:
    cwd = Path.cwd()
    rest = command.strip()
    cd_match = _CD_AND_CHAIN_RE.fullmatch(rest)
    if cd_match is not None:
        cwd = Path(_strip_shell_quotes(cd_match.group('cwd'))).expanduser()
        rest = cd_match.group('rest').strip()
    else:
        ps_match = _PS_SET_LOCATION_RE.fullmatch(rest)
        if ps_match is not None:
            cwd = Path(_strip_shell_quotes(ps_match.group('cwd'))).expanduser()
            rest = ps_match.group('rest').strip()
    py_match = _PYTHON_SCRIPT_CMD_RE.fullmatch(rest)
    if py_match is None:
        raise ValueError('not a python script command')
    python_cmd = py_match.group('python')
    script_token = _strip_shell_quotes(py_match.group('script'))
    suffix = py_match.group('suffix') or ''
    script_path = Path(script_token)
    if not script_path.is_absolute():
        script_path = (cwd / script_path).resolve()
    return cwd, python_cmd, str(script_path), suffix


def _next_available_versioned_output_path(path: Path) -> Path:
    stem_match = _VERSION_SUFFIX_RE.search(path.stem)
    if stem_match is not None:
        return path
    base_stem = _VERSION_SUFFIX_RE.sub("", path.stem)
    version = 1
    while True:
        candidate = path.with_name(f"{base_stem}_V{version}{path.suffix}")
        if not candidate.exists():
            return candidate
        version += 1


def _build_permission_retry_command(command: str, combined_output: str) -> str | None:
    denied_match = _PERMISSION_DENIED_OUTPUT_RE.search(combined_output)
    if denied_match is None:
        return None
    denied_path = Path(denied_match.group(1)).expanduser()
    try:
        cwd, python_cmd, script_path_str, suffix = _extract_python_script_context(command)
    except ValueError:
        return None
    script_path = Path(script_path_str)
    if not script_path.exists():
        return None
    try:
        script_text = script_path.read_text(encoding='utf-8')
    except Exception:
        return None
    fallback_seed = (_TMP_ALIAS_ROOT / denied_path.name).resolve()
    fallback_path = _next_available_versioned_output_path(fallback_seed)
    replaced = False

    def _replace_output(match: re.Match[str]) -> str:
        nonlocal replaced
        raw_path = match.group('path')
        if Path(raw_path).expanduser() != denied_path:
            return match.group(0)
        replaced = True
        prefix = match.group(1)
        quote = match.group(2)
        return f"{prefix}{quote}{fallback_path}{quote}"

    rewritten_script = _REWRITABLE_OUTPUT_PATH_RE.sub(_replace_output, script_text)
    if not replaced:
        return None
    retry_script = script_path.parent / (
        f"{script_path.stem}_retry_unlock_{uuid.uuid4().hex[:8]}{script_path.suffix}"
    )
    retry_script.write_text(rewritten_script, encoding='utf-8')
    retry_script_token = f'"{retry_script}"' if ' ' in str(retry_script) else str(retry_script)
    rebuilt = f"{python_cmd} {retry_script_token}{suffix}"
    cwd_token = f'"{cwd}"' if ' ' in str(cwd) else str(cwd)
    return f"cd {cwd_token} && {rebuilt}"



class BashTool(Tool):
    """Execute a bash command and return stdout/stderr/exit_code."""

    @staticmethod
    async def _spawn_process(
        command: str,
        *,
        env: dict[str, str],
    ) -> asyncio.subprocess.Process | _CompatProcess:
        if os.name != "nt":
            return await asyncio.create_subprocess_shell(
                command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        windows_command = _rewrite_windows_command(command)
        proc = subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-Command", windows_command],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        return _CompatProcess(proc)

    @property
    def definition(self) -> ToolDefinition:
        if os.name == "nt":
            desc = (
                "Execute a command on Windows (PowerShell 5.1). Returns stdout, stderr, and exit code. "
                "IMPORTANT: The host OS is Windows. Use PowerShell or cmd-compatible syntax. "
                "Avoid bash-only syntax (||, /dev/null, which, etc.). "
                "To open a program use: Start-Process -FilePath \"path\\to\\app.exe\""
            )
            cmd_desc = "The command to execute (PowerShell syntax preferred)."
        else:
            desc = "Execute a bash command. Returns stdout, stderr, and exit code."
            cmd_desc = "The bash command to execute."
        return ToolDefinition(
            name="bash",
            description=desc,
            parameters=[
                ToolParameter(
                    name="command", type="string", description=cmd_desc
                ),
                ToolParameter(
                    name="timeout",
                    type="integer",
                    description="Timeout in seconds (default 30; nano-banana commands use at least 300).",
                    required=False,
                ),
                ToolParameter(
                    name="background",
                    type="boolean",
                    description="Run command in background and return a session id.",
                    required=False,
                ),
            ],
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        raw_command: str = kwargs.get("command", "")
        command = _prefer_project_python_for_direct_script(
            _normalize_home_aliases(
                _normalize_tmp_aliases(
                    _fix_broken_python_path(
                        _normalize_project_python_aliases(
                            _prefer_project_python(_strip_control_chars(raw_command))
                        )
                    )
                )
            )
        )
        timeout = _resolve_command_timeout(command, int(kwargs.get("timeout", 30)))
        background = bool(kwargs.get("background", False))

        if not command.strip():
            return ToolResult(success=False, output="", error="命令为空")

        for pattern in _DANGEROUS_PATTERNS:
            if pattern.search(command):
                return ToolResult(success=False, output="", error=f"危险命令被拦截: {command}")

        env = os.environ.copy()
        if _PROJECT_PYTHON_BIN is not None and _PROJECT_PYTHON_BIN.is_dir():
            env["PATH"] = f"{_PROJECT_PYTHON_BIN}{os.pathsep}{env.get('PATH', '')}"
        _inject_saved_credentials(env)
        _TMP_ALIAS_ROOT.mkdir(parents=True, exist_ok=True)

        try:
            proc = await self._spawn_process(command, env=env)
            if background:
                session = register_background_process(
                    command=command,
                    cwd=os.getcwd(),
                    process=proc,
                )
                return ToolResult(
                    success=True,
                    output=(
                        f"后台命令已启动\n"
                        f"session_id: {session.id}\n"
                        f"pid: {proc.pid or 0}\n"
                        f"command: {command}"
                    ),
                )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            return ToolResult(success=False, output="", error=f"命令超时 ({timeout}s)")
        except OSError as exc:
            return ToolResult(success=False, output="", error=str(exc))

        out = _decode_output_bytes(stdout)[:_MAX_OUTPUT]
        err = _decode_output_bytes(stderr)[:_MAX_OUTPUT]
        exit_code = proc.returncode or 0

        if exit_code != 0:
            retry_command = _build_permission_retry_command(command, f"{out}\n{err}")
            if retry_command is not None and retry_command != command:
                return await self.execute(
                    command=retry_command,
                    timeout=timeout,
                    background=background,
                )

        if exit_code == 0:
            _postprocess_delivery_files(out, command)

        # Truncate excessively long output to avoid overwhelming the LLM context
        _SOFT_LIMIT = 8000
        if len(out) > _SOFT_LIMIT:
            head = out[:_SOFT_LIMIT // 2]
            tail = out[-_SOFT_LIMIT // 2:]
            out = (
                f"{head}\n\n"
                f"... [输出过长，已截断中间 {len(out) - _SOFT_LIMIT} 字符] ...\n\n"
                f"{tail}"
            )

        output = out
        if err:
            output += f"\n[stderr]\n{err}"
        output += f"\n[exit_code: {exit_code}]"

        is_success = exit_code == 0
        if (
            not is_success
            and _NANO_BANANA_SCRIPT_RE.search(command)
            and _NANO_BANANA_SUCCESS_RE.search(out)
        ):
            is_success = True

        return ToolResult(
            success=is_success,
            output=output.strip(),
            error=err if not is_success else None,
        )


_DELIVERY_PATH_RE = re.compile(
    r"((?:/[^\s:\"'<>|]+|[A-Za-z]:[\\/][^\s:\"'<>|]+)\.(?:pptx|docx|html?))\b",
    re.IGNORECASE | re.UNICODE,
)

_POSTPROCESS_RECENCY_SEC = 30
_POSTPROCESS_SUFFIXES = {".pptx", ".docx", ".html", ".htm"}


def _postprocess_delivery_files(output: str, command: str = "") -> None:
    """Auto-fix generated delivery files after a successful bash run.

    Supported: .pptx (face crop + Z-order), .docx (face crop), .html (object-fit).

    Sources (deduplicated):
      1. Paths found in stdout
      2. Paths found in the command text itself
      3. Recently modified files in temp roots (within last 30s)
    """
    import time

    candidates: set[str] = set()

    for text in (output, command):
        for m in _DELIVERY_PATH_RE.finditer(text):
            candidates.add(m.group(1))

    cutoff = time.time() - _POSTPROCESS_RECENCY_SEC
    recent_roots = (_TMP_ALIAS_ROOT, Path(tempfile.gettempdir()))
    for root in recent_roots:
        try:
            for p in root.iterdir():
                if p.suffix.lower() in _POSTPROCESS_SUFFIXES and p.stat().st_mtime >= cutoff:
                    candidates.add(str(p))
        except Exception:
            pass

    for c in candidates:
        p = Path(c)
        if not p.exists():
            continue
        suffix = p.suffix.lower()
        try:
            if suffix == ".pptx":
                from whaleclaw.utils.pptx_postprocess import fix_pptx
                fix_pptx(p)
            elif suffix == ".docx":
                from whaleclaw.utils.docx_postprocess import fix_docx
                fix_docx(p)
            elif suffix in (".html", ".htm"):
                from whaleclaw.utils.html_postprocess import fix_html
                fix_html(p)
        except Exception:
            pass
