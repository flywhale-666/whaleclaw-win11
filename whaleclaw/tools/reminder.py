"""Reminder tool — shortcut for one-shot cron jobs.

Kept as a separate tool so LLMs can use the simpler
``reminder(message, minutes)`` interface instead of the full cron tool.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

from whaleclaw.cron.scheduler import CronAction, CronJob, CronScheduler, Schedule
from whaleclaw.tools.base import Tool, ToolDefinition, ToolParameter, ToolResult


class ReminderTool(Tool):
    """Set a one-shot reminder N minutes from now."""

    def __init__(self, scheduler: CronScheduler) -> None:
        self._scheduler = scheduler
        self.current_session_id: str = ""

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="reminder",
            description="Set a reminder or schedule an agent task after N minutes.",
            parameters=[
                ToolParameter(
                    name="message",
                    type="string",
                    description="Reminder message or agent task instruction.",
                ),
                ToolParameter(
                    name="minutes",
                    type="integer",
                    description="Minutes from now to trigger.",
                ),
                ToolParameter(
                    name="action",
                    type="string",
                    description=(
                        "Action type: 'message' (default) sends a reminder notification; "
                        "'agent_task' runs the message as an agent instruction after the delay."
                    ),
                    required=False,
                    enum=["message", "agent_task"],
                ),
            ],
        )

    async def execute(self, **kwargs: object) -> ToolResult:
        message = str(kwargs.get("message", ""))
        raw_min = kwargs.get("minutes")
        if raw_min is None:
            return ToolResult(success=False, output="", error="缺少 minutes 参数")
        if isinstance(raw_min, bool):
            return ToolResult(success=False, output="", error="minutes 必须为整数")
        if isinstance(raw_min, int):
            minutes = raw_min
        elif isinstance(raw_min, float):
            minutes = int(raw_min)
        elif isinstance(raw_min, str):
            try:
                minutes = int(raw_min.strip())
            except ValueError:
                return ToolResult(success=False, output="", error="minutes 必须为整数")
        else:
            return ToolResult(success=False, output="", error="minutes 必须为整数")
        if minutes < 1:
            return ToolResult(success=False, output="", error="minutes 必须大于 0")

        raw_action = str(kwargs.get("action", "message")).strip().lower()
        action_type: str = "agent" if raw_action == "agent_task" else "message"

        now = datetime.now()
        target = now + timedelta(minutes=minutes)
        payload: dict[str, object] = {"text": message}
        job = CronJob(
            id=f"reminder-{uuid4().hex[:12]}",
            name=f"提醒: {message[:20]}",
            schedule_obj=Schedule(kind="at", at=target),
            action=CronAction(
                type=action_type,  # type: ignore[arg-type]
                target=self.current_session_id or "user",
                payload=payload,
            ),
            enabled=True,
            created_at=now,
            one_shot=True,
        )
        await self._scheduler.add_job(job)
        label = "定时 agent 任务" if action_type == "agent" else "提醒"
        return ToolResult(
            success=True,
            output=f"{label}已设置，{minutes} 分钟后执行",
        )
