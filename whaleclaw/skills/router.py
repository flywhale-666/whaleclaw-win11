"""Skill routing by keyword matching."""

from __future__ import annotations

import re

from whaleclaw.skills.parser import Skill


class SkillRouter:
    """Route user messages to skills by keyword matching."""

    def route(
        self,
        user_message: str,
        available_skills: list[Skill],
    ) -> list[Skill]:
        """Return all matched skills by /use command, explicit mention, or trigger."""
        msg = user_message.strip()
        if msg.startswith("/use "):
            skill_id = msg[5:].strip().lower()
            for s in available_skills:
                if s.id.lower() == skill_id:
                    return [s]

        # Explicit skill mention in natural language:
        # e.g. "用 ppt-generator 这个技能" / "use skill ppt-generator"
        # or just mentioning the skill name directly.
        explicit: list[Skill] = []
        for s in available_skills:
            if self._mentions_skill(msg, s):
                explicit.append(s)
        if explicit:
            explicit.sort(key=lambda x: x.id)
            return explicit

        matched = [s for s in available_skills if self._matches_trigger(msg, s)]
        matched.sort(key=lambda x: x.id)
        return matched

    _AUTOTRIGGER_SPLIT_RE = re.compile(r"[，,、/\s]+")

    def _auto_triggers(self, skill: Skill) -> list[str]:
        """Derive triggers from name + description when triggers list is empty."""
        tokens: list[str] = []
        if skill.name:
            tokens.extend(
                t for t in self._AUTOTRIGGER_SPLIT_RE.split(skill.name) if len(t) >= 2
            )
            tokens.append(skill.name)
        if skill.id:
            tokens.append(skill.id)
            tokens.append(skill.id.replace("-", " "))
        if skill.trigger_description:
            tokens.extend(
                t
                for t in self._AUTOTRIGGER_SPLIT_RE.split(skill.trigger_description)
                if len(t) >= 2
            )
        seen: set[str] = set()
        out: list[str] = []
        for t in tokens:
            key = t.strip().lower()
            if key and key not in seen:
                seen.add(key)
                out.append(t.strip())
        return out

    def _matches_trigger(self, message: str, skill: Skill) -> bool:
        """Return whether any declared or derived trigger is present in the message."""
        triggers = skill.triggers if skill.triggers else self._auto_triggers(skill)
        if not triggers:
            return False
        lower = message.lower()
        return any(t.lower() in lower for t in triggers)

    @staticmethod
    def _norm_text(text: str) -> str:
        return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", text.lower())

    def _mentions_skill(self, message: str, skill: Skill) -> bool:
        lower = message.lower()
        msg_norm = self._norm_text(message)
        for raw in (skill.id, skill.name):
            token = raw.strip().lower()
            if not token:
                continue
            if token in lower:
                return True
            norm = self._norm_text(token)
            if len(norm) >= 5 and norm in msg_norm:
                return True
        return False
