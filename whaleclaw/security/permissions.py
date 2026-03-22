"""Permission model — tool whitelist/blacklist, path restrictions, high-risk approval."""

from __future__ import annotations

import re
import sys
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field


class ToolPermission(BaseModel):
    """Tool permission configuration."""

    allow: list[str] = Field(default_factory=lambda: ["*"])
    deny: list[str] = Field(default_factory=list)


class HighRiskCategory(StrEnum):
    """Categories of operations that require explicit user approval.

    Layer-2 tool-call safety: mirrors OpenClaw's approval mechanism.
    """

    # Irreversible external communications
    SEND_EMAIL = "send_email"
    SEND_MESSAGE = "send_message"
    POST_PUBLIC = "post_public"
    # Destructive filesystem operations outside workspace
    DELETE_SYSTEM_FILE = "delete_system_file"
    # Financial / account privilege changes
    FINANCIAL = "financial"
    ACCOUNT_PERMISSION = "account_permission"


# Regex patterns → HighRiskCategory for bash command classification
_HIGH_RISK_BASH_PATTERNS: list[tuple[re.Pattern[str], HighRiskCategory]] = [
    # Email sending
    (re.compile(r"\bsendmail\b|\bmail\s+-s\b|\bmutt\b|\bswaks\b", re.IGNORECASE), HighRiskCategory.SEND_EMAIL),
    # Mass file deletion outside workspace
    (re.compile(r"\brm\s+(?:-[rRfF]+\s+)?(?!/root/\.whaleclaw)(?!/home/[^/]+/\.whaleclaw)[/~]", re.IGNORECASE), HighRiskCategory.DELETE_SYSTEM_FILE),
    # Privilege escalation / account changes
    (re.compile(r"\bpasswd\b|\busermod\b|\bchmod\s+(?:777|a\+[rwx])\s+/", re.IGNORECASE), HighRiskCategory.ACCOUNT_PERMISSION),
    # curl/wget posting to external endpoints (potential data exfiltration)
    (re.compile(r"\bcurl\b.*\B-[Xd]\s*POST\b|\bwget\b.*--post-data", re.IGNORECASE), HighRiskCategory.FINANCIAL),
]


def _default_denied_paths() -> list[str]:
    """Platform-aware default denied paths."""
    common = ["~/.ssh/", "~/.gnupg/"]
    if sys.platform == "win32":
        return [
            "C:\\Windows\\",
            "C:\\Program Files\\",
            "C:\\Program Files (x86)\\",
            *common,
        ]
    return ["/etc/", "/var/", "/usr/", "/sys/", "/proc/", *common]


def _default_require_approval_for() -> list[str]:
    """Default set of high-risk categories that require user approval."""
    return [
        HighRiskCategory.SEND_EMAIL,
        HighRiskCategory.SEND_MESSAGE,
        HighRiskCategory.POST_PUBLIC,
        HighRiskCategory.DELETE_SYSTEM_FILE,
        HighRiskCategory.FINANCIAL,
        HighRiskCategory.ACCOUNT_PERMISSION,
    ]


class SecurityPolicy(BaseModel):
    """Per-session security policy."""

    sandbox: bool = False
    tools: ToolPermission = Field(default_factory=ToolPermission)
    max_tool_rounds: int = 50
    allow_file_write: bool = True
    allow_network: bool = True
    allowed_paths: list[str] = Field(default_factory=list)
    denied_paths: list[str] = Field(default_factory=_default_denied_paths)
    # High-risk categories that must be approved before execution.
    # Users can remove entries to permanently allow a category ("allow-always").
    require_approval_for: list[str] = Field(default_factory=_default_require_approval_for)


_DANGEROUS_CMD_PATTERNS = [
    re.compile(r"rm\s+-rf\s+/", re.IGNORECASE),
    re.compile(r"rm\s+-fr\s+/", re.IGNORECASE),
    re.compile(r"rm\s+-r\s+-f\s+/", re.IGNORECASE),
    re.compile(r"\bmkfs\b", re.IGNORECASE),
    re.compile(r"dd\s+if=/dev/zero", re.IGNORECASE),
    re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", re.IGNORECASE),
]


class PermissionChecker:
    """Checks tool, path, and command permissions against a SecurityPolicy."""

    @staticmethod
    def check_tool(tool_name: str, policy: SecurityPolicy) -> bool:
        """Return True if tool is allowed, False if denied."""
        tools = policy.tools
        if tool_name in tools.deny:
            return False
        if "*" in tools.allow:
            return True
        return tool_name in tools.allow

    @staticmethod
    def check_path(path: str, policy: SecurityPolicy, write: bool = False) -> bool:
        """Return True if path access is allowed."""
        if write and not policy.allow_file_write:
            return False
        expanded = str(Path(path).expanduser())
        for denied in policy.denied_paths:
            if expanded.startswith(str(Path(denied).expanduser())):
                return False
        if policy.allowed_paths:
            for allowed in policy.allowed_paths:
                if expanded.startswith(str(Path(allowed).expanduser())):
                    return True
            return False
        return True

    @staticmethod
    def check_command(command: str, policy: SecurityPolicy) -> bool:
        """Return False if command matches dangerous patterns."""
        return all(not pat.search(command) for pat in _DANGEROUS_CMD_PATTERNS)

    @staticmethod
    def classify_high_risk(command: str) -> HighRiskCategory | None:
        """Return the HighRiskCategory if a bash command matches a high-risk pattern.

        Used by the agent loop to decide whether to request approval before
        executing an operation.  Returns None for normal (non-high-risk) commands.
        """
        for pattern, category in _HIGH_RISK_BASH_PATTERNS:
            if pattern.search(command):
                return category
        return None

    @staticmethod
    def needs_approval(command: str, policy: SecurityPolicy) -> HighRiskCategory | None:
        """Return the category requiring approval, or None if execution is allowed.

        A command needs approval when:
        1. It matches a known high-risk pattern, AND
        2. That category is still listed in policy.require_approval_for.

        Users can permanently allow a category by removing it from
        ``policy.require_approval_for`` (the "allow-always" flow).
        """
        category = PermissionChecker.classify_high_risk(command)
        if category is None:
            return None
        if category in policy.require_approval_for:
            return category
        return None
