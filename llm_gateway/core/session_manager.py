"""SQLite-backed session lifecycle manager."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import Any

from llm_gateway.models.role_card import RoleCard
from llm_gateway.models.session import Session


class SessionManager:
    """Manage stateful chat sessions with lightweight SQLite persistence."""

    def __init__(
        self,
        db_path: str,
        default_ttl_hours: int = 24,
        cleanup_interval_s: int = 3600,
    ) -> None:
        self._cache: dict[str, Session] = {}
        self._db_path = db_path
        self._default_ttl_hours = default_ttl_hours
        self._cleanup_interval_s = cleanup_interval_s
        self._lock = asyncio.Lock()
        self._cleanup_task: asyncio.Task | None = None
        self._init_db()

    def start_cleanup_loop(self) -> None:
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def shutdown(self) -> None:
        if self._cleanup_task:
            self._cleanup_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._cleanup_task
            self._cleanup_task = None

    async def get(self, session_id: str) -> Session | None:
        async with self._lock:
            session = self._cache.get(session_id)
            if session and not _is_expired(session):
                return session
            if session and _is_expired(session):
                self._cache.pop(session_id, None)

            loaded = self._load_session(session_id)
            if loaded and not _is_expired(loaded):
                self._cache[session_id] = loaded
                return loaded
            if loaded and _is_expired(loaded):
                self._delete_session(session_id)
            return None

    async def get_or_create(
        self,
        session_id: str,
        role_card: str,
        caller: str,
        dimension_overrides: dict[str, str] | None = None,
        ttl_hours: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Session:
        async with self._lock:
            existing = self._cache.get(session_id) or self._load_session(session_id)
            if existing and _is_expired(existing):
                self._delete_session(session_id)
                existing = None

            if existing:
                if role_card and existing.role_card and role_card != existing.role_card:
                    raise ValueError(
                        f"session '{session_id}' already bound to role_card '{existing.role_card}'"
                    )
                if caller and existing.caller and caller != existing.caller:
                    raise ValueError(
                        f"session '{session_id}' already bound to caller '{existing.caller}'"
                    )
                self._cache[session_id] = existing
                return existing

            if not role_card:
                raise ValueError("role_card is required when creating a new session")

            session = Session(
                session_id=session_id,
                role_card=role_card,
                caller=caller,
                ttl_hours=ttl_hours or self._default_ttl_hours,
                dimension_overrides=dict(dimension_overrides or {}),
                metadata=dict(metadata or {}),
            )
            self._upsert_session(session)
            self._cache[session_id] = session
            return session

    async def build_full_messages(
        self,
        session: Session,
        new_messages: list[dict],
        role_card: RoleCard,
        dimension_overrides: dict[str, str] | None = None,
    ) -> list[dict]:
        merged_overrides = dict(session.dimension_overrides)
        merged_overrides.update(dimension_overrides or {})

        role_system_prompt = role_card.build_system_prompt(merged_overrides)
        request_system_messages = [dict(message) for message in new_messages if message.get("role") == "system"]
        request_non_system_messages = [
            dict(message) for message in new_messages
            if message.get("role") != "system"
        ]
        history = await self._truncate_history(
            list(session.messages),
            max_turns=role_card.max_history_turns,
            max_tokens=role_card.max_history_tokens,
        )

        full_messages: list[dict[str, Any]] = [{"role": "system", "content": role_system_prompt}]
        full_messages.extend(request_system_messages)
        full_messages.extend(history)
        full_messages.extend(request_non_system_messages)
        return full_messages

    async def append_messages(
        self,
        session_id: str,
        messages: list[dict],
        usage_tokens: int = 0,
    ) -> None:
        async with self._lock:
            session = self._cache.get(session_id) or self._load_session(session_id)
            if session is None or _is_expired(session):
                return

            session.messages.extend(
                dict(message)
                for message in messages
                if message.get("role") != "system"
            )
            session.total_tokens_used += usage_tokens
            session.updated_at = datetime.now(timezone.utc)
            self._cache[session_id] = session
            self._upsert_session(session)

    async def delete(self, session_id: str) -> bool:
        async with self._lock:
            existed = self._load_session(session_id) is not None or session_id in self._cache
            self._cache.pop(session_id, None)
            self._delete_session(session_id)
            return existed

    async def list_sessions(self, caller: str = "") -> list[Session]:
        async with self._lock:
            sessions = self._list_sessions()
            fresh: list[Session] = []
            for session in sessions:
                if _is_expired(session):
                    self._delete_session(session.session_id)
                    continue
                if caller and not session.caller.startswith(caller):
                    continue
                fresh.append(session)
                self._cache[session.session_id] = session
            fresh.sort(key=lambda item: item.updated_at, reverse=True)
            return fresh

    async def _truncate_history(
        self,
        messages: list[dict],
        max_turns: int,
        max_tokens: int,
    ) -> list[dict]:
        if not messages:
            return []

        kept_by_turns: list[dict] = []
        user_turns = 0
        for message in reversed(messages):
            role = str(message.get("role", ""))
            if role == "user":
                if user_turns >= max_turns and kept_by_turns:
                    break
                user_turns += 1
            kept_by_turns.append(message)
        kept_by_turns.reverse()

        if max_tokens <= 0:
            return kept_by_turns

        kept_by_tokens: list[dict] = []
        total_tokens = 0
        for message in reversed(kept_by_turns):
            estimated_tokens = _estimate_message_tokens(message)
            if kept_by_tokens and total_tokens + estimated_tokens > max_tokens:
                break
            kept_by_tokens.append(message)
            total_tokens += estimated_tokens
        kept_by_tokens.reverse()
        return kept_by_tokens

    async def _cleanup_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._cleanup_interval_s)
                await self._cleanup_expired()
            except asyncio.CancelledError:
                break

    async def _cleanup_expired(self) -> None:
        async with self._lock:
            for session in self._list_sessions():
                if _is_expired(session):
                    self._cache.pop(session.session_id, None)
                    self._delete_session(session.session_id)

    def _init_db(self) -> None:
        os.makedirs(os.path.dirname(self._db_path) or ".", exist_ok=True)
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    role_card TEXT NOT NULL,
                    caller TEXT NOT NULL,
                    messages_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    ttl_hours INTEGER NOT NULL,
                    dimension_overrides_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    total_tokens_used INTEGER NOT NULL
                )
                """
            )

    def _load_session(self, session_id: str) -> Session | None:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                """
                SELECT session_id, role_card, caller, messages_json, created_at, updated_at,
                       ttl_hours, dimension_overrides_json, metadata_json, total_tokens_used
                FROM sessions
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()

        if row is None:
            return None
        return _row_to_session(row)

    def _list_sessions(self) -> list[Session]:
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(
                """
                SELECT session_id, role_card, caller, messages_json, created_at, updated_at,
                       ttl_hours, dimension_overrides_json, metadata_json, total_tokens_used
                FROM sessions
                ORDER BY updated_at DESC
                """
            ).fetchall()
        return [_row_to_session(row) for row in rows]

    def _upsert_session(self, session: Session) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """
                INSERT INTO sessions (
                    session_id, role_card, caller, messages_json, created_at, updated_at,
                    ttl_hours, dimension_overrides_json, metadata_json, total_tokens_used
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    role_card = excluded.role_card,
                    caller = excluded.caller,
                    messages_json = excluded.messages_json,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    ttl_hours = excluded.ttl_hours,
                    dimension_overrides_json = excluded.dimension_overrides_json,
                    metadata_json = excluded.metadata_json,
                    total_tokens_used = excluded.total_tokens_used
                """,
                (
                    session.session_id,
                    session.role_card,
                    session.caller,
                    json.dumps(session.messages, ensure_ascii=False),
                    session.created_at.isoformat(),
                    session.updated_at.isoformat(),
                    session.ttl_hours,
                    json.dumps(session.dimension_overrides, ensure_ascii=False),
                    json.dumps(session.metadata, ensure_ascii=False),
                    session.total_tokens_used,
                ),
            )

    def _delete_session(self, session_id: str) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))


def _row_to_session(row: tuple[Any, ...]) -> Session:
    return Session(
        session_id=row[0],
        role_card=row[1],
        caller=row[2],
        messages=json.loads(row[3] or "[]"),
        created_at=datetime.fromisoformat(row[4]),
        updated_at=datetime.fromisoformat(row[5]),
        ttl_hours=int(row[6]),
        dimension_overrides=json.loads(row[7] or "{}"),
        metadata=json.loads(row[8] or "{}"),
        total_tokens_used=int(row[9] or 0),
    )


def _estimate_message_tokens(message: dict[str, Any]) -> int:
    serialized = json.dumps(message, ensure_ascii=False)
    return max(1, len(serialized) // 4 + 8)


def _is_expired(session: Session) -> bool:
    expires_at = session.updated_at + timedelta(hours=session.ttl_hours)
    return datetime.now(timezone.utc) >= expires_at
