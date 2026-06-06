import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from astrbot.api.event import MessageChain
from astrbot.api.message_components import Image
from astrbot.api.platform import MessageType, PlatformMetadata
from astrbot.core.event_bus import EventBus
from astrbot.core.message.message_event_result import MessageEventResult
from astrbot.core.pipeline.result_decorate.stage import ResultDecorateStage
from astrbot.core.pipeline.waking_check.stage import WakingCheckStage
from astrbot.core.platform.sources.telegram.inline_registry import (
    TELEGRAM_INLINE_STOP_CALLBACK_DATA,
    telegram_inline_result_registry,
    telegram_inline_stop_registry,
)
from astrbot.core.platform.sources.telegram.tg_event import (
    TelegramCallbackQueryEvent,
    TelegramChosenInlineResultEvent,
    TelegramInlineQueryEvent,
)
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.star import StarMetadata, star_map
from astrbot.core.star.star_handler import EventType, StarHandlerMetadata


async def _handler(event):
    yield event.plain_result("ok")


def _make_handler(module_path: str, name: str) -> StarHandlerMetadata:
    return StarHandlerMetadata(
        event_type=EventType.InlineQueryEvent,
        handler_full_name=f"{module_path}_{name}",
        handler_name=name,
        handler_module_path=module_path,
        handler=_handler,
        event_filters=[],
    )


async def _command_handler(self, event, value: str = ""):
    yield event.plain_result(value)


def _make_command_handler(
    module_path: str,
    name: str,
    command_name: str,
) -> StarHandlerMetadata:
    handler = StarHandlerMetadata(
        event_type=EventType.AdapterMessageEvent,
        handler_full_name=f"{module_path}_{name}",
        handler_name=name,
        handler_module_path=module_path,
        handler=_command_handler,
        event_filters=[],
    )
    command_filter = CommandFilter(command_name, handler_md=handler)
    handler.event_filters = [command_filter]
    return handler


class DummyInlineEvent:
    def __init__(self, query="hello"):
        self.inline_query_id = "iq-1"
        self.query = query
        self.message_str = query
        self.unified_msg_origin = "telegram:other:user1"
        self.plugins_name = None
        self.is_wake = False
        self.is_at_or_wake_command = False
        self._extras = {}
        self.answer_inline_options = AsyncMock()

    def get_message_str(self):
        return self.message_str

    def get_extra(self, key=None, default=None):
        if key is None:
            return self._extras
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value


@pytest.fixture(autouse=True)
def clear_inline_registry():
    telegram_inline_result_registry.clear()
    telegram_inline_stop_registry.clear()
    yield
    telegram_inline_result_registry.clear()
    telegram_inline_stop_registry.clear()


@pytest.mark.asyncio
async def test_inline_query_generates_only_llm_option_without_plugin_handlers():
    config_mgr = MagicMock()
    config_mgr.get_conf.return_value = {"plugin_set": ["*"]}
    bus = EventBus(asyncio.Queue(), {}, config_mgr)
    event = DummyInlineEvent()

    with (
        patch(
            "astrbot.core.event_bus.star_handlers_registry.get_handlers_by_event_type",
            return_value=[],
        ),
        patch(
            "astrbot.core.event_bus.SessionPluginManager.filter_handlers_by_session",
            AsyncMock(side_effect=lambda _event, handlers: handlers),
        ),
    ):
        handled = await bus._maybe_handle_inline_query(event)

    assert handled is True
    event.answer_inline_options.assert_awaited_once()
    options = event.answer_inline_options.await_args.args[0]
    assert [option["kind"] for option in options] == ["llm"]
    assert options[0]["title"] == "LLM"
    assert options[0]["description"] == "hello"
    assert telegram_inline_result_registry.resolve(options[0]["id"]).kind == "llm"


@pytest.mark.asyncio
async def test_inline_query_generates_one_option_per_plugin_module(monkeypatch):
    config_mgr = MagicMock()
    config_mgr.get_conf.return_value = {"plugin_set": ["plugin-one"]}
    bus = EventBus(asyncio.Queue(), {}, config_mgr)
    event = DummyInlineEvent("lookup")
    h1 = _make_handler("plugins.one", "first")
    h2 = _make_handler("plugins.one", "second")
    h3 = _make_handler("plugins.two", "first")

    monkeypatch.setitem(
        star_map,
        "plugins.one",
        StarMetadata(name="plugin-one", display_name="Plugin One", desc="First plugin"),
    )
    monkeypatch.setitem(
        star_map,
        "plugins.two",
        StarMetadata(
            name="plugin-two", display_name="Plugin Two", desc="Second plugin"
        ),
    )

    with (
        patch(
            "astrbot.core.event_bus.star_handlers_registry.get_handlers_by_event_type",
            return_value=[h1, h2, h3],
        ) as get_handlers,
        patch(
            "astrbot.core.event_bus.SessionPluginManager.filter_handlers_by_session",
            AsyncMock(return_value=[h1, h2]),
        ),
    ):
        handled = await bus._maybe_handle_inline_query(event)

    assert handled is True
    get_handlers.assert_any_call(
        EventType.InlineQueryEvent,
        plugins_name=["plugin-one"],
    )
    options = event.answer_inline_options.await_args.args[0]
    assert [option["title"] for option in options] == ["LLM", "Plugin One"]
    target = telegram_inline_result_registry.resolve(options[1]["id"])
    assert target.kind == "plugin"
    assert target.plugin_module_path == "plugins.one"
    assert target.handlers == [h1, h2]


@pytest.mark.asyncio
async def test_inline_query_prefixed_query_keeps_normal_options_and_adds_command_entry(
    monkeypatch,
):
    config_mgr = MagicMock()
    config_mgr.get_conf.return_value = {
        "plugin_set": ["*"],
        "wake_prefix": ["/"],
    }
    bus = EventBus(asyncio.Queue(), {}, config_mgr)
    event = DummyInlineEvent("/asmr RJ123456")
    inline_handler = _make_handler("plugins.inline", "inline")
    monkeypatch.setitem(
        star_map,
        "plugins.inline",
        StarMetadata(
            name="inline-plugin",
            display_name="Inline Plugin",
            desc="Inline handler",
        ),
    )

    def get_handlers(event_type, plugins_name=None):
        if event_type == EventType.InlineQueryEvent:
            return [inline_handler]
        return []

    with (
        patch(
            "astrbot.core.event_bus.star_handlers_registry.get_handlers_by_event_type",
            side_effect=get_handlers,
        ),
        patch(
            "astrbot.core.event_bus.SessionPluginManager.filter_handlers_by_session",
            AsyncMock(side_effect=lambda _event, handlers: handlers),
        ),
    ):
        handled = await bus._maybe_handle_inline_query(event)

    assert handled is True
    options = event.answer_inline_options.await_args.args[0]
    assert [option["title"] for option in options] == [
        "LLM",
        "Inline Plugin",
        "插件命令",
    ]
    target = telegram_inline_result_registry.resolve(options[-1]["id"])
    assert target.kind == "command"
    assert target.query == "/asmr RJ123456"
    assert target.use_command_flow is True
    assert target.handlers == []


@pytest.mark.asyncio
async def test_inline_query_non_prefixed_query_has_no_command_entry(monkeypatch):
    config_mgr = MagicMock()
    config_mgr.get_conf.return_value = {
        "plugin_set": ["*"],
        "wake_prefix": ["/"],
    }
    bus = EventBus(asyncio.Queue(), {}, config_mgr)
    event = DummyInlineEvent("asmr RJ123456")
    inline_handler = _make_handler("plugins.inline", "inline")
    monkeypatch.setitem(
        star_map,
        "plugins.inline",
        StarMetadata(
            name="inline-plugin",
            display_name="Inline Plugin",
            desc="Inline handler",
        ),
    )

    with (
        patch(
            "astrbot.core.event_bus.star_handlers_registry.get_handlers_by_event_type",
            return_value=[inline_handler],
        ),
        patch(
            "astrbot.core.event_bus.SessionPluginManager.filter_handlers_by_session",
            AsyncMock(side_effect=lambda _event, handlers: handlers),
        ),
    ):
        handled = await bus._maybe_handle_inline_query(event)

    assert handled is True
    options = event.answer_inline_options.await_args.args[0]
    assert [option["title"] for option in options] == ["LLM", "Inline Plugin"]


class DummyChosenEvent:
    def __init__(self, result_id: str, query: str = "hello"):
        self.result_id = result_id
        self.inline_message_id = "inline-1"
        self.query = query
        self.message_str = query
        self.message_obj = SimpleNamespace(type=MessageType.OTHER_MESSAGE)
        self._extras = {}
        self.plugins_name = None
        self.call_llm = False
        self.is_wake = False
        self.is_at_or_wake_command = False
        self.role = "member"

    @property
    def unified_msg_origin(self):
        return "telegram:other:user1"

    def set_extra(self, key, value):
        self._extras[key] = value

    def get_extra(self, key=None, default=None):
        if key is None:
            return self._extras
        return self._extras.get(key, default)

    def get_sender_id(self):
        return "user1"

    def get_message_str(self):
        return self.message_str

    def get_self_id(self):
        return "bot1"

    def stop_event(self):
        self.stopped = True

    def get_messages(self):
        return []

    def is_private_chat(self):
        return True


async def _build_waking_stage():
    stage = WakingCheckStage()
    ctx = MagicMock()
    ctx.astrbot_config = {
        "admins_id": [],
        "wake_prefix": ["/"],
        "plugin_set": ["*"],
        "platform_settings": {
            "no_permission_reply": False,
            "friend_message_needs_wake_prefix": False,
            "ignore_bot_self_message": False,
            "ignore_at_all": False,
            "unique_session": False,
        },
        "disable_builtin_commands": False,
    }
    await stage.initialize(ctx)
    return stage


@pytest.mark.asyncio
async def test_chosen_inline_llm_route_activates_only_llm():
    result_id = telegram_inline_result_registry.register(
        inline_query_id="iq-1",
        kind="llm",
        query="explain",
        ordinal=0,
    )
    event = DummyChosenEvent(result_id, "old")
    stage = await _build_waking_stage()

    await stage.process(event)

    assert event.message_str == "explain"
    assert event.is_wake is True
    assert event.is_at_or_wake_command is True
    assert event.call_llm is False
    assert event.get_extra("activated_handlers") == []


@pytest.mark.asyncio
async def test_chosen_inline_plugin_route_activates_only_registered_handlers():
    handler = _make_handler("plugins.one", "first")
    result_id = telegram_inline_result_registry.register(
        inline_query_id="iq-1",
        kind="plugin",
        query="lookup",
        ordinal=1,
        handlers=[handler],
        plugin_module_path="plugins.one",
        plugin_name="Plugin One",
    )
    event = DummyChosenEvent(result_id, "old")
    stage = await _build_waking_stage()

    with patch(
        "astrbot.core.pipeline.waking_check.stage.SessionPluginManager.filter_handlers_by_session",
        AsyncMock(return_value=[handler]),
    ):
        await stage.process(event)

    assert event.message_str == "lookup"
    assert event.call_llm is True
    assert event.get_extra("activated_handlers") == [handler]
    assert event.get_extra("handlers_parsed_params") == {}


@pytest.mark.asyncio
async def test_chosen_inline_command_route_uses_normal_command_pipeline():
    handler = _make_command_handler("plugins.asmr", "asmr", "asmr")
    result_id = telegram_inline_result_registry.register(
        inline_query_id="iq-1",
        kind="command",
        query="/asmr RJ123456",
        ordinal=1,
        use_command_flow=True,
    )
    event = DummyChosenEvent(result_id, "old")
    stage = await _build_waking_stage()

    with (
        patch(
            "astrbot.core.pipeline.waking_check.stage.star_handlers_registry.get_handlers_by_event_type",
            side_effect=lambda event_type, plugins_name=None: (
                [handler] if event_type == EventType.AdapterMessageEvent else []
            ),
        ),
        patch(
            "astrbot.core.pipeline.waking_check.stage.SessionPluginManager.filter_handlers_by_session",
            AsyncMock(side_effect=lambda _event, handlers: handlers),
        ),
    ):
        await stage.process(event)

    assert event.message_str == "asmr RJ123456"
    assert event.get_extra("wake_prefix") == "/"
    assert event.get_extra("activated_handlers") == [handler]
    assert event.get_extra("handlers_parsed_params") == {
        handler.handler_full_name: {"value": "RJ123456"}
    }


@pytest.mark.asyncio
async def test_chosen_inline_unknown_result_preserves_legacy_chosen_handlers():
    chosen_handler = _make_handler("plugins.chosen", "chosen")
    chosen_handler.event_type = EventType.ChosenInlineResultEvent
    event = DummyChosenEvent("missing-result", "legacy")
    stage = await _build_waking_stage()

    with (
        patch(
            "astrbot.core.pipeline.waking_check.stage.star_handlers_registry.get_handlers_by_event_type",
            side_effect=lambda event_type, plugins_name=None: (
                [chosen_handler]
                if event_type == EventType.ChosenInlineResultEvent
                else []
            ),
        ),
        patch(
            "astrbot.core.pipeline.waking_check.stage.SessionPluginManager.filter_handlers_by_session",
            AsyncMock(side_effect=lambda _event, handlers: handlers),
        ),
    ):
        await stage.process(event)

    assert event.get_extra("activated_handlers") == [chosen_handler]


def _make_chosen_event(client):
    return TelegramChosenInlineResultEvent(
        result_id="result-1",
        from_user_id="user1",
        from_username="alice",
        query="query",
        inline_message_id="inline-1",
        platform_meta=PlatformMetadata("telegram", "telegram", "telegram"),
        session_id="user1",
        client=client,
    )


def _make_inline_query_event(client, query="tell me a joke"):
    return TelegramInlineQueryEvent(
        query=query,
        from_user_id="user1",
        from_username="alice",
        inline_query_id="iq-1",
        offset="",
        platform_meta=PlatformMetadata("telegram", "telegram", "telegram"),
        session_id="user1",
        client=client,
    )


def _make_callback_event(client, data=TELEGRAM_INLINE_STOP_CALLBACK_DATA):
    return TelegramCallbackQueryEvent(
        callback_query_id="cb-1",
        data=data,
        from_user_id="user1",
        from_username="alice",
        message=None,
        inline_message_id="inline-1",
        platform_meta=PlatformMetadata("telegram", "telegram", "telegram"),
        session_id="user1",
        client=client,
    )


@pytest.mark.asyncio
async def test_chosen_inline_text_only_edits_text():
    client = MagicMock()
    client.edit_message_text = AsyncMock()
    client.edit_message_media = AsyncMock()
    event = _make_chosen_event(client)

    await event.send_with_client(client, MessageChain().message("plain reply"), "user1")

    client.edit_message_text.assert_awaited_once()
    client.edit_message_media.assert_not_awaited()
    assert client.edit_message_text.await_args.kwargs["reply_markup"] is None
    assert event.unified_msg_origin == "telegram:OtherMessage:user1"


def test_chosen_inline_entities_use_compact_layout_with_model_name():
    client = MagicMock()
    event = _make_chosen_event(client)
    event.set_extra("current_chat_model", "openrouter/openai/gpt-5.5")

    text, entities = event._build_inline_entities("answer body")

    assert text == "❓ query\n🤖 gpt-5.5\nanswer body"
    assert len(entities) == 1
    assert entities[0].type == "expandable_blockquote"
    assert entities[0].offset == event._utf16_len("❓ query\n")
    assert entities[0].length == event._utf16_len("🤖 gpt-5.5\nanswer body")


@pytest.mark.asyncio
async def test_chosen_inline_text_preserves_paragraph_breaks():
    client = MagicMock()
    client.edit_message_text = AsyncMock()
    client.edit_message_media = AsyncMock()
    event = _make_chosen_event(client)
    chain = MessageChain().message("first paragraph\n\n")
    chain.message("second paragraph")

    await event.send_with_client(client, chain, "user1")

    kwargs = client.edit_message_text.await_args.kwargs
    assert kwargs["text"] == "❓ query\n🤖 回答\nfirst paragraph\n\nsecond paragraph"


@pytest.mark.asyncio
async def test_chosen_inline_url_image_with_text_edits_photo_caption():
    client = MagicMock()
    client.edit_message_text = AsyncMock()
    client.edit_message_media = AsyncMock()
    event = _make_chosen_event(client)
    chain = MessageChain().message("caption text")
    chain.chain.append(Image.fromURL("https://example.com/a.jpg"))

    await event.send_with_client(client, chain, "user1")

    client.edit_message_media.assert_awaited_once()
    kwargs = client.edit_message_media.await_args.kwargs
    assert kwargs["inline_message_id"] == "inline-1"
    assert kwargs["media"].media == "https://example.com/a.jpg"
    assert kwargs["media"].caption == "caption text"
    assert kwargs["reply_markup"] is None
    client.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_chosen_inline_url_image_without_caption_edits_photo_only():
    client = MagicMock()
    client.edit_message_text = AsyncMock()
    client.edit_message_media = AsyncMock()
    event = _make_chosen_event(client)
    chain = MessageChain()
    chain.chain.append(Image.fromURL("https://example.com/a.jpg"))

    await event.send_with_client(client, chain, "user1")

    client.edit_message_media.assert_awaited_once()
    kwargs = client.edit_message_media.await_args.kwargs
    assert kwargs["media"].media == "https://example.com/a.jpg"
    assert kwargs["media"].caption is None
    client.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_chosen_inline_unpublishable_local_image_falls_back_to_text(tmp_path):
    client = MagicMock()
    client.edit_message_text = AsyncMock()
    client.edit_message_media = AsyncMock()
    event = _make_chosen_event(client)
    image_path = tmp_path / "a.jpg"
    image_path.write_bytes(b"fake")
    chain = MessageChain().message("fallback text")
    chain.chain.append(Image.fromFileSystem(str(image_path)))

    with patch.object(
        Image,
        "register_to_file_service",
        AsyncMock(side_effect=Exception("no file service")),
    ):
        await event.send_with_client(client, chain, "user1")

    client.edit_message_media.assert_not_awaited()
    client.edit_message_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_inline_option_placeholders_include_original_query_text():
    client = MagicMock()
    event = _make_inline_query_event(client, query="/asmr RJ123456")

    results = await event._build_inline_option_results(
        [
            {
                "id": "llm-1",
                "title": "LLM",
                "description": "Ask the model",
                "kind": "llm",
            },
            {
                "id": "cmd-1",
                "title": "插件命令",
                "description": "Run a command plugin",
                "kind": "command",
            },
        ]
    )

    assert results[0].input_message_content.message_text == "/asmr RJ123456"
    assert results[1].input_message_content.message_text == "/asmr RJ123456"
    assert results[0].reply_markup.inline_keyboard[0][0].callback_data == (
        TELEGRAM_INLINE_STOP_CALLBACK_DATA
    )


@pytest.mark.asyncio
async def test_result_decorate_skips_quote_and_mention_for_chosen_inline():
    client = MagicMock()
    event = _make_chosen_event(client)
    event.set_result(MessageEventResult().message("reset ok"))

    stage = ResultDecorateStage()
    ctx = MagicMock()
    ctx.astrbot_config = {
        "platform_settings": {
            "reply_prefix": "",
            "reply_with_mention": True,
            "reply_with_quote": True,
            "forward_threshold": 100,
            "segmented_reply": {
                "enable": False,
                "only_llm_result": False,
                "words_count_threshold": 1000,
                "split_mode": "regex",
                "regex": r".*?[。？！~…]+|.+$",
                "split_words": ["。", "？", "！", "~", "…"],
                "content_cleanup_rule": "",
            },
        },
        "t2i_word_threshold": 150,
        "t2i_strategy": "local",
        "t2i_active_template": "default",
        "content_safety": {"also_use_in_response": False},
        "provider_settings": {"display_reasoning_text": False},
        "provider_tts_settings": {"trigger_probability": 1, "enable": False},
        "t2i": False,
    }
    ctx.plugin_manager = MagicMock()
    ctx.plugin_manager.context.get_using_tts_provider.return_value = None
    await stage.initialize(ctx)

    async for _ in stage.process(event):
        pass

    result = event.get_result()
    assert result is not None
    assert [type(comp).__name__ for comp in result.chain] == ["Plain"]


@pytest.mark.asyncio
async def test_chosen_inline_streaming_edits_with_stop_markup():
    client = MagicMock()
    client.edit_message_text = AsyncMock()
    client.edit_message_media = AsyncMock()
    event = _make_chosen_event(client)

    async def generator():
        yield MessageChain().message("streaming reply")

    await event.send_streaming(generator())

    assert client.edit_message_text.await_count >= 1
    assert client.edit_message_text.await_args.kwargs["reply_markup"] is None


@pytest.mark.asyncio
async def test_chosen_inline_streaming_preserves_spaces_between_chunks():
    client = MagicMock()
    client.edit_message_text = AsyncMock()
    client.edit_message_media = AsyncMock()
    event = _make_chosen_event(client)

    async def generator():
        yield MessageChain().message("hello ")
        yield MessageChain().message("world")

    await event.send_streaming(generator())

    kwargs = client.edit_message_text.await_args.kwargs
    assert kwargs["text"] == "❓ query\n🤖 回答\nhello world"


@pytest.mark.asyncio
async def test_chosen_inline_streaming_image_only_edits_media():
    client = MagicMock()
    client.edit_message_text = AsyncMock()
    client.edit_message_media = AsyncMock()
    event = _make_chosen_event(client)

    async def generator():
        chain = MessageChain()
        chain.chain.append(Image.fromURL("https://example.com/stream.jpg"))
        yield chain

    await event.send_streaming(generator())

    client.edit_message_media.assert_awaited_once()
    kwargs = client.edit_message_media.await_args.kwargs
    assert kwargs["media"].media == "https://example.com/stream.jpg"
    assert kwargs["media"].caption is None
    client.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_inline_stop_callback_requests_agent_stop():
    client = MagicMock()
    client.answer_callback_query = AsyncMock()
    client.edit_message_reply_markup = AsyncMock()
    event = _make_callback_event(client)
    stage = await _build_waking_stage()
    telegram_inline_stop_registry.register("inline-1", "telegram:other:user1", "user1")

    with (
        patch(
            "astrbot.core.pipeline.waking_check.stage.active_event_registry.request_agent_stop_all",
            return_value=1,
        ) as request_stop,
        patch(
            "astrbot.core.pipeline.waking_check.stage.active_event_registry.stop_all",
            return_value=0,
        ) as stop_all,
    ):
        await stage.process(event)

    request_stop.assert_called_once_with("telegram:other:user1")
    stop_all.assert_not_called()
    client.answer_callback_query.assert_awaited_once()
    client.edit_message_reply_markup.assert_awaited_once()


@pytest.mark.asyncio
async def test_inline_stop_callback_uses_full_stop_for_third_party_runner():
    client = MagicMock()
    client.answer_callback_query = AsyncMock()
    client.edit_message_reply_markup = AsyncMock()
    event = _make_callback_event(client)
    stage = await _build_waking_stage()
    stage.ctx.get_config.return_value = {
        "provider_settings": {"agent_runner_type": "coze"}
    }
    telegram_inline_stop_registry.register("inline-1", "telegram:other:user1", "user1")

    with (
        patch(
            "astrbot.core.pipeline.waking_check.stage.active_event_registry.request_agent_stop_all",
            return_value=0,
        ) as request_stop,
        patch(
            "astrbot.core.pipeline.waking_check.stage.active_event_registry.stop_all",
            return_value=1,
        ) as stop_all,
    ):
        await stage.process(event)

    stop_all.assert_called_once_with("telegram:other:user1")
    request_stop.assert_not_called()


@pytest.mark.asyncio
async def test_inline_stop_callback_without_mapping_returns_no_running_task():
    client = MagicMock()
    client.answer_callback_query = AsyncMock()
    client.edit_message_reply_markup = AsyncMock()
    event = _make_callback_event(client)
    stage = await _build_waking_stage()

    with (
        patch(
            "astrbot.core.pipeline.waking_check.stage.active_event_registry.request_agent_stop_all",
            return_value=0,
        ) as request_stop,
        patch(
            "astrbot.core.pipeline.waking_check.stage.active_event_registry.stop_all",
            return_value=0,
        ) as stop_all,
    ):
        await stage.process(event)

    request_stop.assert_not_called()
    stop_all.assert_not_called()
    client.answer_callback_query.assert_awaited_once()
    assert (
        client.answer_callback_query.await_args.kwargs["text"]
        == "No running task in this inline session."
    )
    client.edit_message_reply_markup.assert_not_awaited()


@pytest.mark.asyncio
async def test_inline_stop_callback_ignores_non_owner_user():
    client = MagicMock()
    client.answer_callback_query = AsyncMock()
    client.edit_message_reply_markup = AsyncMock()
    event = _make_callback_event(client)
    event.from_user_id = "user2"
    event.message_obj.sender.user_id = "user2"
    stage = await _build_waking_stage()
    telegram_inline_stop_registry.register("inline-1", "telegram:other:user1", "user1")

    with (
        patch(
            "astrbot.core.pipeline.waking_check.stage.active_event_registry.request_agent_stop_all",
            return_value=1,
        ) as request_stop,
        patch(
            "astrbot.core.pipeline.waking_check.stage.active_event_registry.stop_all",
            return_value=1,
        ) as stop_all,
    ):
        await stage.process(event)

    request_stop.assert_not_called()
    stop_all.assert_not_called()
    client.answer_callback_query.assert_awaited_once()
    assert (
        client.answer_callback_query.await_args.kwargs["text"]
        == "Only the user who triggered this inline task can stop it."
    )
    client.edit_message_reply_markup.assert_not_awaited()
