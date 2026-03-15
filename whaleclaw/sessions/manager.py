"""Session lifecycle management."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from whaleclaw.config.schema import WhaleclawConfig
from whaleclaw.providers.base import Message
from whaleclaw.sessions.store import SessionStore
from whaleclaw.utils.log import get_logger

log = get_logger(__name__)


class Session(BaseModel):
    """In-memory session representation."""

    id: str
    channel: str
    peer_id: str
    messages: list[Message] = Field(default_factory=list)
    model: str
    thinking_level: str = "off"
    created_at: datetime
    updated_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)
    message_count: int = 0


class SessionManager:
    """Manages session lifecycle backed by SQLite."""

    def __init__(self, store: SessionStore, config: WhaleclawConfig) -> None:
        self._store = store
        self._config = config

    @property
    def store(self) -> SessionStore:
        return self._store

    async def create(self, channel: str, peer_id: str) -> Session:
        """Create a new session."""
        now = datetime.now(UTC)
        session = Session(
            id=uuid4().hex,
            channel=channel,
            peer_id=peer_id,
            model=self._config.agent.model,
            thinking_level=self._config.agent.thinking_level,
            created_at=now,
            updated_at=now,
        )
        await self._store.save_session(
            session_id=session.id,
            channel=session.channel,
            peer_id=session.peer_id,
            model=session.model,
            thinking_level=session.thinking_level,
            created_at=now.isoformat(),
            updated_at=now.isoformat(),
        )
        log.info("session.created", session_id=session.id, channel=channel)
        return session

    async def get(self, session_id: str) -> Session | None:
        """Load a session with its message history."""
        row = await self._store.get_session(session_id)
        if not row:
            return None
        msg_rows = await self._store.get_messages(session_id)
        messages: list[Message] = []
        for m in msg_rows:
            if m.role == "tool":
                messages.append(Message(
                    role="tool",  # type: ignore[arg-type]
                    content=m.content,
                    tool_call_id=m.tool_call_id,
                ))
            elif m.role == "assistant":
                # assistant messages may carry tool_calls; stored as metadata JSON.
                tool_calls_data: list[Any] = m.metadata.get("tool_calls", [])  # type: ignore[assignment]
                tool_calls = None
                if tool_calls_data:
                    from whaleclaw.providers.base import ToolCall
                    tool_calls = [
                        ToolCall(
                            id=tc.get("id", ""),
                            name=tc.get("name", ""),
                            arguments=tc.get("arguments", {}),
                        )
                        for tc in tool_calls_data
                        if isinstance(tc, dict)
                    ] or None
                messages.append(Message(
                    role="assistant",  # type: ignore[arg-type]
                    content=m.content,
                    tool_calls=tool_calls,
                ))
            else:
                messages.append(Message(
                    role=m.role,  # type: ignore[arg-type]
                    content=m.content,
                ))
        return Session(
            id=row.id,
            channel=row.channel,
            peer_id=row.peer_id,
            messages=messages,
            model=row.model,
            thinking_level=row.thinking_level,
            created_at=datetime.fromisoformat(row.created_at),
            updated_at=datetime.fromisoformat(row.updated_at),
            metadata=row.metadata,  # type: ignore[arg-type]
        )

    async def get_or_create(self, channel: str, peer_id: str) -> Session:
        """Find an existing session for the peer or create a new one."""
        row = await self._store.get_session_by_peer(channel, peer_id)
        if row:
            session = await self.get(row.id)
            if session:
                return session
        return await self.create(channel, peer_id)

    async def add_message(
        self,
        session: Session,
        role: str,
        content: str,
        *,
        tool_call_id: str | None = None,
        tool_name: str | None = None,
        tool_calls: list[Any] | None = None,
    ) -> None:
        """Append a message to the session and persist it.

        Args:
            tool_calls: Serialisable list of tool call dicts for assistant messages
                        (each dict: {id, name, arguments}).  Stored in message
                        metadata so they can be reconstructed on session reload.
        """
        from whaleclaw.providers.base import ToolCall  # local to avoid circular

        if role == "tool":
            # Keep the real 'tool' role in memory so repair logic works correctly.
            mem_msg = Message(
                role="tool",  # type: ignore[arg-type]
                content=content,
                tool_call_id=tool_call_id,
            )
        elif role == "assistant" and tool_calls:
            tc_objects = [
                ToolCall(
                    id=tc.get("id", "") if isinstance(tc, dict) else tc.id,
                    name=tc.get("name", "") if isinstance(tc, dict) else tc.name,
                    arguments=tc.get("arguments", {}) if isinstance(tc, dict) else tc.arguments,
                )
                for tc in tool_calls
            ] or None
            mem_msg = Message(
                role="assistant",  # type: ignore[arg-type]
                content=content,
                tool_calls=tc_objects,
            )
        else:
            mem_msg = Message(role=role, content=content)  # type: ignore[arg-type]
        session.messages.append(mem_msg)
        session.updated_at = datetime.now(UTC)

        # Build metadata to persist tool_calls for assistant messages.
        metadata: dict[str, object] | None = None
        if role == "assistant" and tool_calls:
            serialised = [
                {
                    "id": tc.get("id", "") if isinstance(tc, dict) else tc.id,
                    "name": tc.get("name", "") if isinstance(tc, dict) else tc.name,
                    "arguments": tc.get("arguments", {}) if isinstance(tc, dict) else tc.arguments,
                }
                for tc in tool_calls
            ]
            metadata = {"tool_calls": serialised}

        await self._store.add_message(
            session_id=session.id,
            role=role,
            content=content,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            metadata=metadata,
        )
        await self._store.update_session_field(
            session.id, updated_at=session.updated_at.isoformat()
        )

    async def update_model(self, session: Session, model: str) -> None:
        """Switch the model for a session."""
        session.model = model
        await self._store.update_session_field(session.id, model=model)

    async def update_thinking(self, session: Session, level: str) -> None:
        session.thinking_level = level
        await self._store.update_session_field(session.id, thinking_level=level)

    async def update_metadata(self, session: Session, metadata: dict[str, Any]) -> None:
        """Update session metadata and persist it."""
        session.metadata = metadata
        await self._store.update_session_field(session.id, metadata=metadata)

    async def reset(self, session_id: str) -> Session | None:
        """Clear messages but keep the session."""
        session = await self.get(session_id)
        if not session:
            return None
        await self._store.delete_messages(session_id)
        session.messages.clear()
        session.updated_at = datetime.now(UTC)
        await self._store.update_session_field(
            session_id, updated_at=session.updated_at.isoformat()
        )
        log.info("session.reset", session_id=session_id)
        return session

    async def list_sessions(self) -> list[Session]:
        rows = await self._store.list_sessions()
        counts = await self._store.get_message_counts()
        sessions: list[Session] = []
        for row in rows:
            s = Session(
                id=row.id,
                channel=row.channel,
                peer_id=row.peer_id,
                model=row.model,
                thinking_level=row.thinking_level,
                created_at=datetime.fromisoformat(row.created_at),
                updated_at=datetime.fromisoformat(row.updated_at),
                metadata=row.metadata,  # type: ignore[arg-type]
            )
            s.message_count = counts.get(row.id, 0)
            sessions.append(s)
        return sessions

    async def delete(self, session_id: str) -> None:
        await self._store.delete_session(session_id)
        log.info("session.deleted", session_id=session_id)
