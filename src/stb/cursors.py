from __future__ import annotations

import secrets
import json
import time
from dataclasses import dataclass
from typing import Any

from stb.errors import StbError


@dataclass(frozen=True)
class CursorEntry:
    state: dict[str, Any]
    created_at: float
    expires_at: float


class CursorRegistry:
    """Generation-local opaque cursor storage with replayable immutable state."""

    def __init__(self, ttl_sec: float = 600.0, maximum: int = 128) -> None:
        self.ttl_sec = ttl_sec
        self.maximum = maximum
        self._entries: dict[str, CursorEntry] = {}
        self._tokens_by_state: dict[str, str] = {}

    def _state_key(self, state: dict[str, Any]) -> str:
        return json.dumps(state, sort_keys=True, separators=(",", ":"))

    def _purge_expired(self, now: float) -> None:
        expired = [
            token for token, entry in self._entries.items() if entry.expires_at <= now
        ]
        for token in expired:
            state_key = self._state_key(self._entries[token].state)
            self._tokens_by_state.pop(state_key, None)
            del self._entries[token]

    def issue(self, state: dict[str, Any]) -> str:
        now = time.monotonic()
        self._purge_expired(now)
        state_key = self._state_key(state)
        existing = self._tokens_by_state.get(state_key)
        if existing is not None:
            entry = self._entries[existing]
            self._entries[existing] = CursorEntry(
                state=entry.state,
                created_at=entry.created_at,
                expires_at=now + self.ttl_sec,
            )
            return existing
        if len(self._entries) >= self.maximum:
            raise StbError(
                "limit_exceeded",
                "active cursor limit reached",
                {"limit": self.maximum},
            )
        token = f"cur-{secrets.token_urlsafe(18)}"
        self._entries[token] = CursorEntry(
            state=state,
            created_at=now,
            expires_at=now + self.ttl_sec,
        )
        self._tokens_by_state[state_key] = token
        return token

    def get(self, token: str | None) -> dict[str, Any] | None:
        if not token:
            return None
        now = time.monotonic()
        self._purge_expired(now)
        entry = self._entries.get(token)
        if entry is None:
            raise StbError("cursor_expired", "invalid or expired cursor")
        # Sliding expiry changes registry metadata, not the immutable query state.
        self._entries[token] = CursorEntry(
            state=entry.state,
            created_at=entry.created_at,
            expires_at=now + self.ttl_sec,
        )
        return entry.state

    def clear(self) -> None:
        self._entries.clear()
        self._tokens_by_state.clear()

    @property
    def active_count(self) -> int:
        self._purge_expired(time.monotonic())
        return len(self._entries)
