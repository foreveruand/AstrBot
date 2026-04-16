import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from astrbot.api.event import MessageChain
from astrbot.api.message_components import Image
from astrbot.api.platform import MessageType, PlatformMetadata
from astrbot.core.event_bus import EventBus
from astrbot.core.pipeline.waking_check.stage import WakingCheckStage
from astrbot.core.platform.sources.telegram.inline_registry import (
    telegram_inline_result_registry,
)
from astrbot.core.platform.sources.telegram.tg_event import (
    TelegramChosenInlineResultEvent,
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
    yield
    telegram_inline_result_registry.clear()


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
async def test_inline_query_command_mode_only_lists_matching_command_plugin(
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
    asmr_handler = _make_command_handler("plugins.asmr", "asmr", "asmr")
    other_handler = _make_command_handler("plugins.other", "other", "other")

    monkeypatch.setitem(
        star_map,
        "plugins.asmr",
        StarMetadata(name="asmr", display_name="ASMR", desc="Download ASMR"),
    )
    monkeypatch.setitem(
        star_map,
        "plugins.other",
        StarMetadata(name="other", display_name="Other", desc="Other command"),
    )

    def get_handlers(event_type, plugins_name=None):
        if event_type == EventType.InlineQueryEvent:
            return [inline_handler]
        if event_type == EventType.AdapterMessageEvent:
            return [asmr_handler, other_handler]
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
    assert [option["title"] for option in options] == ["ASMR"]
    target = telegram_inline_result_registry.resolve(options[0]["id"])
    assert target.kind == "plugin"
    assert target.query == "/asmr RJ123456"
    assert target.use_command_flow is True
    assert target.handlers == [asmr_handler]


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
        kind="plugin",
        query="/asmr RJ123456",
        ordinal=1,
        handlers=[handler],
        plugin_module_path="plugins.asmr",
        plugin_name="ASMR",
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


@pytest.mark.asyncio
async def test_chosen_inline_text_only_edits_text():
    client = MagicMock()
    client.edit_message_text = AsyncMock()
    client.edit_message_media = AsyncMock()
    event = _make_chosen_event(client)

    await event.send_with_client(client, MessageChain().message("plain reply"), "user1")

    client.edit_message_text.assert_awaited_once()
    client.edit_message_media.assert_not_awaited()


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
