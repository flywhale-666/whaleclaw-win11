"""Shared test fixtures for WhaleClaw."""

from __future__ import annotations

import pytest

from whaleclaw.config.loader import reset_config
from whaleclaw.config.schema import AgentConfig, GatewayConfig, WhaleclawConfig


@pytest.fixture()
def default_config() -> WhaleclawConfig:
    """Return a default WhaleclawConfig for testing."""
    return WhaleclawConfig()


@pytest.fixture()
def custom_config() -> WhaleclawConfig:
    """Return a customised config for testing."""
    return WhaleclawConfig(
        gateway=GatewayConfig(port=19000, bind="0.0.0.0", verbose=True),
        agent=AgentConfig(model="anthropic/claude-sonnet-4-20250514"),
    )


@pytest.fixture(autouse=True)
def _reset_global_config() -> None:
    """Ensure global config is reset between tests."""
    reset_config()


@pytest.fixture(autouse=True)
def _reset_skill_hooks_globals() -> None:
    """Ensure skill hooks global state is reset between tests."""
    from whaleclaw.agent.helpers import tool_execution
    tool_execution._active_skill_hooks = None

    from whaleclaw.skills import hooks as hooks_mod
    hooks_mod._hooks_cache.clear()


@pytest.fixture(autouse=True)
def _reset_agent_module_patches() -> None:
    """Restore agent module globals that manual MonkeyPatch may leak."""
    import whaleclaw.agent.single_agent as sa

    orig_execute_tool = sa._execute_tool
    orig_route_skills = sa._assembler.route_skills
    yield
    sa._execute_tool = orig_execute_tool
    sa._assembler.route_skills = orig_route_skills
