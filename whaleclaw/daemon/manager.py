"""Daemon manager — platform detection and service delegation."""

from __future__ import annotations

import platform
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel

if TYPE_CHECKING:
    pass


class _ServiceProto(Protocol):
    def install(self, *, python_path: str, port: int, bind: str) -> None: ...
    def uninstall(self) -> None: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def is_installed(self) -> bool: ...


class _UnsupportedService:
    """Placeholder for platforms without daemon support (e.g. Windows)."""

    def _err(self) -> RuntimeError:
        return RuntimeError("Windows 暂不支持系统服务安装，请使用 bat 脚本或任务计划程序启动")

    def install(self, *, python_path: str, port: int = 18666, bind: str = "127.0.0.1") -> None:
        raise self._err()

    def uninstall(self) -> None:
        raise self._err()

    def start(self) -> None:
        raise self._err()

    def stop(self) -> None:
        raise self._err()

    def is_installed(self) -> bool:
        return False


class ServiceStatus(BaseModel):
    """Daemon service status model."""

    installed: bool
    running: bool = False
    platform: str


class DaemonManager:
    """Daemon manager — delegates to launchd (macOS), systemd (Linux), or stub (Windows)."""

    def __init__(self) -> None:
        self._platform = platform.system().lower()
        service: _ServiceProto
        if self._platform == "darwin":
            from whaleclaw.daemon.launchd import LaunchdService
            service = LaunchdService()
        elif self._platform == "linux":
            from whaleclaw.daemon.systemd import SystemdService
            service = SystemdService()
        else:
            service = _UnsupportedService()
        self._service: _ServiceProto = service

    def install(
        self,
        python_path: str,
        port: int = 18666,
        bind: str = "127.0.0.1",
    ) -> None:
        self._service.install(python_path=python_path, port=port, bind=bind)

    def uninstall(self) -> None:
        self._service.uninstall()

    def start(self) -> None:
        self._service.start()

    def stop(self) -> None:
        self._service.stop()

    def status(self) -> ServiceStatus:
        installed = self._service.is_installed()
        running = False
        return ServiceStatus(
            installed=installed,
            running=running,
            platform=self._platform,
        )
