"""Multi-Agent configuration parsing and scenario prompts."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, cast

from whaleclaw.agent.helpers.regex_patterns import (
    _CN_DIGIT_MAP,
    _MULTI_AGENT_CANCEL_PATTERNS,
    _MULTI_AGENT_CONFIRM_PATTERNS,
    _MULTI_AGENT_DISCUSS_DONE_PATTERNS,
    _MULTI_AGENT_ROUNDS_PATTERNS,
)

if TYPE_CHECKING:
    from whaleclaw.config.schema import WhaleclawConfig
    from whaleclaw.sessions.manager import Session


def _multi_agent_cfg(config: WhaleclawConfig) -> dict[str, object]:
    plugins_raw = config.plugins
    plugins: dict[str, object] = plugins_raw if isinstance(plugins_raw, dict) else {}  # pyright: ignore[reportAssignmentType, reportUnnecessaryIsInstance]
    raw_ma = plugins.get("multi_agent", {})
    if not isinstance(raw_ma, dict):
        return {"enabled": False, "mode": "parallel", "max_rounds": 1, "roles": []}

    raw: dict[str, object] = raw_ma  # pyright: ignore[reportAssignmentType, reportUnknownVariableType]
    enabled = bool(raw.get("enabled", False))
    mode_raw = str(raw.get("mode", "parallel")).strip().lower()
    mode = mode_raw if mode_raw in {"parallel", "serial"} else "parallel"

    try:
        max_rounds = int(raw.get("max_rounds", 1))  # pyright: ignore[reportArgumentType]
    except Exception:
        max_rounds = 1
    max_rounds = max(1, min(max_rounds, 10))

    roles_raw = raw.get("roles")
    roles: list[dict[str, object]] = []
    if isinstance(roles_raw, list):
        for idx, item in enumerate(roles_raw[:20], start=1):  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
            if not isinstance(item, dict):
                continue
            role_item: dict[str, object] = item  # pyright: ignore[reportAssignmentType, reportUnknownVariableType]
            rid = str(role_item.get("id", f"role_{idx}")).strip().lower()
            rid = "".join(ch for ch in rid if ch.isalnum() or ch in {"_", "-"})
            if not rid:
                rid = f"role_{idx}"
            name = str(role_item.get("name", f"角色{idx}")).strip() or f"角色{idx}"
            model = str(role_item.get("model", "")).strip()
            system_prompt = str(role_item.get("system_prompt", "")).strip()
            roles.append(
                {
                    "id": rid[:64],
                    "name": name[:50],
                    "enabled": bool(role_item.get("enabled", True)),
                    "model": model[:100],
                    "system_prompt": system_prompt[:3000],
                }
            )
    return {
        "enabled": enabled,
        "mode": mode,
        "max_rounds": max_rounds,
        "roles": roles,
    }


def _scenario_discuss_focus(scenario: str) -> str:
    if scenario == "product_design":
        return (
            "重点和用户确认：产品目标、目标用户、核心场景、关键流程、约束条件、"
            "以及交付物类型（如 PRD 文档、流程图图片、原型说明、里程碑计划）。"
        )
    if scenario == "content_creation":
        return (
            "重点和用户确认：受众人群、内容主题、语气风格、发布渠道、篇幅限制、"
            "素材来源与交付物类型（文章/脚本/海报文案/配图说明）。"
        )
    if scenario == "software_development":
        return (
            "重点和用户确认：功能目标、技术栈、运行环境、改动范围、验收标准、"
            "交付物类型（代码补丁、命令步骤、测试报告、部署说明）。"
        )
    if scenario == "data_analysis_decision":
        return (
            "重点和用户确认：决策问题、指标口径、数据来源、时间窗口、可信度要求、"
            "交付物类型（分析报告、图表、结论摘要、决策建议表）。"
        )
    if scenario == "scientific_research":
        return (
            "重点和用户确认：研究问题、假设、实验条件、对照设计、评估指标、"
            "交付物类型（研究提纲、实验方案、结果解读、论文结构草稿）。"
        )
    if scenario == "intelligent_assistant":
        return (
            "重点和用户确认：任务目标、时效要求、可调用工具、执行边界、"
            "交付物类型（行动计划、提醒清单、消息草稿、执行结果汇总）。"
        )
    if scenario == "workflow_automation":
        return (
            "重点和用户确认：触发条件、上下游系统、字段映射、失败重试、监控告警、"
            "交付物类型（流程设计文档、自动化脚本、运行手册、告警规则清单）。"
        )
    return "重点和用户确认目标、约束、验收标准与最终交付物类型。"


def _scenario_delivery_focus(scenario: str) -> str:  # pyright: ignore[reportUnusedFunction]
    if scenario == "product_design":
        return (
            "最终答复必须包含：\n"
            "1) 产品方案摘要\n"
            "2) 结构化交付物清单（文档/图片/表格）\n"
            "3) 每个交付物的建议文件名与路径（例如 ~/.whaleclaw/workspace/tmp/product_prd.md）\n"
            "4) 执行优先级与里程碑\n"
            "5) 风险与备选方案"
        )
    if scenario == "content_creation":
        return (
            "最终答复必须包含：\n"
            "1) 内容策略与目标受众\n"
            "2) 成品文案或脚本草案\n"
            "3) 渠道适配版本（至少 2 个）\n"
            "4) 交付物清单与建议文件名/路径（如 ~/.whaleclaw/workspace/tmp/content_plan.md）\n"
            "5) 发布节奏与复盘指标"
        )
    if scenario == "software_development":
        return (
            "最终答复必须包含：\n"
            "1) 技术方案与实现路径\n"
            "2) 关键代码改动点或命令步骤\n"
            "3) 测试与回归计划\n"
            "4) 交付物清单与建议文件名/路径（如 ~/.whaleclaw/workspace/tmp/impl_plan.md）\n"
            "5) 风险、回滚与上线注意项"
        )
    if scenario == "data_analysis_decision":
        return (
            "最终答复必须包含：\n"
            "1) 数据结论与关键洞察\n"
            "2) 指标口径说明与分析过程摘要\n"
            "3) 决策选项对比与推荐方案\n"
            "4) 交付物清单与建议文件名/路径（如 ~/.whaleclaw/workspace/tmp/analysis_report.md）\n"
            "5) 风险假设与后续验证计划"
        )
    if scenario == "scientific_research":
        return (
            "最终答复必须包含：\n"
            "1) 研究目标与假设\n"
            "2) 方法设计与实验步骤\n"
            "3) 结果解读框架与可信度边界\n"
            "4) 交付物清单与建议文件名/路径（如 ~/.whaleclaw/workspace/tmp/research_plan.md）\n"
            "5) 下一步实验与论文化建议"
        )
    if scenario == "intelligent_assistant":
        return (
            "最终答复必须包含：\n"
            "1) 任务拆解与优先级\n"
            "2) 可执行动作清单\n"
            "3) 关键提醒与时间节点\n"
            "4) 交付物清单与建议文件名/路径（如 ~/.whaleclaw/workspace/tmp/assistant_actions.md）\n"
            "5) 异常处理与后续跟进建议"
        )
    if scenario == "workflow_automation":
        return (
            "最终答复必须包含：\n"
            "1) 自动化流程设计（触发-处理-输出）\n"
            "2) 集成接口与字段映射说明\n"
            "3) 失败重试与告警策略\n"
            "4) 交付物清单与建议文件名/路径（如 ~/.whaleclaw/workspace/tmp/workflow_spec.md）\n"
            "5) 上线运行与运维检查清单"
        )
    return (
        "最终答复必须包含：结论、可执行清单、交付物清单（含建议文件名/路径）、"
        "风险与回滚建议。"
    )


def _resolve_multi_agent_cfg(
    config: WhaleclawConfig,
    session: Session | None,
) -> dict[str, object]:
    """Build effective multi-agent config with optional session overrides."""
    cfg = _multi_agent_cfg(config)
    plugins_raw2 = config.plugins
    plugins2: dict[str, object] = plugins_raw2 if isinstance(plugins_raw2, dict) else {}  # pyright: ignore[reportAssignmentType, reportUnnecessaryIsInstance]
    raw = plugins2.get("multi_agent", {})
    if isinstance(raw, dict):  # pyright: ignore[reportUnnecessaryIsInstance]
        raw_typed: dict[str, object] = raw  # pyright: ignore[reportAssignmentType, reportUnknownVariableType]
        scenario = str(raw_typed.get("scenario", "software_development")).strip()
        cfg["scenario"] = scenario or "software_development"
    else:
        cfg["scenario"] = "software_development"
    if session is None or not isinstance(session.metadata, dict):  # pyright: ignore[reportUnnecessaryIsInstance]
        return cfg

    metadata = session.metadata
    if isinstance(metadata.get("multi_agent_enabled"), bool):
        cfg["enabled"] = bool(metadata["multi_agent_enabled"])

    mode_raw = str(metadata.get("multi_agent_mode", "")).strip().lower()
    if mode_raw in {"parallel", "serial"}:
        cfg["mode"] = mode_raw

    rounds_raw = metadata.get("multi_agent_max_rounds")
    if isinstance(rounds_raw, int):
        cfg["max_rounds"] = max(1, min(rounds_raw, 10))

    return cfg


def _is_multi_agent_confirm(text: str) -> bool:
    t = text.strip()
    if not t:
        return False
    return any(p.search(t) for p in _MULTI_AGENT_CONFIRM_PATTERNS)


def _is_multi_agent_cancel(text: str) -> bool:
    t = text.strip()
    if not t:
        return False
    return any(p.search(t) for p in _MULTI_AGENT_CANCEL_PATTERNS)


def _extract_multi_agent_rounds(text: str) -> int | None:
    t = text.strip()
    if not t:
        return None
    for p in _MULTI_AGENT_ROUNDS_PATTERNS:
        m = p.search(t)
        if not m:
            continue
        raw = m.group(1).strip()
        if raw in _CN_DIGIT_MAP:
            value = _CN_DIGIT_MAP[raw]
        else:
            try:
                value = int(raw)
            except Exception:
                return None
        if 1 <= value <= 10:
            return value
    return None


def _attach_rounds_marker(topic: str, rounds: int) -> str:
    clean = re.sub(r"\[MA_ROUNDS=\d{1,2}\]\s*", "", topic).strip()
    return f"[MA_ROUNDS={rounds}] {clean}".strip()


def _extract_rounds_marker(topic: str) -> tuple[str, int | None]:
    m = re.search(r"\[MA_ROUNDS=(\d{1,2})\]", topic)
    if not m:
        return (topic, None)
    try:
        value = int(m.group(1))
    except Exception:
        value = None
    clean = re.sub(r"\[MA_ROUNDS=\d{1,2}\]\s*", "", topic).strip()
    if value is None or not (1 <= value <= 10):
        return (clean, None)
    return (clean, value)


def _is_multi_agent_discuss_done(text: str) -> bool:
    t = text.strip()
    if not t:
        return False
    return any(p.search(t) for p in _MULTI_AGENT_DISCUSS_DONE_PATTERNS)


def _format_multi_agent_preflight_text(  # pyright: ignore[reportUnusedFunction]
    *,
    cfg: dict[str, object],
    topic: str,
) -> str:
    roles = [
        role
        for role in cast(list[dict[str, object]], cfg["roles"])
        if bool(role.get("enabled", True))
    ]
    mode = cast(str, cfg["mode"])
    rounds = cast(int, cfg["max_rounds"])
    mode_cn = "并行" if mode == "parallel" else "串行"
    lines = [
        "已进入多Agent准备阶段（尚未开始执行）。",
        f"- 当前模式: {mode_cn}（{mode}）",
        f"- 计划回合: {rounds}",
        f"- 角色数量: {len(roles)}",
        "",
        "角色分工:",
    ]
    for role in roles:
        name = str(role.get("name", role.get("id", "角色"))).strip() or "角色"
        duty = _multi_agent_system_prompt(role)
        lines.append(f"- {name}: {_compact_role_output(duty, 120)}")
    lines.extend(
        [
            "",
            "主控建议:",
            "- 先确认目标、交付形式、截止时间与约束。",
            "- 若希望更快出结论，可先改为 2 轮；若任务复杂建议 4 轮以上。",
            "",
            f"当前议题: {topic.strip() or '(未提供)'}",
            "",
            "回复以下任一指令继续:",
            "- 回复\u201c确认开始\u201d：按当前配置启动多Agent执行",
            "- 回复\u201c改为N轮\u201d：修改回合后继续等待确认",
            "- 回复\u201c取消\u201d：退出本次多Agent执行",
        ]
    )
    return "\n".join(lines)


def _multi_agent_module():  # noqa: ANN202
    from whaleclaw.agent import multi_agent

    return multi_agent


def multi_agent_system_prompt(role: dict[str, object]) -> str:
    return _multi_agent_module().multi_agent_system_prompt(role)


def compact_role_output(text: str, max_chars: int = 600) -> str:
    return _multi_agent_module().compact_role_output(text, max_chars)


def looks_like_bad_coordinator_output(text: str) -> bool:
    return _multi_agent_module().looks_like_bad_coordinator_output(text)


def looks_like_role_stall_output(text: str) -> bool:
    return _multi_agent_module().looks_like_role_stall_output(text)


def need_image_output(user_message: str) -> bool:
    return _multi_agent_module().need_image_output(user_message)


def extract_requested_deliverables(user_message: str) -> list[str]:
    return _multi_agent_module().extract_requested_deliverables(user_message)


def build_multi_agent_requirement_baseline(
    *,
    message: str,
    scenario: str,
    mode: str,
    max_rounds: int,
    requested_deliverables: list[str],
) -> str:
    return _multi_agent_module().build_multi_agent_requirement_baseline(
        message=message,
        scenario=scenario,
        mode=mode,
        max_rounds=max_rounds,
        requested_deliverables=requested_deliverables,
    )


_multi_agent_system_prompt = multi_agent_system_prompt
_compact_role_output = compact_role_output
_looks_like_bad_coordinator_output = looks_like_bad_coordinator_output
_looks_like_role_stall_output = looks_like_role_stall_output
_need_image_output = need_image_output
_extract_requested_deliverables = extract_requested_deliverables
_build_multi_agent_requirement_baseline = build_multi_agent_requirement_baseline

# Public aliases for cross-module import (functions defined with _ prefix).
multi_agent_cfg = _multi_agent_cfg
scenario_discuss_focus = _scenario_discuss_focus
scenario_delivery_focus = _scenario_delivery_focus
resolve_multi_agent_cfg = _resolve_multi_agent_cfg
is_multi_agent_confirm = _is_multi_agent_confirm
is_multi_agent_cancel = _is_multi_agent_cancel
extract_multi_agent_rounds = _extract_multi_agent_rounds
attach_rounds_marker = _attach_rounds_marker
extract_rounds_marker = _extract_rounds_marker
is_multi_agent_discuss_done = _is_multi_agent_discuss_done
format_multi_agent_preflight_text = _format_multi_agent_preflight_text
