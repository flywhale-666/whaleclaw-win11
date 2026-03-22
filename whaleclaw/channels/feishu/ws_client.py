"""Feishu long-connection (WebSocket) client bridge.

Uses the official ``lark-oapi`` SDK to maintain a persistent WebSocket
connection with the Feishu server.  Events are dispatched to the
FeishuBot in the main asyncio event loop via ``run_coroutine_threadsafe``.

The SDK's ``lark.ws.Client.start()`` blocks its own event loop, so we
run it in a dedicated daemon thread.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import parse_qs, urlparse

import lark_oapi as lark
import websockets
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
from lark_oapi.core.log import logger as lark_logger
from lark_oapi.ws.const import DEVICE_ID, SERVICE_ID
from whaleclaw.utils.log import get_logger

log = get_logger(__name__)

_WS_OPEN_TIMEOUT = 30
_WS_PING_INTERVAL = 120


class FeishuWSBridge:
    """Bridge between ``lark.ws.Client`` and the async FeishuBot."""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        on_message: Callable[[dict[str, Any]], Awaitable[None]],
        main_loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._on_message = on_message
        self._main_loop = main_loop
        self._thread: threading.Thread | None = None
        self._ws_client: lark.ws.Client | None = None

    def _handle_message_receive(self, data: P2ImMessageReceiveV1) -> None:
        """Called by the SDK in its own thread when a message arrives."""
        try:
            raw = lark.JSON.marshal(data)
            import json
            body: dict[str, Any] = json.loads(raw)
            asyncio.run_coroutine_threadsafe(
                self._on_message(body), self._main_loop,
            )
        except Exception:
            log.exception("feishu.ws.handle_error")

    @staticmethod
    def _patch_sdk_connect(sdk_client: lark.ws.Client) -> None:
        """Monkey-patch the SDK's ``_connect`` to pass proxy-friendly timeouts.

        The stock ``lark-oapi`` calls ``websockets.connect(url)`` with the
        default ``open_timeout=10``.  When the system HTTP proxy is active
        (e.g. Clash global mode), the CONNECT tunnel adds latency and 10 s
        is often not enough, causing *timed out during opening handshake*.

        This patch replaces ``_connect`` so that ``websockets.connect`` is
        called with a larger ``open_timeout`` and explicit ``ping_interval``
        while keeping all other SDK behaviour intact.
        """
        import lark_oapi.ws.client as _ws_mod

        original_lock = sdk_client._lock  # noqa: SLF001

        async def _patched_connect() -> None:
            await original_lock.acquire()
            if sdk_client._conn is not None:  # noqa: SLF001
                return
            try:
                conn_url = sdk_client._get_conn_url()  # noqa: SLF001
                u = urlparse(conn_url)
                q = parse_qs(u.query)
                conn_id = q[DEVICE_ID][0]
                service_id = q[SERVICE_ID][0]

                conn = await websockets.connect(
                    conn_url,
                    open_timeout=_WS_OPEN_TIMEOUT,
                    ping_interval=_WS_PING_INTERVAL,
                    ping_timeout=_WS_PING_INTERVAL,
                )
                sdk_client._conn = conn  # noqa: SLF001
                sdk_client._conn_url = conn_url  # noqa: SLF001
                sdk_client._conn_id = conn_id  # noqa: SLF001
                sdk_client._service_id = service_id  # noqa: SLF001

                lark_logger.info(
                    sdk_client._fmt_log("connected to {}", conn_url),  # noqa: SLF001
                )
                _ws_mod.loop.create_task(
                    sdk_client._receive_message_loop(),  # noqa: SLF001
                )
            except websockets.InvalidStatusCode as e:
                from lark_oapi.ws.client import _parse_ws_conn_exception

                _parse_ws_conn_exception(e)
            finally:
                original_lock.release()

        sdk_client._connect = _patched_connect  # type: ignore[assignment]  # noqa: SLF001

    def _run(self) -> None:
        """Entry point for the daemon thread.

        The SDK caches a module-level ``loop`` obtained via
        ``asyncio.get_event_loop()`` at import time, which will be the
        main uvicorn loop.  We must replace it with a fresh loop that
        belongs to *this* thread so ``run_until_complete`` works.
        """
        import lark_oapi.ws.client as _ws_mod

        thread_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(thread_loop)
        _ws_mod.loop = thread_loop

        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._handle_message_receive)
            .build()
        )
        self._ws_client = lark.ws.Client(
            self._app_id,
            self._app_secret,
            event_handler=handler,
            log_level=lark.LogLevel.INFO,
        )
        self._patch_sdk_connect(self._ws_client)
        log.info("feishu.ws.connecting")
        try:
            self._ws_client.start()
        except Exception:
            log.exception("feishu.ws.connection_failed")
        finally:
            thread_loop.close()

    def start(self) -> None:
        """Start the long-connection in a background daemon thread."""
        self._thread = threading.Thread(
            target=self._run, name="feishu-ws", daemon=True,
        )
        self._thread.start()
        log.info("feishu.ws.started")

    def stop(self) -> None:
        """Best-effort cleanup (SDK thread is daemon, dies with process)."""
        log.info("feishu.ws.stopped")
