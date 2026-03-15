"""Security subsystem — permissions, pairing, sandbox, audit, constitution."""

from whaleclaw.security.audit import AuditEvent, AuditLogger
from whaleclaw.security.constitution import (
    CONSTITUTION_TEXT,
    HIGH_RISK_LABELS,
    format_approval_prompt,
)
from whaleclaw.security.pairing import AllowListStore, PairingRequest, PairingService
from whaleclaw.security.permissions import (
    HighRiskCategory,
    PermissionChecker,
    SecurityPolicy,
    ToolPermission,
)
from whaleclaw.security.sandbox import DockerSandbox, SandboxConfig, SandboxMode

__all__ = [
    "AllowListStore",
    "AuditEvent",
    "AuditLogger",
    "CONSTITUTION_TEXT",
    "DockerSandbox",
    "HIGH_RISK_LABELS",
    "HighRiskCategory",
    "PairingRequest",
    "PairingService",
    "PermissionChecker",
    "SandboxConfig",
    "SandboxMode",
    "SecurityPolicy",
    "ToolPermission",
    "format_approval_prompt",
]
