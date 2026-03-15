"""AI Constitution — Layer-1 immutable safety constraints.

These rules are hard-coded and must never be removed, overridden, or
weakened by any configuration, plugin, skill, or user instruction.

Design rationale (mirrors OpenClaw's three-layer security model):
- Layer 1 (this file): AI constitution, system-level, cannot be modified.
- Layer 2 (permissions.py): Tool-call approval mechanism.
- Layer 3 (prompt.py static layer): Workspace behaviour boundaries.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Layer-1: AI Constitution — injected once into every system prompt.
# Kept compact to respect the ~1200-token system-prompt budget.
# ---------------------------------------------------------------------------

CONSTITUTION_TEXT: str = """\
【安全底线】不追求自我保存/复制/权力扩张；安全优先于任务完成；不绕过或修改安全机制；不泄露用户私人数据；删文件优先回收站而非直接销毁。"""

# ---------------------------------------------------------------------------
# Approval-required categories — used by PermissionChecker.needs_approval()
# and by the agent loop to surface confirmation requests to the user.
# ---------------------------------------------------------------------------

# Human-readable labels for each HighRiskCategory value.
HIGH_RISK_LABELS: dict[str, str] = {
    "send_email": "发送电子邮件（不可撤回的对外通信）",
    "send_message": "向外部发送消息（不可撤回的对外通信）",
    "post_public": "发布公开内容（推文/帖子等不可撤回）",
    "delete_system_file": "删除工作空间以外的系统文件",
    "financial": "涉及金钱/支付的操作",
    "account_permission": "账号权限变更（用户/权限/密码修改）",
}

APPROVAL_PROMPT_TEMPLATE: str = (
    "⚠️  [需要确认] 以下操作属于「{label}」，执行后可能不可撤回。\n"
    "命令：{command}\n\n"
    "请回复 /approve 确认执行，或 /cancel 取消。"
)


def format_approval_prompt(category: str, command: str) -> str:
    """Build a human-readable approval request for a high-risk command.

    Args:
        category: HighRiskCategory value string.
        command: The command/action pending approval.

    Returns:
        Formatted prompt string to present to the user.
    """
    label = HIGH_RISK_LABELS.get(category, category)
    return APPROVAL_PROMPT_TEMPLATE.format(label=label, command=command)


__all__ = [
    "APPROVAL_PROMPT_TEMPLATE",
    "CONSTITUTION_TEXT",
    "HIGH_RISK_LABELS",
    "format_approval_prompt",
]
