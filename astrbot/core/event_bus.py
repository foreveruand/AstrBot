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
from astrbot.core.message.message_event_result import (
    CommandResult,
    MessageChain,
    MessageEventResult,
    ResultContentType,
)
from astrbot.core.pipeline.context_utils import call_handler
from astrbot.core.pipeline.scheduler import PipelineScheduler
from astrbot.core.star.session_plugin_manager import SessionPluginManager
from astrbot.core.star.star import star_map
from astrbot.core.star.star_handler import EventType, star_handlers_registry

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

        handlers = star_handlers_registry.get_handlers_by_event_type(
            EventType.InlineQueryEvent,
            plugins_name=event.plugins_name,
        )
        handlers = await SessionPluginManager.filter_handlers_by_session(
            event,
            handlers,
        )
        handled = False

        for handler in handlers:
            md = star_map.get(handler.handler_module_path)
            if not md:
                continue
            try:
                wrapper = call_handler(event, handler.handler)
                async for _ in wrapper:
                    if await self._dispatch_inline_result(event):
                        handled = True
                        break
                event.clear_result()
            except Exception as e:
                logger.error(
                    f"InlineQuery handler error: {md.name} - {handler.handler_name}: {e}",
                    exc_info=True,
                )

            if event.is_stopped():
                break

        if not handled and query:
            await event.send(MessageChain().message(query))
            handled = True

        return True

    async def _dispatch_inline_result(self, event: AstrMessageEvent) -> bool:
        result = event.get_result()
        if result is None:
            return False
        if isinstance(result, MessageEventResult | CommandResult):
            if result.result_content_type == ResultContentType.STREAMING_RESULT:
                if result.async_stream is None:
                    logger.warning("async_stream 为空，跳过发送。")
                    return True
                await event.send_streaming(result.async_stream)
                return True
            await event.send(result)
            return True
        return False

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
