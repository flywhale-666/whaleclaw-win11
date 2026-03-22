from __future__ import annotations

import pytest

from whaleclaw.tools.browser import BrowserTool


class _FakeBrowser:
    def __init__(self, *, connected: bool) -> None:
        self._connected = connected

    def is_connected(self) -> bool:
        return self._connected


class _FakeContext:
    def __init__(self, browser: _FakeBrowser) -> None:
        self.browser = browser


class _FakePage:
    def __init__(self, *, closed: bool, connected: bool) -> None:
        self._closed = closed
        self.context = _FakeContext(_FakeBrowser(connected=connected))

    def is_closed(self) -> bool:
        return self._closed


@pytest.mark.asyncio
async def test_ensure_browser_recreates_closed_page(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = BrowserTool()
    tool._page = _FakePage(closed=True, connected=False)
    tool._context = object()
    tool._browser = object()
    tool._playwright = object()

    fresh_page = object()
    dispose_calls = 0
    launch_calls = 0

    async def fake_dispose_browser() -> None:
        nonlocal dispose_calls
        dispose_calls += 1
        tool._page = None
        tool._context = None
        tool._browser = None
        tool._playwright = None

    async def fake_launch_browser() -> object:
        nonlocal launch_calls
        launch_calls += 1
        tool._page = fresh_page
        return fresh_page

    monkeypatch.setattr(tool, "_dispose_browser", fake_dispose_browser)
    monkeypatch.setattr(tool, "_launch_browser", fake_launch_browser)
    monkeypatch.setattr(BrowserTool, "_read_cdp_url", staticmethod(lambda: ""))

    page = await tool._ensure_browser()

    assert page is fresh_page
    assert dispose_calls == 1
    assert launch_calls == 1
