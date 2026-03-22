"""Xiaomi MiMo provider adapter (OpenAI-compatible)."""

from __future__ import annotations

from whaleclaw.providers.openai_compat import OpenAICompatProvider


class XiaomiProvider(OpenAICompatProvider):
    """Xiaomi MiMo API (MiMo-V2-Pro, etc.)."""

    provider_name = "xiaomi"
    default_base_url = "https://api.xiaomimimo.com/v1"
    env_key = "XIAOMI_MIMO_API_KEY"
