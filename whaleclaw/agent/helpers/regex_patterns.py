"""Centralized regex patterns and simple matching functions for the agent."""

from __future__ import annotations

import re

_IMG_MD_RE = re.compile(r"!\[([^\]]*)\]\(([A-Za-z]:\\[^)]+|/[^)]+)\)")
_ABS_IMAGE_PATH_RE = re.compile(
    r"([A-Za-z]:\\[^\s\"')]+?\.(?:png|jpg|jpeg|gif|webp)|/[^\s\"')]+?\.(?:png|jpg|jpeg|gif|webp))(?=[\s\"')]|$)",
    re.IGNORECASE,
)
_IMAGE_REFERENCE_RE = re.compile(
    r"(这张图|这张图片|这幅图|这幅图片|图里|图中|参考图|按这张|基于这张|用这张)",
    re.IGNORECASE,
)
_IMAGE_EDIT_FOLLOWUP_RE = re.compile(
    r"(改(?:成|下|一下)?|修改|调整|优化|增强|变得|变成|换成|把.+(?:改|变|换)|"
    r"让.+(?:改成|变成|换成)|"
    r"加上|加个|加一(?:个|只)?|加只|添加|增加|补上|再来(?:个|只)?|放一(?:个|只)?)",
    re.IGNORECASE,
)
_IMAGE_EDIT_SUBJECT_CONTINUATION_RE = re.compile(
    r"^\s*(?:请)?(?:帮我)?(?:让|给)\s*(?:这|那|它|他|她)"
    r"(?:只|个|头|名|位|条|匹|张|幅)?",
    re.IGNORECASE,
)
_IMAGE_REGENERATE_RE = re.compile(
    r"(重试|重做|重新生成|重生成|重新来|再来一版|再生成一次|再试一次|重画|这图不好看)",
    re.IGNORECASE,
)
_NANO_BANANA_RATIO_CLAUSE_RE = re.compile(
    r"[，,、;\s]*(?:图片)?(?:尺寸|比例|画幅|宽高比)"
    r"(?:改成|改为|是|为|设为|设置为|调整为|调成|[:：])?\s*"
    r"(?:\d{1,2}\s*:\s*\d{1,2}|\d{3,5}\s*[xX]\s*\d{3,5})",
    re.IGNORECASE,
)
_RATIO_ONLY_CHANGE_RE = re.compile(
    r"^\s*(?:请|帮我|把|将)?\s*(?:图片|图|尺寸|比例|画幅|宽高比)?\s*"
    r"(?:尺寸|比例|画幅|宽高比)\s*"
    r"(?:改成|改为|是|为|设为|设置为|调整为|调成|换成|改到|[:：])\s*"
    r"(?:\d{1,2}\s*:\s*\d{1,2}|\d{3,5}\s*[xX]\s*\d{3,5})\s*$",
    re.IGNORECASE,
)
_NANO_BANANA_REGENERATE_PREFIX_RE = re.compile(
    r"^\s*(?:请)?(?:帮我)?(?:再)?(?:重新生成|重生成|重试|再试一次|再生成一次|重新来|重做|重画)"
    r"(?:一下|下)?[，,、:\s]*",
    re.IGNORECASE,
)
_NANO_BANANA_MODEL_PREFIX_RE = re.compile(
    r"^\s*(?:用|改用|切换到)?\s*(?:香蕉2|香蕉pro)\s*",
    re.IGNORECASE,
)
_NANO_BANANA_TEXT_TO_IMAGE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:^|[\s,，])(?:文生图|生图|出图|作画|画一张|画一幅|画张|帮我画|给我画|生成一张|生成一幅|生成图片|生成图像)", re.IGNORECASE),
    re.compile(r"^[\s]*(?:请|帮我|给我|来)?(?:文生图|生图|出图|作画|画一张|画一幅|画张|生成一张|生成一幅|生成图片|生成图像)", re.IGNORECASE),
)
_EVOMAP_LINE_RE = re.compile(r"^\s*-\s*([^:]+):\s*(.+?)\s*$")
_VERSION_SUFFIX_RE = re.compile(r"_V\d+$", re.IGNORECASE)
_COORDINATOR_ASK_RE = re.compile(
    r"(?:你要(?:我|什么)|需要你(?:提供|告诉|回复|回答|确认|选择)|"
    r"请(?:告诉|选择|提供|告知)我|"
    r"(?:按|用)(?:下面|以下)(?:模板|格式)(?:回|填|答))",
)
_EVOMAP_CHOICE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*(?:选|选择)?\s*([ABCabc])\s*$"),
    re.compile(r"^\s*(?:选|选择)?\s*([123])\s*$"),
)
_ASSISTANT_NAME_RESET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"恢复默认名字"),
    re.compile(r"改回\s*whaleclaw", re.IGNORECASE),
    re.compile(r"还是叫\s*whaleclaw", re.IGNORECASE),
)
_ASSISTANT_NAME_SET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?:以后|从现在起|今后|之后|开始)\s*(?:你|机器人|助手)?\s*(?:就)?(?:叫|改叫|改名叫)\s*([^\s，。！？!?、,]{1,24})"
    ),
    re.compile(r"(?:把你|你)\s*(?:改名|改名为|名字改成|名字改为)\s*([^\s，。！？!?、,]{1,24})"),
    re.compile(r"^\s*(?:你|助手|机器人)\s*(?:就)?叫\s*([^\s，。！？!?、,]{1,24})\s*$"),
)
_USE_CMD_RE = re.compile(r"^\s*/use\s+([^\s]+)\s*(.*)$", re.IGNORECASE | re.DOTALL)
_USE_CLEAR_IDS = {"clear", "none", "off", "default", "reset"}
_TASK_DONE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*任务完成\s*$"),
    re.compile(r"^\s*完成任务\s*$"),
    re.compile(r"^\s*任务结束\s*$"),
    re.compile(r"^\s*结束任务\s*$"),
    re.compile(r"^\s*完成了?\s*$"),
    re.compile(r"^\s*结束了?\s*$"),
    re.compile(r"^\s*可以了?\s*$"),
    re.compile(r"^\s*ok\s*$", re.IGNORECASE),
)
_SKILL_ACTIVATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:使用|调用|启动|启用|走|用).{0,24}(?:技能|skill)", re.IGNORECASE),
    re.compile(r"(?:技能|skill).{0,16}(?:文生图|图生图|处理|执行|联调)", re.IGNORECASE),
)
_NON_SKILL_STEP_RE = re.compile(
    r"(?:做|创建|生成|写|制作|出|新建|帮我做|帮我出|帮我写|帮我制作|帮我生成)"
    r".{0,20}"
    r"(?:ppt|pptx|word|docx|excel|xlsx|文档|幻灯片|报告|方案|简历|表格|页|册子|文件)",
    re.IGNORECASE,
)
_MULTI_AGENT_CONFIRM_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"^\s*(确认|确认开始|开始执行|开始多agent|开始多\s*agent|执行吧|开始吧)\s*$",
        re.IGNORECASE,
    ),
    re.compile(r"^\s*/multi\s+go\s*$", re.IGNORECASE),
)
_MULTI_AGENT_CANCEL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*(取消|取消执行|先别执行|暂停|停止)\s*$", re.IGNORECASE),
    re.compile(r"^\s*/multi\s+cancel\s*$", re.IGNORECASE),
)
_CN_DIGIT_MAP: dict[str, int] = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}
_CN_DIGIT_CHARS = "".join(_CN_DIGIT_MAP.keys())
_MULTI_AGENT_ROUNDS_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        rf"(?:改为|改成|设为|设置为|设置|调整为|改到)\s*(\d{{1,2}}|[{_CN_DIGIT_CHARS}])\s*轮",
        re.IGNORECASE,
    ),
    re.compile(rf"^\s*(\d{{1,2}}|[{_CN_DIGIT_CHARS}])\s*轮\s*$", re.IGNORECASE),
    re.compile(r"^\s*/multi\s+rounds\s+(\d{1,2})\s*$", re.IGNORECASE),
)
_MULTI_AGENT_DISCUSS_DONE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*(需求确认|确认需求|需求已确认|信息齐了|进入执行确认)\s*$"),
    re.compile(r"^\s*/multi\s+ready\s*$", re.IGNORECASE),
)
_MULTI_AGENT_SCENARIO_LABELS: dict[str, str] = {
    "product_design": "产品设计",
    "content_creation": "内容创作",
    "software_development": "软件开发",
    "data_analysis_decision": "数据分析决策",
    "scientific_research": "科研",
    "intelligent_assistant": "智能助理",
    "workflow_automation": "自动化工作流",
}
_TEXT_TO_IMAGE_RE = re.compile(
    r"(?:文生图|生图|出图|作画|画图|生成.{0,4}图|做.{0,4}图|画.{0,4}图|"
    r"给我.{0,4}图|来.{0,4}图|要.{0,4}图|出一张|生成一张|做一张|画一张|来一张)",
    re.IGNORECASE,
)
_NANO_BANANA_BASH_RE = re.compile(r"test_nano_banana(?:_\d+)?\.py", re.IGNORECASE)
_NOT_IMAGE_PROMPT_RE = re.compile(
    r"^(?:请|帮我|给我|麻烦)?\s*(?:讲|说|聊|介绍|解释|分析|翻译|写|搜索|查|找|看|打开|关闭|截图|录屏|发送|转发)",
    re.IGNORECASE,
)
_NOT_IMAGE_PROMPT_QA_RE = re.compile(
    r"(?:怎么|如何|什么|为什么|多少|哪里|能不能|可以吗|是不是|是什么)",
    re.IGNORECASE,
)
_NOT_IMAGE_PROMPT_DESKTOP_RE = re.compile(
    r"^(?:请|帮我|给我)?\s*(?:将|把)?\s*(?:桌面|屏幕|窗口|页面)",
    re.IGNORECASE,
)

_TOOL_HINTS: dict[str, str] = {
    "browser": "搜索相关资料",
    "web_fetch": "抓取网页正文",
    "desktop_capture": "点亮并截图桌面",
    "bash": "执行命令",
    "process": "查看或结束进程",
    "file_write": "生成文件",
    "file_read": "读取文件",
    "file_edit": "编辑文件",
    "patch_apply": "应用补丁",
    "ppt_edit": "修改现有PPT",
    "docx_edit": "修改现有Word",
    "xlsx_edit": "修改现有Excel",
    "memory_search": "检索长期记忆",
    "memory_add": "写入长期记忆",
    "memory_list": "查看长期记忆",
    "skill": "查找技能",
}

_SKILL_LOCK_STATUS_KEYWORDS: tuple[str, ...] = (
    "当前技能",
    "锁定技能",
    "锁定了什么",
    "在哪个技能",
    "哪个技能",
    "技能锁",
    "锁定在",
    "当前锁",
    "locked skill",
)


def _is_skill_lock_status_question(text: str) -> bool:
    """Return True when the message is a plain query about the current skill lock.

    Only meant to be called when a lock is already active; avoids false positives
    on unrelated messages that happen to contain "技能" or similar words.
    """
    q = text.strip()
    if not q or len(q) > 60:
        return False
    return any(kw in q for kw in _SKILL_LOCK_STATUS_KEYWORDS)


def _is_evomap_status_question(text: str) -> bool:
    q = text.lower().strip()
    if not q:
        return False
    if "evomap" not in q and "evo map" not in q:
        return False
    status_hints = (
        "开着",
        "开启",
        "启用",
        "打开",
        "关闭",
        "状态",
        "on",
        "off",
        "enabled",
    )
    return any(h in q for h in status_hints)


def _is_compound_task_message(message: str) -> bool:
    """判断用户消息是否为复合任务（包含非技能步骤，如创建文档再生图再插入）。

    复合任务特征：消息同时含有「创建/生成文档类」意图，
    说明 LLM 需要多步骤编排，不能立即锁定单一技能。
    """
    return bool(_NON_SKILL_STEP_RE.search(message))


def _is_creation_task_message(text: str) -> bool:
    low = text.lower().strip()
    if not low:
        return False
    keys = (
        "ppt",
        "幻灯片",
        "演示文稿",
        "文档",
        "报告",
        "方案",
        "写",
        "生成",
        "制作",
        "整理",
        "设计",
        "润色",
        "总结",
        "改写",
        "脚本",
        "代码",
        "html",
        "页面",
        "海报",
        "计划",
        "create",
        "generate",
        "draft",
        "design",
        "write",
        "build",
        "compose",
    )
    return any(k in low for k in keys)


# Public aliases for cross-module import.
ASSISTANT_NAME_RESET_PATTERNS = _ASSISTANT_NAME_RESET_PATTERNS
ASSISTANT_NAME_SET_PATTERNS = _ASSISTANT_NAME_SET_PATTERNS
COORDINATOR_ASK_RE = _COORDINATOR_ASK_RE
IMG_MD_RE = _IMG_MD_RE
MULTI_AGENT_SCENARIO_LABELS = _MULTI_AGENT_SCENARIO_LABELS
NANO_BANANA_BASH_RE = _NANO_BANANA_BASH_RE
NANO_BANANA_RATIO_CLAUSE_RE = _NANO_BANANA_RATIO_CLAUSE_RE
NANO_BANANA_TEXT_TO_IMAGE_PATTERNS = _NANO_BANANA_TEXT_TO_IMAGE_PATTERNS
RATIO_ONLY_CHANGE_RE = _RATIO_ONLY_CHANGE_RE
SKILL_ACTIVATION_PATTERNS = _SKILL_ACTIVATION_PATTERNS
TASK_DONE_PATTERNS = _TASK_DONE_PATTERNS
TEXT_TO_IMAGE_RE = _TEXT_TO_IMAGE_RE
TOOL_HINTS = _TOOL_HINTS
USE_CLEAR_IDS = _USE_CLEAR_IDS
USE_CMD_RE = _USE_CMD_RE
VERSION_SUFFIX_RE = _VERSION_SUFFIX_RE
is_compound_task_message = _is_compound_task_message
is_creation_task_message = _is_creation_task_message
is_evomap_status_question = _is_evomap_status_question
is_skill_lock_status_question = _is_skill_lock_status_question
