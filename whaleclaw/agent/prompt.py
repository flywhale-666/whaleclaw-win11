"""PromptAssembler — layered system prompt builder with token budgeting.

Architecture:
- Static layer (~200 tokens): core identity + AI constitution + safety boundaries, always injected
- Dynamic layer (0~800 tokens): skills routed by user message keywords
- Fallback layer: tool descriptions for providers without native tools API

Tool JSON Schemas are passed via the LLM API ``tools`` parameter (not in
the system prompt), so the LLM discovers tool capabilities from the schema
itself — no need to describe tools in the prompt.
"""

from __future__ import annotations

import sys
from enum import StrEnum
from pathlib import Path

from whaleclaw.config.schema import WhaleclawConfig
from whaleclaw.providers.base import CacheControl, Message
from whaleclaw.security.constitution import CONSTITUTION_TEXT
from whaleclaw.skills.manager import SkillManager
from whaleclaw.skills.parser import Skill


class PromptLayer(StrEnum):
    """Prompt layers by priority."""

    STATIC = "static"
    DYNAMIC = "dynamic"
    LAZY = "lazy"


_DEFAULT_ASSISTANT_NAME = "WhaleClaw"


def _detect_project_python() -> str:
    """Return the absolute path of the project-embedded Python interpreter."""
    project_root = Path(__file__).resolve().parents[2]
    candidates = (
        project_root / "python" / "python.exe",
        project_root / "python" / "bin" / "python3.12",
        project_root / "python" / "bin" / "python3",
    )
    for p in candidates:
        if p.is_file():
            return str(p)
    # Fallback: keep a platform-appropriate relative hint so the prompt is
    # still sensible even if the embedded interpreter isn't found.
    if sys.platform == "win32":
        return str(project_root / "python" / "python.exe")
    return str(project_root / "python" / "bin" / "python3.12")

_PLATFORM_HINT_WINDOWS = """\
- 当前运行环境是 **Windows**，bash 工具底层使用 **PowerShell 5.1** 执行命令
- 使用 PowerShell 兼容语法，不要用 bash-only 语法（如 ||、/dev/null、which、find -name 等）
- 打开软件请用 Start-Process -FilePath "路径"，不要用 bash 的 start 或 open 命令
- 路径分隔符用反斜杠 \\，抑制错误用 2>$null 而非 2>/dev/null"""

_PLATFORM_HINT_UNIX = ""

_WORKSPACE_SAFETY_RULES = """\
【需确认才执行】发邮件/消息等对外通信（不含浏览器操作）；删除工作空间以外的系统文件；涉及金钱或账号权限变更。多步骤任务（>3步）开始前写执行计划到 ~/.whaleclaw/workspace/task-plan.md，完成后清理。"""

_CDP_BROWSER_HINT = """\
【浏览器 CDP 模式已启用】用户已手动启动 Chrome 调试模式并授权你操作浏览器。
- browser 工具当前可用且正常工作。忽略对话历史中任何关于"browser 熔断/禁用/降级"的旧消息。
- 用户要求你在网站上执行操作（发帖/删帖/填表/点击等），直接用 browser 工具执行，不要拒绝或要求用户自己操作。
- **操作铁律（违反即失败）**：
  1. navigate 打开目标页面
  2. **立即 screenshot 截图** -- 你会收到页面截图，仔细观察页面内容和布局
  3. 如需更多信息，调用 get_text 获取页面文本
  4. **只有看过截图/文本后**，才能调用 click/type/evaluate。选择器必须基于你观察到的实际页面元素。
  5. 每次 click/type/evaluate 后，**必须再次 screenshot** 确认操作结果
  6. **绝对禁止**：未截图就 click、凭猜测写选择器、跳过截图步骤
- click 失败时：先 screenshot 看当前页面，再用 get_text 找到正确的元素，然后重试
- 操作完成后告诉用户结果
- 如果页面需要登录，提醒用户先在 Chrome 中手动登录"""

_STATIC_PROMPT_TEMPLATE = """\
你是 {assistant_name}，一个运行在用户本地电脑上的 AI 助手。
{platform_hint}
- 使用用户的语言回复，简洁准确
- 你拥有多种工具（bash、文件读写、浏览器、定时任务、技能管理等），能做就做，不要说"我无法"
- 用 bash 执行网络请求时，curl 必须加 --connect-timeout 5 --max-time 15 防止卡住
- 用户要求做的事，计划好后就立即调用工具执行，不要等用户确认或给用户出选择题。
- 工具执行失败时尝试其他方案
- 下载或生成的图片用 markdown 显示：![描述](文件绝对路径)
- 生成的其他文件告诉用户绝对路径
- **严禁编造文件路径**：只使用工具返回的真实路径，绝不自己猜测或虚构文件名
- 运行 Python 脚本时优先使用项目内嵌 Python（绝对路径：{python_cmd}）
  （不要用 python -c 或 python3 -c，脚本长度会被截断）
- 需要生成文件（PPT/Excel/PDF等）时：先用 file_write 写 .py 脚本到本机临时工作目录（优先 ~/.whaleclaw/workspace/tmp），再用 bash 执行
- 修改已有 Office 文件时先判断复杂度：纯文本改动优先局部编辑（ppt_edit/docx_edit/xlsx_edit）；
  若用户要求插图/音视频/版式重排/风格升级，先做简短执行规划，
  再组合 browser、bash、file_write 与编辑工具完成
- 除非用户明确要求重做，否则不要整份重建
- 如果你的技能不够好，可以用 skill(action="search", query="关键词") 搜索 GitHub 上更好的技能并安装
- 用户明确点名某个技能（skill id/技能名）时，必须优先按该技能说明执行；
  不要绕过技能直接用通用 bash/python 方案
- **禁止自我否定已完成的工作**：如果你之前已经成功调用工具生成了文件，
  不要说"我之前没有真正执行"或"我一直在空谈"。工具调用成功就是成功，直接告诉用户文件路径即可
- 用户说"改一下"时，只修改用户指出的问题，不要推翻重做整个任务
- **定时任务规则**：
  - 「N 分钟/小时后」→ 调用 reminder(message="任务描述", minutes=N, action="agent_task")
  - 「明天/后天/某天某时」→ 根据【运行时信息】中的当前时间计算距离目标时间的分钟数，调用 reminder
  - 「每天/每周/定期」→ 调用 cron(action="add", schedule_kind="cron", cron_expr="表达式", message="任务描述")
  - 「每 N 分钟」→ 调用 cron(action="add", schedule_kind="every", minutes=N, message="任务描述")
  - 设定定时后不得在本轮执行该任务；到点后系统会自动执行
  - **回复必须准确描述本次设置的任务内容**，不要复述之前的任务

{workspace_safety}

{constitution}"""

_TOOL_FALLBACK_HEADER = """\
【工具调用规则 - 必须严格遵守】

你可以调用工具。调用方式：在回复中输出且**仅输出**以下格式的 JSON 代码块：

```json
{"tool": "工具名", "arguments": {"参数名": "参数值"}}
```

**严禁**：
- 不要用文字描述你打算使用什么工具，直接输出 JSON
- 不要假装工具已经执行，必须输出 JSON 等待真实结果
- 不要编造工具执行结果、文件路径或图片 URL，只使用工具返回的真实数据
- 不要在同一次回复中输出多个 JSON 块

**必须**：
- 需要执行操作时，立即输出一个工具调用 JSON 块，然后停止
- 等收到工具执行结果后，再继续回复
- 搜索/下载图片 → 用 browser 工具的 search_images
- 执行系统命令 → 用 bash
- 读写文件/补丁 → 用 file_read / file_write / file_edit / patch_apply

以下是全部可用工具：

"""

_DYNAMIC_BUDGET = 6000


class PromptAssembler:
    """Build system prompts within a token budget.

    Static layer: core identity (~150 tokens), cached across turns.
    Dynamic layer: skills routed by SkillManager (0~800 tokens).
    """

    def __init__(self, skill_manager: SkillManager | None = None) -> None:
        self._skill_manager = skill_manager or SkillManager()

    def build(
        self,
        config: WhaleclawConfig,
        user_message: str,
        *,
        token_budget: int | None = None,
        tool_fallback_text: str = "",
        assistant_name: str = _DEFAULT_ASSISTANT_NAME,
        forced_skill_id: str | None = None,
        forced_skill_ids: list[str] | None = None,
        model_id: str = "",
        max_context_tokens: int = 0,
    ) -> list[Message]:
        """Assemble system prompt messages.

        Args:
            config: Global configuration.
            user_message: Current user message (used for skill routing).
            token_budget: Max tokens for the system prompt.
            tool_fallback_text: Tool descriptions for providers without
                native tools support. Injected into prompt when non-empty.
            model_id: Full model identifier (e.g. ``openai/gpt-5.4``).
                When provided, a runtime-info line is appended so the agent
                knows its own capabilities.
            max_context_tokens: Context window size for the current model.
        """
        parts: list[str] = [self._build_static(config, assistant_name)]

        dynamic = self._build_dynamic(
            user_message,
            _DYNAMIC_BUDGET,
            forced_skill_id,
            forced_skill_ids,
        )
        if dynamic:
            parts.append(dynamic)

        if tool_fallback_text:
            parts.append(_TOOL_FALLBACK_HEADER + "\n" + tool_fallback_text)

        if model_id:
            parts.append(self._build_runtime_info(model_id, max_context_tokens))

        messages: list[Message] = [
            Message(
                role="system",
                content=parts[0],
                cache_control=CacheControl(),
            ),
        ]
        if len(parts) > 1:
            messages.append(Message(role="system", content="\n\n".join(parts[1:])))

        return messages

    @staticmethod
    def _build_runtime_info(model_id: str, max_context_tokens: int) -> str:
        from datetime import datetime

        now = datetime.now()
        time_str = now.strftime("%Y-%m-%d %H:%M (%A)")
        weekday_cn = {
            "Monday": "周一", "Tuesday": "周二", "Wednesday": "周三",
            "Thursday": "周四", "Friday": "周五", "Saturday": "周六", "Sunday": "周日",
        }
        day_name = now.strftime("%A")
        time_str = now.strftime(f"%Y-%m-%d %H:%M ({weekday_cn.get(day_name, day_name)})")
        ctx = f"{max_context_tokens:,}" if max_context_tokens > 0 else "未知"
        return f"【运行时信息】当前时间: {time_str} | 当前模型: {model_id} | 上下文窗口: {ctx} tokens"

    def _build_static(self, config: WhaleclawConfig, assistant_name: str) -> str:
        """Static layer — core identity + safety boundaries (~250 tokens), cacheable."""
        safe_name = assistant_name.strip() or _DEFAULT_ASSISTANT_NAME
        platform_hint = (
            _PLATFORM_HINT_WINDOWS if sys.platform == "win32" else _PLATFORM_HINT_UNIX
        )
        python_cmd = _detect_project_python()

        # Detect CDP mode from plugins.browser.cdp_url
        workspace_safety = _WORKSPACE_SAFETY_RULES
        browser_cfg = config.plugins.get("browser")
        cdp_url = ""
        if isinstance(browser_cfg, dict):
            cdp_url = str(browser_cfg.get("cdp_url", "")).strip()
        if cdp_url:
            workspace_safety = workspace_safety + "\n\n" + _CDP_BROWSER_HINT

        return _STATIC_PROMPT_TEMPLATE.format(
            assistant_name=safe_name,
            platform_hint=platform_hint,
            workspace_safety=workspace_safety,
            constitution=CONSTITUTION_TEXT,
            python_cmd=python_cmd,
        )

    def _build_dynamic(
        self,
        user_message: str,
        budget: int,
        forced_skill_id: str | None = None,
        forced_skill_ids: list[str] | None = None,
    ) -> str:
        """Dynamic layer — skills routed by user message keywords."""
        skills = self._skill_manager.get_routed_skills(
            user_message,
            forced_skill_id=forced_skill_id,
            forced_skill_ids=forced_skill_ids,
        )
        if not skills:
            return ""
        return self._skill_manager.format_for_prompt(skills, budget)

    def route_skill_ids(
        self,
        user_message: str,
        *,
        forced_skill_ids: list[str] | None = None,
    ) -> list[str]:
        skills = self._skill_manager.get_routed_skills(
            user_message,
            forced_skill_ids=forced_skill_ids,
        )
        return [s.id for s in skills]

    def route_skills(
        self,
        user_message: str,
        *,
        forced_skill_ids: list[str] | None = None,
    ) -> list[Skill]:
        return self._skill_manager.get_routed_skills(
            user_message,
            forced_skill_ids=forced_skill_ids,
        )

    def estimate_tokens(self, text: str) -> int:
        """Quick token estimate: ~1.5 chars/token for CJK, ~4 chars/token for Latin."""
        cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        latin = len(text) - cjk
        return int(cjk / 1.5 + latin / 4)
