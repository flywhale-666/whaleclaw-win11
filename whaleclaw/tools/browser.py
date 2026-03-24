"""Browser control tool powered by Playwright (uses local Chrome)."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

from whaleclaw.config.paths import CONFIG_FILE, WHALECLAW_HOME
from whaleclaw.tools.base import Tool, ToolDefinition, ToolParameter, ToolResult
from whaleclaw.utils.log import get_logger

log = get_logger(__name__)

_ACTIONS = [
    "navigate",
    "screenshot",
    "click",
    "type",
    "get_text",
    "evaluate",
    "search_images",
    "upload",
    "back",
    "close",
]

_SCREENSHOT_DIR = WHALECLAW_HOME / "screenshots"
_DOWNLOAD_DIR = WHALECLAW_HOME / "downloads"

_VIEWPORT = {"width": 1280, "height": 800}
_TIMEOUT = 15_000
_MAX_INTERACTIVE_ITEMS = 30

_EXTRACT_INTERACTIVE_JS = """
() => {
    const items = [];
    const seen = new Set();
    const vw = window.innerWidth, vh = window.innerHeight;

    function isVisible(el) {
        const r = el.getBoundingClientRect();
        if (r.width < 5 || r.height < 5) return false;
        const style = getComputedStyle(el);
        return style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0';
    }

    function inViewport(el) {
        const r = el.getBoundingClientRect();
        return r.top < vh * 2 && r.bottom > -vh && r.left < vw && r.right > 0;
    }

    function textOf(el) {
        return (el.innerText || el.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 80);
    }

    // Links
    for (const a of document.querySelectorAll('a[href]')) {
        if (!isVisible(a) || !inViewport(a)) continue;
        const text = textOf(a);
        if (!text || text.length < 2) continue;
        const key = text.slice(0, 40);
        if (seen.has(key)) continue;
        seen.add(key);
        items.push({tag: 'a', text: text, href: (a.href || '').slice(0, 200)});
        if (items.length >= MAX_ITEMS) break;
    }

    // Buttons
    if (items.length < MAX_ITEMS) {
        for (const btn of document.querySelectorAll('button, [role="button"], input[type="submit"]')) {
            if (!isVisible(btn) || !inViewport(btn)) continue;
            const text = textOf(btn) || btn.value || btn.getAttribute('aria-label') || '';
            if (!text || text.length < 1) continue;
            const key = 'btn:' + text.slice(0, 40);
            if (seen.has(key)) continue;
            seen.add(key);
            items.push({tag: 'button', text: text.slice(0, 80)});
            if (items.length >= MAX_ITEMS) break;
        }
    }

    // Inputs
    if (items.length < MAX_ITEMS) {
        for (const inp of document.querySelectorAll('input:not([type="hidden"]), textarea, select')) {
            if (!isVisible(inp) || !inViewport(inp)) continue;
            const ph = inp.placeholder || inp.getAttribute('aria-label') || inp.name || '';
            const key = 'inp:' + (ph || inp.type || 'input');
            if (seen.has(key)) continue;
            seen.add(key);
            items.push({tag: inp.tagName.toLowerCase(), type: inp.type || '', placeholder: ph.slice(0, 60)});
            if (items.length >= MAX_ITEMS) break;
        }
    }

    return items;
}
""".replace("MAX_ITEMS", str(_MAX_INTERACTIVE_ITEMS))
_GENERIC_IMAGE_QUERIES = {
    "图",
    "图片",
    "照片",
    "近照",
    "高清",
    "最新",
    "随便",
    "来一张",
    "1",
    "2",
    "3",
    "?",
    "？",
}
_IMAGE_INTENT_HINTS = (
    "近照",
    "高清",
    "写真",
    "活动",
    "机场",
    "红毯",
    "肖像",
    "人像",
    "photo",
    "portrait",
    "recent",
    "latest",
    "hd",
)

_BING_JS = """
() => {
    const imgs = document.querySelectorAll('a.iusc');
    const urls = [];
    for (const a of imgs) {
        try {
            const m = JSON.parse(a.getAttribute('m') || '{}');
            if (m.murl) urls.push(m.murl);
        } catch {}
    }
    if (!urls.length) {
        for (const img of document.querySelectorAll('img.mimg, img[src^="http"]')) {
            const s = img.src || '';
            if (s.startsWith('http') && !s.includes('bing.com/th?') && img.naturalWidth > 60)
                urls.push(s);
        }
    }
    return urls.slice(0, 8);
}
"""

_BAIDU_JS = """
() => {
    const urls = [];
    for (const img of document.querySelectorAll('img.main_img, img[data-imgurl]')) {
        const u = img.getAttribute('data-imgurl') || img.src || '';
        if (u.startsWith('http') && !u.includes('baidu.com/img/'))
            urls.push(u);
    }
    if (!urls.length) {
        for (const a of document.querySelectorAll('a[href*="objurl"]')) {
            const m = new URLSearchParams(a.href.split('?')[1] || '');
            const ou = m.get('objurl');
            if (ou) urls.push(decodeURIComponent(ou));
        }
    }
    return urls.slice(0, 8);
}
"""

_GOOGLE_JS = """
() => {
    const imgs = document.querySelectorAll('img[src^="http"]');
    const urls = [];
    for (const img of imgs) {
        const src = img.src;
        if (src.includes('gstatic.com/images') || src.includes('google.com/logos'))
            continue;
        if (img.naturalWidth > 80 && img.naturalHeight > 80)
            urls.push(src);
    }
    return urls.slice(0, 5);
}
"""

_IMAGE_ENGINES: list[tuple[str, Callable[[str], str], str]] = [
    (
        "google",
        lambda q: f"https://www.google.com/search?q={q}&tbm=isch&udm=2",
        _GOOGLE_JS,
    ),
    (
        "bing",
        lambda q: f"https://www.bing.com/images/search?q={q}&form=HDRSC2",
        _BING_JS,
    ),
    (
        "baidu",
        lambda q: f"https://image.baidu.com/search/index?tn=baiduimage&word={q}",
        _BAIDU_JS,
    ),
]


class BrowserTool(Tool):
    """Browser control tool -- uses local Chrome via Playwright.

    Supports two modes:
    - **CDP mode**: connect to an existing Chrome via ``--remote-debugging-port``
      (preserves user login sessions). Configure ``plugins.browser.cdp_url``.
    - **Launch mode** (default): start a fresh Chrome instance.

    Supports navigation, screenshots, DOM interaction, JS evaluation,
    and an image-search shortcut that downloads the first result.
    """

    def __init__(self) -> None:
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._playwright: Any = None
        self._cdp_mode: bool = False

    @staticmethod
    def _is_page_usable(page: Any) -> bool:
        if page is None:
            return False
        is_closed = getattr(page, "is_closed", None)
        if callable(is_closed):
            try:
                if bool(is_closed()):
                    return False
            except Exception:
                return False
        context = getattr(page, "context", None)
        if context is None:
            return True
        browser = getattr(context, "browser", None)
        if browser is None:
            return True
        is_connected = getattr(browser, "is_connected", None)
        if callable(is_connected):
            try:
                return bool(is_connected())
            except Exception:
                return False
        return True

    async def _dispose_browser(self) -> None:
        """Release browser resources.

        In CDP mode we only disconnect (don't close the user's browser).
        In launch mode we close everything.
        """
        cdp = self._cdp_mode
        context = self._context
        browser = self._browser
        playwright = self._playwright

        self._context = None
        self._browser = None
        self._page = None
        self._playwright = None
        self._cdp_mode = False

        if cdp:
            # CDP mode: just disconnect, don't close user's browser/contexts
            if browser is not None:
                with suppress(Exception):
                    await browser.close()  # disconnect only
            if playwright is not None:
                with suppress(Exception):
                    await playwright.stop()
        else:
            if context is not None:
                with suppress(Exception):
                    await context.close()
            if browser is not None:
                with suppress(Exception):
                    await browser.close()
            if playwright is not None:
                with suppress(Exception):
                    await playwright.stop()

    # ── Config readers ────────────────────────────────────

    @staticmethod
    def _read_browser_config() -> dict[str, object]:
        """Read ``plugins.browser`` dict from user config file."""
        try:
            if not CONFIG_FILE.is_file():
                return {}
            raw_obj: object = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if not isinstance(raw_obj, dict):
                return {}
            raw = cast(dict[str, object], raw_obj)
            plugins_obj = raw.get("plugins")
            if not isinstance(plugins_obj, dict):
                return {}
            plugins = cast(dict[str, object], plugins_obj)
            browser_cfg_obj = plugins.get("browser")
            if not isinstance(browser_cfg_obj, dict):
                return {}
            return cast(dict[str, object], browser_cfg_obj)
        except Exception:
            return {}

    @staticmethod
    def _read_cdp_url() -> str:
        """Read ``plugins.browser.cdp_url`` from user config.

        Returns:
            CDP endpoint URL (e.g. ``http://localhost:9222``), or empty string.
        """
        cfg = BrowserTool._read_browser_config()
        cdp_url = cfg.get("cdp_url", "")
        return str(cdp_url).strip() if cdp_url else ""

    @staticmethod
    def _is_headless_enabled() -> bool:
        """Read browser visibility setting from user config.

        plugins.browser.visible=true  -> headless=False (show browser window)
        plugins.browser.visible=false -> headless=True  (no browser window)
        """
        cfg = BrowserTool._read_browser_config()
        visible_obj = cfg.get("visible")
        if visible_obj is None:
            return False
        return not bool(visible_obj)

    # ── Browser lifecycle ─────────────────────────────────

    async def _connect_cdp(self, cdp_url: str) -> Any:
        """Connect to an existing Chrome instance via CDP.

        Args:
            cdp_url: CDP endpoint, e.g. ``http://localhost:9222``.

        Returns:
            The first page of the default context.
        """
        from whaleclaw.tools.deps import ensure_tool_dep

        if not ensure_tool_dep("playwright"):
            raise RuntimeError(
                "playwright is not installed: "
                "pip install playwright && playwright install chromium"
            )

        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.connect_over_cdp(cdp_url)
        self._cdp_mode = True

        contexts = self._browser.contexts
        if contexts:
            self._context = contexts[0]
            pages = self._context.pages
            if pages:
                self._page = pages[0]
            else:
                self._page = await self._context.new_page()
        else:
            self._context = await self._browser.new_context(viewport=_VIEWPORT)
            self._page = await self._context.new_page()

        _SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        _DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

        # Suppress Runtime.Enable leak that anti-bot systems (Cloudflare/DataDome)
        # use to detect CDP-controlled browsers.
        try:
            cdp_session = await self._page.context.new_cdp_session(self._page)
            await cdp_session.send("Runtime.disable")
            await cdp_session.detach()
        except Exception as exc:
            log.debug("browser.runtime_disable_failed", error=str(exc))

        log.info("browser.cdp_connected", endpoint=cdp_url)
        return self._page

    async def _launch_browser(self) -> Any:
        """Launch a fresh Chrome instance (fallback when CDP is not configured)."""
        from whaleclaw.tools.deps import ensure_tool_dep

        if not ensure_tool_dep("playwright"):
            raise RuntimeError(
                "playwright is not installed: "
                "pip install playwright && playwright install chromium"
            )

        from playwright.async_api import async_playwright

        headless = self._is_headless_enabled()
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            channel="chrome",
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        self._cdp_mode = False
        self._context = await self._browser.new_context(
            viewport=_VIEWPORT,
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        self._page = await self._context.new_page()
        _SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        _DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        log.info("browser.launched", channel="chrome", headless=headless)
        return self._page

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="browser",
            description=(
                "Control a real Chrome browser. Actions: "
                "navigate(url) -- open URL (returns page interactive elements); "
                "screenshot -- capture current page; "
                "click(selector) -- click element (supports CSS selectors and "
                "Playwright text selectors like 'text=Click me'); "
                "type(selector, text) -- type into input; "
                "get_text(selector?) -- extract page/element text; "
                "evaluate(script) -- run JavaScript; "
                "search_images(query) -- image search and download one image per query; "
                "upload(selector, file_paths) -- upload files to a file input element; "
                "back -- go back; "
                "close -- close browser. "
                "IMPORTANT: navigate returns clickable elements on the page. "
                "Use the listed text content to build selectors. "
                "On search engine pages, links use redirect URLs so always "
                "prefer text selectors (e.g. 'text=文章标题') over href selectors."
            ),
            parameters=[
                ToolParameter(
                    name="action",
                    type="string",
                    description="Action to perform.",
                    required=True,
                    enum=_ACTIONS,
                ),
                ToolParameter(
                    name="url",
                    type="string",
                    description="URL for navigate action.",
                    required=False,
                ),
                ToolParameter(
                    name="selector",
                    type="string",
                    description=(
                        "CSS selector or Playwright text selector for click/type/get_text/upload. "
                        "Examples: 'text=登录' (by visible text), '#submit-btn' (by id), "
                        "'.search-result a' (by class). Prefer text selectors on search result pages."
                    ),
                    required=False,
                ),
                ToolParameter(
                    name="text",
                    type="string",
                    description=(
                        "Text to type, or search query for search_images. "
                        "For image search use explicit keywords, e.g. "
                        "'cute cat photo hd'."
                    ),
                    required=False,
                ),
                ToolParameter(
                    name="script",
                    type="string",
                    description="JavaScript code for evaluate action.",
                    required=False,
                ),
                ToolParameter(
                    name="file_paths",
                    type="string",
                    description=(
                        "For upload action: comma-separated absolute file paths "
                        "to upload, e.g. 'C:/Users/me/pic1.jpg,C:/Users/me/pic2.png'."
                    ),
                    required=False,
                ),
            ],
        )

    async def _ensure_browser(self, *, force_reset: bool = False) -> Any:
        """Ensure browser is running. Prefers CDP if configured."""
        if force_reset:
            await self._dispose_browser()
        elif self._is_page_usable(self._page):
            return self._page
        elif (
            self._page is not None
            or self._browser is not None
            or self._context is not None
            or self._playwright is not None
        ):
            await self._dispose_browser()

        # Try CDP first
        cdp_url = self._read_cdp_url()
        if cdp_url:
            try:
                return await self._connect_cdp(cdp_url)
            except Exception as exc:
                log.warning(
                    "browser.cdp_connect_failed",
                    endpoint=cdp_url,
                    error=str(exc),
                )
                # Clean up partial state before falling back
                await self._dispose_browser()

        return await self._launch_browser()

    async def _close(self) -> ToolResult:
        was_cdp = self._cdp_mode
        await self._dispose_browser()
        if was_cdp:
            return ToolResult(
                success=True,
                output="browser disconnected (CDP mode, Chrome keeps running)",
            )
        return ToolResult(success=True, output="browser closed")

    async def execute(self, **kwargs: Any) -> ToolResult:
        action: str = kwargs.get("action", "")
        if not action:
            return ToolResult(success=False, output="", error="action is empty")

        if action == "close":
            return await self._close()

        try:
            page = await self._ensure_browser()
        except Exception as exc:
            return ToolResult(success=False, output="", error=f"browser launch failed: {exc}")

        try:
            return await self._dispatch(page, action, kwargs)
        except Exception as exc:
            log.error("browser.error", action=action, error=str(exc))
            return ToolResult(success=False, output="", error=str(exc))

    async def _extract_interactive_summary(self, page: Any) -> str:
        """Extract visible interactive elements from the page for LLM context."""
        try:
            items: list[dict[str, str]] = await page.evaluate(_EXTRACT_INTERACTIVE_JS)
        except Exception:
            return ""
        if not items:
            return ""
        lines: list[str] = []
        for item in items:
            tag = item.get("tag", "")
            text = item.get("text", "")
            if tag == "a":
                href = item.get("href", "")
                lines.append(f"  [link] \"{text}\" -> {href}")
            elif tag == "button":
                lines.append(f"  [button] \"{text}\"")
            else:
                inp_type = item.get("type", "")
                placeholder = item.get("placeholder", "")
                label = placeholder or inp_type or tag
                lines.append(f"  [input:{inp_type}] placeholder=\"{label}\"")
        return "\n".join(lines)

    async def _dispatch(self, page: Any, action: str, kwargs: dict[str, Any]) -> ToolResult:
        if action == "navigate":
            url = kwargs.get("url", "")
            if not url:
                return ToolResult(success=False, output="", error="url is empty")
            await page.goto(url, timeout=_TIMEOUT, wait_until="domcontentloaded")
            await page.wait_for_timeout(800)
            title = await page.title()
            summary = await self._extract_interactive_summary(page)
            output = f"Navigated to: {url}\nTitle: {title}"
            if summary:
                output += (
                    f"\n\nInteractive elements on page:\n{summary}"
                    "\n\nTip: use text content (e.g. selector='text=链接文字') "
                    "to click links, especially on search engine result pages "
                    "where href is a redirect URL."
                )
            return ToolResult(success=True, output=output)

        elif action == "screenshot":
            return await self._screenshot(page)

        elif action == "click":
            selector = kwargs.get("selector", "")
            if not selector:
                return ToolResult(success=False, output="", error="selector is empty")
            try:
                await page.click(selector, timeout=_TIMEOUT)
                return ToolResult(success=True, output=f"Clicked: {selector}")
            except Exception as click_exc:
                summary = await self._extract_interactive_summary(page)
                ss = await self._screenshot(page)
                error_msg = str(click_exc)
                hint = f"Click failed: {error_msg}"
                if summary:
                    hint += (
                        f"\n\nAvailable clickable elements:\n{summary}"
                        "\n\nTip: use text content (e.g. selector='text=链接文字') "
                        "instead of href-based selectors on search result pages."
                    )
                if ss.success:
                    hint += f"\n\nPage screenshot: {ss.output}"
                log.error("browser.click_failed", selector=selector, error=error_msg)
                return ToolResult(success=False, output=hint, error=error_msg)

        elif action == "type":
            selector = kwargs.get("selector", "")
            text = kwargs.get("text", "")
            if not selector or not text:
                return ToolResult(
                    success=False, output="", error="selector and text are required"
                )
            await page.fill(selector, text, timeout=_TIMEOUT)
            return ToolResult(
                success=True, output=f"Typed '{text}' into {selector}"
            )

        elif action == "get_text":
            selector = kwargs.get("selector", "")
            if selector:
                el = await page.query_selector(selector)
                if el is None:
                    return ToolResult(
                        success=False, output="", error=f"Element not found: {selector}"
                    )
                text = await el.inner_text()
            else:
                text = await page.inner_text("body")
            truncated = text[:5000]
            if len(text) > 5000:
                truncated += f"\n...(truncated, total {len(text)} chars)"
            return ToolResult(success=True, output=truncated)

        elif action == "evaluate":
            script = kwargs.get("script", "")
            if not script:
                return ToolResult(
                    success=False, output="", error="script is empty"
                )
            result = await page.evaluate(script)
            return ToolResult(success=True, output=str(result)[:5000])

        elif action == "search_images":
            query = kwargs.get("text", "")
            if not query:
                return ToolResult(
                    success=False, output="", error="text is empty (search query)"
                )
            try:
                normalized_query = _normalize_image_query(str(query))
            except ValueError as exc:
                return ToolResult(
                    success=False,
                    output="",
                    error=str(exc),
                )
            return await self._search_images(page, normalized_query)

        elif action == "upload":
            return await self._upload_files(page, kwargs)

        elif action == "back":
            await page.go_back(timeout=_TIMEOUT)
            title = await page.title()
            return ToolResult(success=True, output=f"Went back, current page: {title}")

        return ToolResult(
            success=False, output="", error=f"Unknown action: {action}"
        )

    async def _upload_files(self, page: Any, kwargs: dict[str, Any]) -> ToolResult:
        """Upload files by clicking the upload trigger and intercepting the file chooser.

        Flow: listen for ``filechooser`` event → click the upload button/area →
        Playwright intercepts the OS file dialog and fills in the files
        programmatically.  From the website's perspective this looks identical
        to a real user clicking and selecting files.

        Works cross-platform (Windows/macOS/Linux).
        """
        import asyncio as _aio
        from pathlib import Path as _Path

        selector = str(kwargs.get("selector", "")).strip()
        raw_paths = str(kwargs.get("file_paths", "")).strip()
        if not raw_paths:
            return ToolResult(success=False, output="", error="file_paths is required for upload action")

        paths: list[str] = []
        for p in raw_paths.split(","):
            p = p.strip().strip("'\"")
            if not p:
                continue
            resolved = _Path(p).expanduser().resolve()
            if not resolved.is_file():
                return ToolResult(
                    success=False, output="",
                    error=f"File not found: {resolved}",
                )
            paths.append(str(resolved))

        if not paths:
            return ToolResult(success=False, output="", error="No valid file paths provided")

        if not selector:
            selector = 'input[type="file"]'

        try:
            fc_future: _aio.Future[Any] = _aio.get_event_loop().create_future()

            def _on_filechooser(chooser: Any) -> None:
                if not fc_future.done():
                    fc_future.set_result(chooser)

            page.on("filechooser", _on_filechooser)
            try:
                el = await page.query_selector(selector)
                if el is None:
                    return ToolResult(
                        success=False, output="",
                        error=f"Upload element not found: {selector}",
                    )
                visible = await el.is_visible()
                if visible:
                    await el.click(timeout=5000)
                else:
                    await el.dispatch_event("click")

                file_chooser = await _aio.wait_for(fc_future, timeout=10.0)
                await file_chooser.set_files(paths)
            finally:
                page.remove_listener("filechooser", _on_filechooser)

        except _aio.TimeoutError:
            log.debug("browser.upload_filechooser_timeout", selector=selector)
            el = await page.query_selector(selector)
            if el is None:
                all_inputs = await page.query_selector_all('input[type="file"]')
                el = all_inputs[0] if all_inputs else None
            if el is None:
                return ToolResult(
                    success=False, output="",
                    error=f"File input not found and filechooser timed out: {selector}",
                )
            if len(paths) == 1:
                await el.set_input_files(paths[0])
            else:
                await el.set_input_files(paths)
        except Exception as exc:
            return ToolResult(success=False, output="", error=f"Upload failed: {exc}")

        names = [_Path(p).name for p in paths]
        return ToolResult(
            success=True,
            output=f"Uploaded {len(paths)} file(s): {', '.join(names)}",
        )

    async def _screenshot(self, page: Any) -> ToolResult:
        if not self._is_page_usable(page):
            page = await self._ensure_browser(force_reset=True)
        filename = f"screenshot_{uuid.uuid4().hex[:8]}.png"
        path = _SCREENSHOT_DIR / filename

        # Mock document.fonts so Playwright won't wait for font loading
        # (XHS editor fonts load forever, causing screenshot timeout)
        with suppress(Exception):
            await page.evaluate(
                "document.fonts = {ready: Promise.resolve(), check: () => true, "
                "addEventListener: () => {}, removeEventListener: () => {}}"
            )

        await page.screenshot(path=str(path), full_page=False, timeout=_TIMEOUT)

        return ToolResult(success=True, output=f"Screenshot saved: {path}")

    async def _search_images(self, page: Any, query: str) -> ToolResult:
        """Image search -> download first result. Tries Bing then Google."""
        img_urls: list[str] = []

        for engine, url_fn, extract_js in _IMAGE_ENGINES:
            try:
                if not self._is_page_usable(page):
                    page = await self._ensure_browser(force_reset=True)
                search_url = url_fn(query)
                log.info("browser.image_search", engine=engine, query=query)
                await page.goto(search_url, timeout=_TIMEOUT, wait_until="domcontentloaded")
                await page.wait_for_timeout(2500)
                img_urls = await page.evaluate(extract_js)
                if img_urls:
                    break
            except Exception as exc:
                log.warning("browser.image_search_failed", engine=engine, error=str(exc))
                continue

        if not img_urls:
            ss = await self._screenshot(page)
            return ToolResult(
                success=False,
                output=f"No image results found. Page screenshot: {ss.output}",
                error="Image search returned no valid results",
            )

        import httpx

        min_image_bytes = 50 * 1024  # prefer images >= 50 KB
        best_fallback: tuple[Path, int] | None = None

        for url in img_urls:
            try:
                async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                    resp = await client.get(url)
                    if resp.status_code != 200:
                        continue
                    ct = resp.headers.get("content-type", "")
                    if "image" not in ct:
                        continue

                    ext = "jpg"
                    if "png" in ct:
                        ext = "png"
                    elif "webp" in ct:
                        ext = "webp"
                    elif "gif" in ct:
                        ext = "gif"

                    safe_name = "".join(
                        c if c.isascii() and c.isalnum() or c in "-_" else ""
                        for c in query[:30]
                    ).strip("_") or "image"
                    filename = f"{safe_name}_{uuid.uuid4().hex[:8]}.{ext}"
                    path = _DOWNLOAD_DIR / filename
                    data = resp.content
                    path.write_bytes(data)

                    if len(data) >= min_image_bytes:
                        size_kb = len(data) / 1024
                        return ToolResult(
                            success=True,
                            output=(
                                f"Query: {query}\n"
                                f"Image downloaded (DO NOT modify or fabricate path):\n"
                                f"![image]({path})\n"
                                f"File: {path}\n"
                                f"Size: {size_kb:.0f}KB"
                            ),
                        )
                    if best_fallback is None or len(data) > best_fallback[1]:
                        best_fallback = (path, len(data))
                    else:
                        path.unlink(missing_ok=True)
            except Exception:
                continue

        if best_fallback is not None:
            fb_path, fb_size = best_fallback
            size_kb = fb_size / 1024
            return ToolResult(
                success=True,
                output=(
                    f"Query: {query}\n"
                    f"Image downloaded (smaller, DO NOT modify or fabricate path):\n"
                    f"![image]({fb_path})\n"
                    f"File: {fb_path}\n"
                    f"Size: {size_kb:.0f}KB"
                ),
            )

        return ToolResult(
            success=False,
            output="",
            error="All image URL downloads failed",
        )


def _normalize_image_query(query: str) -> str:
    """Normalize and validate image-search query quality."""
    # strip ASCII control chars that may appear in malformed tool arguments
    q = "".join(ch for ch in query if ord(ch) >= 32 and ord(ch) != 127)
    # strip literal escaped noise like "\\n0\\n0\\x10"
    q = re.sub(r"(?:\\[nrt]\d*|\\x[0-9a-fA-F]{2})+", " ", q)
    q = " ".join(q.strip().split())
    if not q:
        raise ValueError("搜索词无效：内容为空")
    if q.lower() in _GENERIC_IMAGE_QUERIES or q in _GENERIC_IMAGE_QUERIES:
        raise ValueError(f"搜索词过于泛化：{q}")
    if re.fullmatch(r"[\d\s\W_]+", q):
        raise ValueError(f"搜索词无效：{q}")
    if len(q) < 2:
        raise ValueError(f"搜索词无效：过短 ({q})")

    # Enforce one visual intent per search call.
    multi_intent_parts = re.split(r"[、，,;/|+]+", q)
    non_empty_parts = [p.strip() for p in multi_intent_parts if p.strip()]
    if len(non_empty_parts) >= 2:
        raise ValueError(
            "search_images supports one subject per call, split into multiple calls"
        )

    has_hint = any(h in q.lower() for h in _IMAGE_INTENT_HINTS)
    if not has_hint:
        has_cjk = any("\u4e00" <= ch <= "\u9fff" for ch in q)
        q = f"{q} 近照 高清 人像" if has_cjk else f"{q} photo hd portrait"
    return q
