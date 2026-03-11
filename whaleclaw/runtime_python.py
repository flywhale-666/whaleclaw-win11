"""Runtime helpers for enforcing project-embedded Python."""

from __future__ import annotations

import os
import sys
from pathlib import Path

_REEXEC_ENV = "WHALECLAW_EMBEDDED_PY_REEXEC"


def _project_python() -> Path:
    """Return the expected embedded Python path under project root."""
    root = Path(__file__).resolve().parents[1]
    candidates = (
        root / "python" / "python.exe",
        root / "python" / "bin" / "python3.12",
        root / "python" / "bin" / "python3",
    )
    for p in candidates:
        if p.is_file():
            return p
    # Fallback: prefer Windows layout
    return root / "python" / "python.exe"


def ensure_embedded_python(*, module: str) -> None:
    """Re-exec current process with embedded Python when available."""
    if os.environ.get("WHALECLAW_DISABLE_EMBEDDED_PYTHON") == "1":
        return
    if "PYTEST_CURRENT_TEST" in os.environ:
        return
    if os.environ.get(_REEXEC_ENV) == "1":
        return

    target = _project_python()
    if not target.is_file():
        return

    try:
        current = Path(sys.executable).resolve()
        target_resolved = target.resolve()
    except OSError:
        return

    if current == target_resolved:
        return

    os.environ[_REEXEC_ENV] = "1"
    args = [str(target_resolved), "-m", module, *sys.argv[1:]]
    if sys.platform == "win32":
        import subprocess
        raise SystemExit(subprocess.call(args))
    else:
        os.execv(str(target_resolved), args)
