from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Generic, Literal, TypeVar

from astrbot.core.star.star_handler import StarHandlerMetadata

TELEGRAM_INLINE_STOP_CALLBACK_DATA = "astrbot_inline_stop"

InlineTargetKind = Literal["llm", "plugin", "command"]


@dataclass(slots=True)
class TelegramInlineTarget:
    kind: InlineTargetKind
    query: str
    handlers: list[StarHandlerMetadata] | None = None
    plugin_module_path: str | None = None
    plugin_name: str | None = None
    use_command_flow: bool = False
    created_at: float = 0.0


@dataclass(slots=True)
class TelegramInlineStopTarget:
    unified_msg_origin: str
    owner_user_id: str
    created_at: float = 0.0


_T = TypeVar("_T", TelegramInlineTarget, TelegramInlineStopTarget)


class _TTLRegistry(Generic[_T]):
    """带 TTL 过期和容量上限的通用注册表基类。"""

    def __init__(self, ttl_seconds: float, max_items: int) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_items = max_items
        self._targets: dict[str, _T] = {}

    def prune(self) -> None:
        """移除所有已过期的条目。"""
        now = time.monotonic()
        expired = [
            key
            for key, target in self._targets.items()
            if now - target.created_at > self.ttl_seconds
        ]
        for key in expired:
            self._targets.pop(key, None)

    def clear(self) -> None:
        """清空所有条目。"""
        self._targets.clear()

    def _drop_oldest(self) -> None:
        """强制移除最旧的一条条目（容量满时使用）。"""
        oldest = min(self._targets, key=lambda k: self._targets[k].created_at)
        self._targets.pop(oldest, None)


class TelegramInlineResultRegistry(_TTLRegistry[TelegramInlineTarget]):
    """Short-lived mapping from Telegram inline result IDs to AstrBot targets."""

    def __init__(self, ttl_seconds: float = 300.0, max_items: int = 512) -> None:
        super().__init__(ttl_seconds, max_items)

    def register(
        self,
        *,
        inline_query_id: str,
        kind: InlineTargetKind,
        query: str,
        ordinal: int,
        handlers: list[StarHandlerMetadata] | None = None,
        plugin_module_path: str | None = None,
        plugin_name: str | None = None,
        use_command_flow: bool = False,
    ) -> str:
        self.prune()
        seed = f"{inline_query_id}:{kind}:{plugin_module_path or ''}:{ordinal}"
        digest = hashlib.blake2b(seed.encode("utf-8"), digest_size=8).hexdigest()
        result_id = f"ab-{digest}"
        self._targets[result_id] = TelegramInlineTarget(
            kind=kind,
            query=query,
            handlers=list(handlers or []),
            plugin_module_path=plugin_module_path,
            plugin_name=plugin_name,
            use_command_flow=use_command_flow,
            created_at=time.monotonic(),
        )
        if len(self._targets) > self.max_items:
            self._drop_oldest()
        return result_id

    def resolve(self, result_id: str) -> TelegramInlineTarget | None:
        target = self._targets.get(result_id)
        if target is None:
            return None
        if time.monotonic() - target.created_at > self.ttl_seconds:
            self._targets.pop(result_id, None)
            return None
        return target


telegram_inline_result_registry = TelegramInlineResultRegistry()


class TelegramInlineStopRegistry(_TTLRegistry[TelegramInlineStopTarget]):
    """Short-lived mapping from inline_message_id to the chosen-inline stop target."""

    def __init__(self, ttl_seconds: float = 600.0, max_items: int = 512) -> None:
        super().__init__(ttl_seconds, max_items)

    def register(
        self,
        inline_message_id: str,
        unified_msg_origin: str,
        owner_user_id: str,
    ) -> None:
        if not inline_message_id:
            return
        self.prune()
        self._targets[inline_message_id] = TelegramInlineStopTarget(
            unified_msg_origin=unified_msg_origin,
            owner_user_id=owner_user_id,
            created_at=time.monotonic(),
        )
        if len(self._targets) > self.max_items:
            self._drop_oldest()

    def resolve(self, inline_message_id: str) -> TelegramInlineStopTarget | None:
        target = self._targets.get(inline_message_id)
        if target is None:
            return None
        if time.monotonic() - target.created_at > self.ttl_seconds:
            self._targets.pop(inline_message_id, None)
            return None
        return target


telegram_inline_stop_registry = TelegramInlineStopRegistry()
