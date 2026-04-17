"""事件总线, 用于处理事件的分发和处理
事件总线是一个异步队列, 用于接收各种消息事件, 并将其发送到Scheduler调度器进行处理
其中包含了一个无限循环的调度函数, 用于从事件队列中获取新的事件, 并创建一个新的异步任务来执行管道调度器的处理逻辑

class:
    EventBus: 事件总线, 用于处理事件的分发和处理

工作流程:
1. 维护一个异步队列, 来接受各种消息事件
2. 无限循环的调度函数, 从事件队列中获取新的事件, 打印日志并创建一个新的异步任务来执行管道调度器的处理逻辑
"""

import asyncio
from asyncio import Queue

from astrbot.core import logger
from astrbot.core.astrbot_config_mgr import AstrBotConfigManager
from astrbot.core.pipeline.scheduler import PipelineScheduler
from astrbot.core.platform.sources.telegram.inline_registry import (
    telegram_inline_result_registry,
)
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.command_group import CommandGroupFilter
from astrbot.core.star.filter.permission import PermissionTypeFilter
from astrbot.core.star.session_plugin_manager import SessionPluginManager
from astrbot.core.star.star import star_map
from astrbot.core.star.star_handler import (
    EventType,
    StarHandlerMetadata,
    star_handlers_registry,
)

from .platform import AstrMessageEvent


class EventBus:
    """用于处理事件的分发和处理"""

    def __init__(
        self,
        event_queue: Queue,
        pipeline_scheduler_mapping: dict[str, PipelineScheduler],
        astrbot_config_mgr: AstrBotConfigManager,
    ) -> None:
        self.event_queue = event_queue  # 事件队列
        # abconf uuid -> scheduler
        self.pipeline_scheduler_mapping = pipeline_scheduler_mapping
        self.astrbot_config_mgr = astrbot_config_mgr

    async def dispatch(self) -> None:
        while True:
            event: AstrMessageEvent = await self.event_queue.get()
            conf_info = self.astrbot_config_mgr.get_conf_info(event.unified_msg_origin)
            conf_id = conf_info["id"]
            conf_name = conf_info.get("name") or conf_id
            self._print_event(event, conf_name)
            if await self._maybe_handle_inline_query(event):
                continue
            scheduler = self.pipeline_scheduler_mapping.get(conf_id)
            if not scheduler:
                logger.error(
                    f"PipelineScheduler not found for id: {conf_id}, event ignored."
                )
                continue
            asyncio.create_task(scheduler.execute(event))

    async def _maybe_handle_inline_query(self, event: AstrMessageEvent) -> bool:
        if not hasattr(event, "inline_query_id"):
            return False
        if hasattr(event, "inline_message_id"):
            return False

        query = str(getattr(event, "query", "") or "").strip()
        conf = self.astrbot_config_mgr.get_conf(event.unified_msg_origin)
        enabled_plugins_name = conf.get("plugin_set", ["*"])
        if enabled_plugins_name == ["*"]:
            event.plugins_name = None
        else:
            event.plugins_name = enabled_plugins_name

        command_query = self._normalize_inline_command_query(query, conf)
        if command_query is not None:
            await self._answer_inline_command_options(
                event,
                command_query,
                conf,
            )
            return True

        handlers = star_handlers_registry.get_handlers_by_event_type(
            EventType.InlineQueryEvent,
            plugins_name=event.plugins_name,
        )
        handlers = await SessionPluginManager.filter_handlers_by_session(
            event,
            handlers,
        )

        handlers_by_module = {}
        for handler in handlers:
            handlers_by_module.setdefault(handler.handler_module_path, []).append(
                handler
            )

        inline_query_id = str(getattr(event, "inline_query_id", ""))
        llm_result_id = telegram_inline_result_registry.register(
            inline_query_id=inline_query_id,
            kind="llm",
            query=query,
            ordinal=0,
        )
        options = [
            {
                "id": llm_result_id,
                "title": "LLM",
                "description": "Ask AstrBot's configured language model.",
                "kind": "llm",
            }
        ]

        for ordinal, (module_path, module_handlers) in enumerate(
            handlers_by_module.items(), start=1
        ):
            md = star_map.get(module_path)
            if not md:
                continue
            plugin_name = md.display_name or md.name or module_path.rsplit(".", 1)[-1]
            description = md.desc or "Run this plugin for the inline query."
            result_id = telegram_inline_result_registry.register(
                inline_query_id=inline_query_id,
                kind="plugin",
                query=query,
                ordinal=ordinal,
                handlers=module_handlers,
                plugin_module_path=module_path,
                plugin_name=plugin_name,
            )
            options.append(
                {
                    "id": result_id,
                    "title": str(plugin_name),
                    "description": str(description),
                    "kind": "plugin",
                }
            )

        if hasattr(event, "answer_inline_options"):
            await event.answer_inline_options(options)
        else:
            logger.warning("Inline query event does not support targeted options.")

        return True

    def _normalize_inline_command_query(
        self,
        query: str,
        conf: dict,
    ) -> str | None:
        message = query.strip()
        if not message:
            return None
        for wake_prefix in conf.get("wake_prefix", []):
            wake_prefix = str(wake_prefix)
            if wake_prefix and message.startswith(wake_prefix):
                return message[len(wake_prefix) :].strip()
        return None

    async def _answer_inline_command_options(
        self,
        event: AstrMessageEvent,
        command_query: str,
        conf: dict,
    ) -> None:
        handlers = star_handlers_registry.get_handlers_by_event_type(
            EventType.AdapterMessageEvent,
            plugins_name=event.plugins_name,
        )
        handlers = await SessionPluginManager.filter_handlers_by_session(
            event,
            handlers,
        )

        handlers_by_module: dict[str, list[StarHandlerMetadata]] = {}
        for handler in handlers:
            if self._is_command_group_handler(handler):
                continue
            if not self._match_inline_command_handler(
                event,
                handler,
                command_query,
                conf,
            ):
                continue
            handlers_by_module.setdefault(handler.handler_module_path, []).append(
                handler
            )

        options = []
        inline_query_id = str(getattr(event, "inline_query_id", ""))
        for ordinal, (module_path, module_handlers) in enumerate(
            handlers_by_module.items(), start=1
        ):
            md = star_map.get(module_path)
            if not md:
                continue
            plugin_name = md.display_name or md.name or module_path.rsplit(".", 1)[-1]
            description = md.desc or "Run this command plugin for the inline query."
            result_id = telegram_inline_result_registry.register(
                inline_query_id=inline_query_id,
                kind="plugin",
                query=str(getattr(event, "query", "") or "").strip(),
                ordinal=ordinal,
                handlers=module_handlers,
                plugin_module_path=module_path,
                plugin_name=plugin_name,
                use_command_flow=True,
            )
            options.append(
                {
                    "id": result_id,
                    "title": str(plugin_name),
                    "description": str(description),
                    "kind": "plugin",
                }
            )

        if hasattr(event, "answer_inline_options"):
            await event.answer_inline_options(options)
        else:
            logger.warning("Inline query event does not support targeted options.")

    def _is_command_group_handler(self, handler: StarHandlerMetadata) -> bool:
        return any(
            isinstance(filter_, CommandGroupFilter) for filter_ in handler.event_filters
        )

    def _match_inline_command_handler(
        self,
        event: AstrMessageEvent,
        handler: StarHandlerMetadata,
        command_query: str,
        conf: dict,
    ) -> bool:
        if not any(
            isinstance(filter_, CommandFilter) for filter_ in handler.event_filters
        ):
            return False

        original_message = event.message_str
        original_is_wake = event.is_wake
        original_is_at_or_wake = event.is_at_or_wake_command
        parsed_before = event.get_extra("parsed_params", None)
        had_parsed_before = "parsed_params" in event.get_extra(default={})

        event.message_str = command_query
        event.is_wake = True
        event.is_at_or_wake_command = True
        event._extras.pop("parsed_params", None)
        try:
            for filter_ in handler.event_filters:
                if isinstance(filter_, PermissionTypeFilter):
                    if not filter_.filter(event, conf):
                        return False
                    continue
                if not filter_.filter(event, conf):
                    return False
            return True
        except Exception:
            return False
        finally:
            event.message_str = original_message
            event.is_wake = original_is_wake
            event.is_at_or_wake_command = original_is_at_or_wake
            event._extras.pop("parsed_params", None)
            if had_parsed_before:
                event.set_extra("parsed_params", parsed_before)

    def _print_event(self, event: AstrMessageEvent, conf_name: str) -> None:
        """用于记录事件信息

        Args:
            event (AstrMessageEvent): 事件对象

        """
        # 如果有发送者名称: [平台名] 发送者名称/发送者ID: 消息概要
        if event.get_sender_name():
            logger.info(
                f"[{conf_name}] [{event.get_platform_id()}({event.get_platform_name()})] {event.get_sender_name()}/{event.get_sender_id()}: {event.get_message_outline()}",
            )
        # 没有发送者名称: [平台名] 发送者ID: 消息概要
        else:
            logger.info(
                f"[{conf_name}] [{event.get_platform_id()}({event.get_platform_name()})] {event.get_sender_id()}: {event.get_message_outline()}",
            )
