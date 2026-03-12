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
        max_skills: int = 2,
    ) -> list[Skill]:
        """Select top skills by /use command or keyword score."""
        msg = user_message.strip()
        lower = msg.lower()
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
            return explicit[:max_skills]

        scored = [(self._score(msg, s), s) for s in available_skills]
        scored = [(score, s) for score, s in scored if score > 0]
        scored.sort(key=lambda x: (-x[0], x[1].id))
        return [s for _, s in scored[:max_skills]]

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

    def _score(self, message: str, skill: Skill) -> float:
        """Return hit_count / total_triggers, 0 if no triggers."""
        triggers = skill.triggers if skill.triggers else self._auto_triggers(skill)
        if not triggers:
            return 0.0
        lower = message.lower()
        hits = sum(1 for t in triggers if t.lower() in lower)
        return hits / len(triggers)

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
