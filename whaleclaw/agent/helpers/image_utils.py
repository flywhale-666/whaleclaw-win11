"""Image processing and Nano-Banana command helpers for the agent."""

from __future__ import annotations

import base64
import re
import shlex
from pathlib import Path

from whaleclaw.agent.helpers.regex_patterns import (
    _ABS_IMAGE_PATH_RE,
    _IMAGE_EDIT_FOLLOWUP_RE,
    _IMAGE_EDIT_SUBJECT_CONTINUATION_RE,
    _IMAGE_REFERENCE_RE,
    _IMAGE_REGENERATE_RE,
    _IMG_MD_RE,
    _NANO_BANANA_MODEL_PREFIX_RE,
    _NANO_BANANA_RATIO_CLAUSE_RE,
    _NANO_BANANA_REGENERATE_PREFIX_RE,
    _NANO_BANANA_TEXT_TO_IMAGE_PATTERNS,
    _NOT_IMAGE_PROMPT_DESKTOP_RE,
    _NOT_IMAGE_PROMPT_QA_RE,
    _NOT_IMAGE_PROMPT_RE,
    _RATIO_ONLY_CHANGE_RE,
    _TASK_DONE_PATTERNS,
    _TEXT_TO_IMAGE_RE,
    _TOOL_HINTS,
)
from whaleclaw.agent.helpers.skill_lock import (
    is_nano_banana_activation_message as _is_nano_banana_activation_message,
)
from whaleclaw.agent.helpers.skill_lock import (
    is_nano_banana_control_message as _is_nano_banana_control_message,
)
from whaleclaw.providers.base import ImageContent, ToolCall
from whaleclaw.sessions.manager import Session
from whaleclaw.skills.parser import Skill
from whaleclaw.utils.log import get_logger

log = get_logger(__name__)

_NANO_BANANA_PARALLEL_BATCH_SIZE = 5
_NANO_BANANA_PARALLEL_BATCH_DELAY_S = 1.5

_DISPLAY_TO_MODEL: dict[str, str] = {
    "香蕉2": "gemini-3.1-flash-image-preview",
    "香蕉pro": "nano-banana-2",
}


def _make_plan_hint(tool_names: list[str], user_msg: str) -> str:
    """Generate a brief plan message when LLM jumps straight to tool calls."""
    steps: list[str] = []
    seen: set[str] = set()
    for name in tool_names:
        if name in seen:
            continue
        seen.add(name)
        steps.append(_TOOL_HINTS.get(name, f"调用 {name}"))
    plan = "、".join(steps)
    return f"好的，我来处理。正在{plan}…\n\n"


def _fix_image_paths(text: str, known_paths: list[str] | None = None) -> str:
    """Validate image paths in markdown; fix fabricated paths using known real ones."""
    unused_real = list(known_paths or [])

    def _replace(m: re.Match[str]) -> str:
        alt, raw_path = m.group(1), m.group(2)
        fp = Path(raw_path)
        if fp.is_file():
            return m.group(0)

        for i, real in enumerate(unused_real):
            rp = Path(real)
            if rp.is_file():
                unused_real.pop(i)
                log.info("fix_image_path.known", original=raw_path, found=real)
                return f"![{alt}]({real})"

        stem = fp.stem
        hash_m = re.search(r"_([0-9a-f]{6,8})$", stem)
        if hash_m and fp.parent.is_dir():
            suffix = hash_m.group(0) + fp.suffix
            for candidate in fp.parent.iterdir():
                if candidate.name.endswith(suffix) and candidate.is_file():
                    log.info("fix_image_path.fuzzy", original=raw_path, found=str(candidate))
                    return f"![{alt}]({candidate})"

        log.warning("fix_image_path.removed", path=raw_path)
        return f"[图片未找到: {alt}]"

    return _IMG_MD_RE.sub(_replace, text)


def _message_may_need_prior_images(message: str) -> bool:
    """Detect whether the user is referring to a previously uploaded image."""
    return bool(_IMAGE_REFERENCE_RE.search(message))


def _message_is_ratio_only_change(message: str) -> bool:  # pyright: ignore[reportUnusedFunction]
    """Detect messages that only request a ratio/size change, not content editing."""
    return bool(_RATIO_ONLY_CHANGE_RE.search(message))


def _is_nano_banana_relevant_message(  # pyright: ignore[reportUnusedFunction]
    message: str,
    *,
    has_images: bool = False,
) -> bool:
    """Check whether a message should be handled by nano-banana shortcut."""
    stripped = message.strip()
    if not stripped:
        return False
    if has_images:
        return True
    if _extract_input_image_paths_from_text(stripped):
        return True
    if _IMG_MD_RE.search(stripped):
        return True
    if _TEXT_TO_IMAGE_RE.search(stripped):
        return True
    if _message_requests_image_edit(stripped):
        return True
    if _message_requests_image_regenerate(stripped):
        return True
    if _is_nano_banana_control_message(stripped):
        return True
    if _is_nano_banana_activation_message(stripped):
        return True
    if _RATIO_ONLY_CHANGE_RE.search(stripped):
        return True
    if _NANO_BANANA_RATIO_CLAUSE_RE.search(stripped):
        return True
    return False


def _is_clearly_unrelated_to_image(message: str) -> bool:
    """Return True only for messages that are obviously NOT image prompts."""
    stripped = message.strip()
    if not stripped:
        return True
    if any(pattern.fullmatch(stripped) for pattern in _TASK_DONE_PATTERNS):
        return True
    if _NOT_IMAGE_PROMPT_RE.search(stripped):
        return True
    if _NOT_IMAGE_PROMPT_QA_RE.search(stripped):
        return True
    if _NOT_IMAGE_PROMPT_DESKTOP_RE.search(stripped):
        return True
    return False


def _message_requests_image_edit(message: str) -> bool:
    """Detect edit follow-ups that should continue from the latest output image."""
    if _message_may_need_prior_images(message):
        return True
    if _IMAGE_EDIT_SUBJECT_CONTINUATION_RE.search(message):
        return True
    return bool(_IMAGE_EDIT_FOLLOWUP_RE.search(message))


def _message_requests_image_regenerate(message: str) -> bool:
    """Detect reruns that should go back to the original input image set."""
    return bool(_IMAGE_REGENERATE_RE.search(message))


def _is_parallelizable_nano_bash_call(tc: ToolCall) -> bool:
    from whaleclaw.agent.helpers.regex_patterns import _NANO_BANANA_BASH_RE

    return tc.name == "bash" and bool(
        _NANO_BANANA_BASH_RE.search(str(tc.arguments.get("command", "")))
    )


def _skill_requires_images(skills: list[Skill]) -> bool:
    """Return whether any active skill explicitly requires image inputs."""
    for skill in skills:
        guard = skill.param_guard
        if guard is None or not guard.enabled:
            continue
        for param in guard.params:
            if param.type.strip().lower() == "images":
                return True
    return False


def _mime_from_image_path(path: Path) -> str:
    """Infer an image mime type from the local file suffix."""
    suffix = path.suffix.lower()
    if suffix == ".png":
        return "image/png"
    if suffix == ".webp":
        return "image/webp"
    if suffix == ".gif":
        return "image/gif"
    return "image/jpeg"


def _recover_recent_session_images(  # pyright: ignore[reportUnusedFunction]
    session: Session | None,
    *,
    limit: int = 4,
) -> list[ImageContent]:
    """Reload recent local image references from prior user messages."""
    paths = _recover_recent_session_image_paths(session, limit=limit)
    recovered: list[ImageContent] = []
    for raw_path in paths:
        path = Path(raw_path).expanduser()
        try:
            data = path.read_bytes()
        except OSError:
            continue
        recovered.append(ImageContent(
            mime=_mime_from_image_path(path),
            data=base64.b64encode(data).decode("ascii"),
        ))
    return recovered


def _load_images_from_paths(paths: list[str]) -> list[ImageContent]:
    """Read local image paths into inline message payloads."""
    recovered: list[ImageContent] = []
    for raw_path in paths:
        path = Path(raw_path).expanduser()
        try:
            data = path.read_bytes()
        except OSError:
            continue
        recovered.append(ImageContent(
            mime=_mime_from_image_path(path),
            data=base64.b64encode(data).decode("ascii"),
        ))
    return recovered


def _recover_latest_generated_image(session: Session | None) -> list[ImageContent]:
    """Return only the latest generated image for edit-followup turns."""
    if session is None:
        return []
    metadata = session.metadata if isinstance(session.metadata, dict) else {}  # pyright: ignore[reportUnnecessaryIsInstance]
    latest_generated = str(metadata.get("last_generated_image_path", "")).strip()
    if not latest_generated:
        return []
    return _load_images_from_paths([latest_generated])


def _recover_last_input_images(session: Session | None) -> list[ImageContent]:
    """Return the last explicit input image set for regenerate-followup turns."""
    if session is None:
        return []
    metadata = session.metadata if isinstance(session.metadata, dict) else {}  # pyright: ignore[reportUnnecessaryIsInstance]
    raw_paths = metadata.get("last_input_image_paths", [])
    if not isinstance(raw_paths, list):
        return []
    paths = [str(item).strip() for item in raw_paths if str(item).strip()]  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    return _load_images_from_paths(paths)


def _recover_recent_session_image_paths(
    session: Session | None,
    *,
    limit: int = 4,
) -> list[str]:
    """Collect recent valid local image paths, preferring latest generated outputs."""
    if session is None:
        return []

    recovered: list[str] = []
    seen_paths: set[str] = set()

    metadata = session.metadata if isinstance(session.metadata, dict) else {}  # pyright: ignore[reportUnnecessaryIsInstance]
    latest_generated = str(metadata.get("last_generated_image_path", "")).strip()
    if latest_generated:
        path = Path(latest_generated).expanduser()
        if path.is_file():
            seen_paths.add(str(path))
            recovered.append(str(path))
            if len(recovered) >= limit:
                return recovered

    for msg in reversed(session.messages):
        if msg.role not in {"user", "assistant"} or not msg.content:
            continue
        markdown_paths = [match.group(2).strip() for match in _IMG_MD_RE.finditer(msg.content)]
        plain_paths = [match.group(1).strip() for match in _ABS_IMAGE_PATH_RE.finditer(msg.content)]
        for raw_path in [*markdown_paths, *plain_paths]:
            path = Path(raw_path).expanduser()
            resolved = str(path)
            if resolved in seen_paths or not path.is_file():
                continue
            seen_paths.add(resolved)
            recovered.append(resolved)
            if len(recovered) >= limit:
                return recovered
    return recovered


def _extract_input_image_paths_from_text(
    text: str,
    *,
    limit: int = 8,
) -> list[str]:
    """Extract unique local image paths from the current user message text."""
    extracted: list[str] = []
    seen_paths: set[str] = set()
    markdown_paths = [match.group(2).strip() for match in _IMG_MD_RE.finditer(text)]
    plain_paths = [match.group(1).strip() for match in _ABS_IMAGE_PATH_RE.finditer(text)]
    for raw_path in [*markdown_paths, *plain_paths]:
        path = Path(raw_path).expanduser()
        resolved = str(path)
        if resolved in seen_paths or not path.is_file():
            continue
        seen_paths.add(resolved)
        extracted.append(resolved)
        if len(extracted) >= limit:
            break
    return extracted


def _strip_inline_image_markdown(text: str) -> str:
    """Remove appended local image markdown from a user prompt string."""
    stripped = _IMG_MD_RE.sub("", text)
    stripped = _ABS_IMAGE_PATH_RE.sub("", stripped)
    stripped = stripped.replace("(用户发送了图片)", "")
    return stripped.strip()


def _clean_nano_banana_prompt_delta(message: str) -> str:
    cleaned = _strip_inline_image_markdown(message).strip()
    if not cleaned:
        return ""
    quoted_prompt_match = re.search(
        r'(?:提示词|prompt)\s*[=:：]\s*[""](.+?)[""](?:[，,、；;]|$)',
        cleaned,
        re.IGNORECASE,
    )
    if quoted_prompt_match:
        return quoted_prompt_match.group(1).strip()
    prompt_match = re.search(
        r"(?:提示词|prompt)\s*[=:：]\s*([^，,、；;\n]+)",
        cleaned,
        re.IGNORECASE,
    )
    if prompt_match:
        return prompt_match.group(1).strip().strip("\"'""")
    cleaned = _NANO_BANANA_MODEL_PREFIX_RE.sub("", cleaned)
    cleaned = _NANO_BANANA_REGENERATE_PREFIX_RE.sub("", cleaned)
    cleaned = _NANO_BANANA_RATIO_CLAUSE_RE.sub("", cleaned)
    cleaned = re.sub(r"^[\s，,、]*(?:请|帮我|麻烦|把|将|给我)?\s*(?:图片|图|这张图|那张图)?\s*$", "", cleaned)
    cleaned = re.sub(r"^[，,、；;：:\s]+", "", cleaned)
    cleaned = re.sub(r"[，,、；;：:\s]+$", "", cleaned)
    return cleaned.strip()


def _merge_nano_banana_prompt(
    *,
    previous_prompt: str,
    message: str,
    regenerate: bool,
    image_edit: bool,
) -> str:
    previous = _strip_inline_image_markdown(previous_prompt).strip()
    delta = _clean_nano_banana_prompt_delta(message)
    if not previous:
        return delta
    if not delta:
        return previous
    if delta in previous:
        return previous
    if regenerate or image_edit:
        return (
            f"{previous}\n\n"
            "在保留原始主题、主体和核心场景设定的前提下，按以下要求重新生成或修改："
            f"{delta}"
        )
    return delta


def _recover_latest_generated_image_path(session: Session | None) -> str:
    if session is None:
        return ""
    metadata = session.metadata if isinstance(session.metadata, dict) else {}  # pyright: ignore[reportUnnecessaryIsInstance]
    latest_generated = str(metadata.get("last_generated_image_path", "")).strip()
    path = Path(latest_generated).expanduser()
    if latest_generated and path.is_file():
        return str(path)
    return ""


def _recover_last_input_image_paths(session: Session | None) -> list[str]:
    if session is None:
        return []
    metadata = session.metadata if isinstance(session.metadata, dict) else {}  # pyright: ignore[reportUnnecessaryIsInstance]
    raw_paths = metadata.get("last_input_image_paths", [])
    if not isinstance(raw_paths, list):
        return []
    output: list[str] = []
    for item in raw_paths:  # pyright: ignore[reportUnknownVariableType]
        path = Path(str(item).strip()).expanduser()  # pyright: ignore[reportUnknownArgumentType]
        if path.is_file():
            output.append(str(path))
    return output


def _recover_last_nano_banana_mode(session: Session | None) -> str:
    if session is None:
        return ""
    metadata = session.metadata if isinstance(session.metadata, dict) else {}  # pyright: ignore[reportUnnecessaryIsInstance]
    raw_mode = str(metadata.get("last_nano_banana_mode", "")).strip().lower()
    if raw_mode in {"text", "edit"}:
        return raw_mode
    state_map_raw = metadata.get("skill_param_state", {})
    if not isinstance(state_map_raw, dict):
        return ""
    nano_state_raw = state_map_raw.get("nano-banana-image-t8", {})  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
    if not isinstance(nano_state_raw, dict):
        return ""
    state_mode = str(nano_state_raw.get("__last_mode__", "")).strip().lower()  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    if state_mode in {"text", "edit"}:
        return state_mode
    return ""


def _should_continue_nano_banana_last_mode(  # pyright: ignore[reportUnusedFunction]
    llm_message: str,
    session: Session | None,
) -> bool:
    if session is None:
        return False
    if _recover_last_nano_banana_mode(session) != "edit":
        return False
    stripped = llm_message.strip()
    if not stripped:
        return False
    if any(pattern.fullmatch(stripped) for pattern in _TASK_DONE_PATTERNS):
        return False
    if _is_nano_banana_control_message(stripped) or _is_nano_banana_activation_message(stripped):
        return False
    if _message_requests_image_regenerate(stripped):
        return False
    return bool(_clean_nano_banana_prompt_delta(stripped))


def _resolve_nano_banana_input_paths(
    llm_message: str,
    session: Session | None,
) -> list[str]:
    """Resolve the input image paths for a nano-banana execution turn."""
    explicit_paths = _extract_input_image_paths_from_text(llm_message)
    if explicit_paths:
        return explicit_paths
    if any(p.search(llm_message) for p in _NANO_BANANA_TEXT_TO_IMAGE_PATTERNS):
        return []
    if _message_requests_image_regenerate(llm_message):
        if _recover_last_nano_banana_mode(session) == "text":
            return []
        return _recover_last_input_image_paths(session)
    if _message_requests_image_edit(llm_message):
        latest_generated = _recover_latest_generated_image_path(session)
        if latest_generated:
            return [latest_generated]
        return _recover_last_input_image_paths(session)
    return []


def _build_nano_banana_command(
    *,
    mode: str,
    model_display: str,
    prompt: str,
    input_paths: list[str],
    ratio: str,
    base_url: str = "",
) -> str:
    """Build the fixed nano-banana script command."""
    script_path = (
        Path.home()
        / ".whaleclaw"
        / "workspace"
        / "skills"
        / "nano-banana-image-t8"
        / "scripts"
        / "test_nano_banana_2.py"
    )
    project_root = Path(__file__).resolve().parents[3]
    python_candidates = (
        project_root / "python" / "python.exe",
        project_root / "python" / "bin" / "python3.12",
        project_root / "python" / "bin" / "python3",
    )
    python_cmd = next(
        (str(p) for p in python_candidates if p.is_file()),
        "./python/bin/python3.12",
    )
    model_id = _DISPLAY_TO_MODEL.get(model_display, model_display)
    model_flag = "--edit-model" if mode == "edit" else "--model"
    parts = [
        python_cmd,
        shlex.quote(str(script_path)),
        "--mode",
        shlex.quote(mode),
        model_flag,
        shlex.quote(model_id),
        "--prompt",
        shlex.quote(prompt),
        "--aspect-ratio",
        shlex.quote(ratio or "auto"),
    ]
    if base_url.strip():
        parts.extend(["--base-url", shlex.quote(base_url.strip())])
    if model_display == "香蕉pro":
        parts.extend(["--image-size", "2K"])
    for path in input_paths:
        parts.extend(["--input-image", shlex.quote(path)])
    return " ".join(parts)


# Public aliases for cross-module import.
DISPLAY_TO_MODEL = _DISPLAY_TO_MODEL
NANO_BANANA_PARALLEL_BATCH_DELAY_S = _NANO_BANANA_PARALLEL_BATCH_DELAY_S
NANO_BANANA_PARALLEL_BATCH_SIZE = _NANO_BANANA_PARALLEL_BATCH_SIZE
build_nano_banana_command = _build_nano_banana_command
clean_nano_banana_prompt_delta = _clean_nano_banana_prompt_delta
extract_input_image_paths_from_text = _extract_input_image_paths_from_text
fix_image_paths = _fix_image_paths
is_clearly_unrelated_to_image = _is_clearly_unrelated_to_image
is_nano_banana_relevant_message = _is_nano_banana_relevant_message
is_parallelizable_nano_bash_call = _is_parallelizable_nano_bash_call
load_images_from_paths = _load_images_from_paths
make_plan_hint = _make_plan_hint
merge_nano_banana_prompt = _merge_nano_banana_prompt
message_is_ratio_only_change = _message_is_ratio_only_change
message_may_need_prior_images = _message_may_need_prior_images
message_requests_image_edit = _message_requests_image_edit
message_requests_image_regenerate = _message_requests_image_regenerate
mime_from_image_path = _mime_from_image_path
recover_last_input_image_paths = _recover_last_input_image_paths
recover_last_input_images = _recover_last_input_images
recover_last_nano_banana_mode = _recover_last_nano_banana_mode
recover_latest_generated_image = _recover_latest_generated_image
recover_latest_generated_image_path = _recover_latest_generated_image_path
recover_recent_session_image_paths = _recover_recent_session_image_paths
recover_recent_session_images = _recover_recent_session_images
resolve_nano_banana_input_paths = _resolve_nano_banana_input_paths
should_continue_nano_banana_last_mode = _should_continue_nano_banana_last_mode
skill_requires_images = _skill_requires_images
strip_inline_image_markdown = _strip_inline_image_markdown
