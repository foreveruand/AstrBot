from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import mcp
import pytest

from astrbot.core.agent.agent import Agent
from astrbot.core.agent.handoff import HandoffTool
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
from astrbot.core.message.components import Image
from astrbot.core.provider.func_tool_manager import (
    FunctionToolManager,
    _PermissionGuardedTool,
)
from astrbot.core.provider.provider import Provider


class _DummyEvent:
    def __init__(
        self,
        message_components: list[object] | None = None,
        extras: dict[str, object] | None = None,
    ) -> None:
        self.unified_msg_origin = "webchat:FriendMessage:webchat!user!session"
        self.message_obj = SimpleNamespace(message=message_components or [])
        self.role = "member"
        self._extras = extras or {}

    def get_extra(self, key: str):
        return self._extras.get(key)


class _DummyTool:
    def __init__(self) -> None:
        self.name = "transfer_to_subagent"
        self.agent = SimpleNamespace(name="subagent")


def _build_run_context(message_components: list[object] | None = None):
    event = _DummyEvent(message_components=message_components)
    ctx = SimpleNamespace(event=event, context=SimpleNamespace())
    return ContextWrapper(context=ctx)


class _DoneRunner:
    async def step_until_done(self, _max_step):
        for item in ():
            yield item

    def get_final_llm_resp(self):
        return SimpleNamespace(role="assistant", completion_text="done")


def test_build_handoff_toolset_keeps_permission_guards_for_default_tools():
    mgr = FunctionToolManager()
    plugin_tool = FunctionTool(
        name="admin_only_mcp",
        description="admin tool",
        parameters={"type": "object", "properties": {}},
    )
    handoff = HandoffTool(Agent(name="child"))
    mgr.func_list = [plugin_tool, handoff]

    event = _DummyEvent()
    context = SimpleNamespace(
        get_config=lambda **_kwargs: {
            "provider_settings": {"computer_use_runtime": "none"}
        },
        get_llm_tool_manager=lambda: mgr,
    )
    run_context = ContextWrapper(context=SimpleNamespace(event=event, context=context))

    toolset = FunctionToolExecutor._build_handoff_toolset(run_context, tools=None)

    assert toolset is not None
    assert isinstance(toolset.get_tool("admin_only_mcp"), _PermissionGuardedTool)
    assert toolset.get_tool("transfer_to_child") is None


@pytest.mark.asyncio
async def test_collect_handoff_image_urls_normalizes_filters_and_appends_event_image(
    monkeypatch: pytest.MonkeyPatch,
):
    async def _fake_convert_to_file_path(self):
        return "/tmp/event_image.png"

    monkeypatch.setattr(Image, "convert_to_file_path", _fake_convert_to_file_path)

    run_context = _build_run_context([Image(file="file:///tmp/original.png")])
    image_urls_input = (
        " https://example.com/a.png ",
        "/tmp/not_an_image.txt",
        "/tmp/local.webp",
        123,
    )

    image_urls = await FunctionToolExecutor._collect_handoff_image_urls(
        run_context,
        image_urls_input,
    )

    assert image_urls == [
        "https://example.com/a.png",
        "/tmp/local.webp",
        "/tmp/event_image.png",
    ]


@pytest.mark.asyncio
async def test_collect_handoff_image_urls_skips_failed_event_image_conversion(
    monkeypatch: pytest.MonkeyPatch,
):
    async def _fake_convert_to_file_path(self):
        raise RuntimeError("boom")

    monkeypatch.setattr(Image, "convert_to_file_path", _fake_convert_to_file_path)

    run_context = _build_run_context([Image(file="file:///tmp/original.png")])
    image_urls = await FunctionToolExecutor._collect_handoff_image_urls(
        run_context,
        ["https://example.com/a.png"],
    )

    assert image_urls == ["https://example.com/a.png"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("image_refs", "expected_supported_refs"),
    [
        pytest.param(
            (
                "https://example.com/valid.png",
                "base64://iVBORw0KGgoAAAANSUhEUgAAAAUA",
                "file:///tmp/photo.heic",
                "file://localhost/tmp/vector.svg",
                "file://fileserver/share/image.webp",
                "file:///tmp/not-image.txt",
                "mailto:user@example.com",
                "random-string-without-scheme-or-extension",
            ),
            {
                "https://example.com/valid.png",
                "base64://iVBORw0KGgoAAAANSUhEUgAAAAUA",
                "file:///tmp/photo.heic",
                "file://localhost/tmp/vector.svg",
                "file://fileserver/share/image.webp",
            },
            id="mixed_supported_and_unsupported_refs",
        ),
    ],
)
async def test_collect_handoff_image_urls_filters_supported_schemes_and_extensions(
    image_refs: tuple[str, ...],
    expected_supported_refs: set[str],
):
    run_context = _build_run_context([])
    result = await FunctionToolExecutor._collect_handoff_image_urls(
        run_context, image_refs
    )
    assert set(result) == expected_supported_refs


@pytest.mark.asyncio
async def test_collect_handoff_image_urls_collects_event_image_when_args_is_none(
    monkeypatch: pytest.MonkeyPatch,
):
    async def _fake_convert_to_file_path(self):
        return "/tmp/event_only.png"

    monkeypatch.setattr(Image, "convert_to_file_path", _fake_convert_to_file_path)

    run_context = _build_run_context([Image(file="file:///tmp/original.png")])
    image_urls = await FunctionToolExecutor._collect_handoff_image_urls(
        run_context,
        None,
    )

    assert image_urls == ["/tmp/event_only.png"]


@pytest.mark.asyncio
async def test_do_handoff_background_reports_prepared_image_urls(
    monkeypatch: pytest.MonkeyPatch,
):
    captured: dict = {}

    async def _fake_execute_handoff(
        cls, tool, run_context, image_urls_prepared=False, **tool_args
    ):
        assert image_urls_prepared is True
        yield mcp.types.CallToolResult(
            content=[mcp.types.TextContent(type="text", text="ok")]
        )

    async def _fake_wake(cls, run_context, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(
        FunctionToolExecutor,
        "_execute_handoff",
        classmethod(_fake_execute_handoff),
    )
    monkeypatch.setattr(
        FunctionToolExecutor,
        "_wake_main_agent_for_background_result",
        classmethod(_fake_wake),
    )

    run_context = _build_run_context()
    await FunctionToolExecutor._do_handoff_background(
        tool=_DummyTool(),
        run_context=run_context,
        task_id="task-id",
        input="hello",
        image_urls="https://example.com/raw.png",
    )

    assert captured["tool_args"]["image_urls"] == ["https://example.com/raw.png"]


@pytest.mark.asyncio
async def test_execute_handoff_skips_renormalize_when_image_urls_prepared(
    monkeypatch: pytest.MonkeyPatch,
):
    captured: dict = {}

    def _boom(_items):
        raise RuntimeError("normalize should not be called")

    async def _fake_get_current_chat_provider_id(_umo):
        return "provider-id"

    async def _fake_tool_loop_agent(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(completion_text="ok")

    provider = MagicMock(spec=Provider)
    provider.provider_config = {"id": "provider-id"}

    context = SimpleNamespace(
        get_current_chat_provider_id=_fake_get_current_chat_provider_id,
        get_provider_by_id=lambda _provider_id: provider,
        tool_loop_agent=_fake_tool_loop_agent,
        get_config=lambda **_kwargs: {"provider_settings": {}},
    )
    event = _DummyEvent([])
    run_context = ContextWrapper(context=SimpleNamespace(event=event, context=context))
    tool = SimpleNamespace(
        name="transfer_to_subagent",
        provider_id=None,
        agent=SimpleNamespace(
            name="subagent",
            tools=[],
            instructions="subagent-instructions",
            begin_dialogs=[],
            run_hooks=None,
        ),
    )

    monkeypatch.setattr(
        "astrbot.core.astr_agent_tool_exec.normalize_and_dedupe_strings", _boom
    )

    results = []
    async for result in FunctionToolExecutor._execute_handoff(
        tool,
        run_context,
        image_urls_prepared=True,
        input="hello",
        image_urls=["https://example.com/raw.png"],
    ):
        results.append(result)

    assert len(results) == 1
    assert captured["image_urls"] == ["https://example.com/raw.png"]


@pytest.mark.asyncio
async def test_collect_handoff_image_urls_keeps_extensionless_existing_event_file(
    monkeypatch: pytest.MonkeyPatch,
):
    async def _fake_convert_to_file_path(self):
        return "/tmp/astrbot-handoff-image"

    monkeypatch.setattr(Image, "convert_to_file_path", _fake_convert_to_file_path)
    monkeypatch.setattr(
        "astrbot.core.astr_agent_tool_exec.get_astrbot_temp_path", lambda: "/tmp"
    )
    monkeypatch.setattr(
        "astrbot.core.utils.image_ref_utils.os.path.exists", lambda _: True
    )

    run_context = _build_run_context([Image(file="file:///tmp/original.png")])
    image_urls = await FunctionToolExecutor._collect_handoff_image_urls(
        run_context,
        [],
    )

    assert image_urls == ["/tmp/astrbot-handoff-image"]


@pytest.mark.asyncio
async def test_collect_handoff_image_urls_filters_extensionless_missing_event_file(
    monkeypatch: pytest.MonkeyPatch,
):
    async def _fake_convert_to_file_path(self):
        return "/tmp/astrbot-handoff-missing-image"

    monkeypatch.setattr(Image, "convert_to_file_path", _fake_convert_to_file_path)
    monkeypatch.setattr(
        "astrbot.core.astr_agent_tool_exec.get_astrbot_temp_path", lambda: "/tmp"
    )
    monkeypatch.setattr(
        "astrbot.core.utils.image_ref_utils.os.path.exists", lambda _: False
    )

    run_context = _build_run_context([Image(file="file:///tmp/original.png")])
    image_urls = await FunctionToolExecutor._collect_handoff_image_urls(
        run_context,
        [],
    )

    assert image_urls == []


@pytest.mark.asyncio
async def test_execute_handoff_passes_tool_call_timeout_to_tool_loop_agent(
    monkeypatch: pytest.MonkeyPatch,
):
    captured: dict = {}

    async def _fake_get_current_chat_provider_id(_umo):
        return "provider-id"

    async def _fake_tool_loop_agent(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(completion_text="ok")

    provider = MagicMock(spec=Provider)
    provider.provider_config = {"id": "provider-id"}

    context = SimpleNamespace(
        get_current_chat_provider_id=_fake_get_current_chat_provider_id,
        get_provider_by_id=lambda _provider_id: provider,
        tool_loop_agent=_fake_tool_loop_agent,
        get_config=lambda **_kwargs: {"provider_settings": {}},
    )
    event = _DummyEvent([])
    run_context = ContextWrapper(
        context=SimpleNamespace(event=event, context=context),
        tool_call_timeout=120,
    )
    tool = SimpleNamespace(
        name="transfer_to_subagent",
        provider_id=None,
        agent=SimpleNamespace(
            name="subagent",
            tools=[],
            instructions="subagent-instructions",
            begin_dialogs=[],
            run_hooks=None,
        ),
    )

    results = []
    async for result in FunctionToolExecutor._execute_handoff(
        tool,
        run_context,
        image_urls_prepared=True,
        input="hello",
        image_urls=[],
    ):
        results.append(result)

    assert len(results) == 1
    assert captured["tool_call_timeout"] == 120


@pytest.mark.asyncio
async def test_background_wakeup_passes_provider_settings_to_main_agent(
    monkeypatch: pytest.MonkeyPatch,
):
    provider_settings = {
        "fallback_chat_models": ["fallback-provider"],
        "request_max_retries": 3,
        "stream": True,
    }
    captured: dict = {}

    async def _fake_get_session_conv(**_kwargs):
        return SimpleNamespace(history="[]")

    async def _fake_build_main_agent(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(agent_runner=_DoneRunner())

    monkeypatch.setattr(
        "astrbot.core.astr_main_agent._get_session_conv",
        _fake_get_session_conv,
    )
    monkeypatch.setattr(
        "astrbot.core.astr_main_agent.build_main_agent",
        _fake_build_main_agent,
    )
    monkeypatch.setattr(
        "astrbot.core.astr_agent_tool_exec.persist_agent_history",
        AsyncMock(),
    )

    send_tool = FunctionTool(
        name="send_message_to_user",
        description="send",
        parameters={"type": "object", "properties": {}},
    )
    context = SimpleNamespace(
        get_config=lambda **_kwargs: {"provider_settings": provider_settings},
        get_llm_tool_manager=lambda: SimpleNamespace(
            get_builtin_tool=lambda _tool_cls: send_tool
        ),
        conversation_manager=SimpleNamespace(),
    )
    run_context = ContextWrapper(
        context=SimpleNamespace(event=_DummyEvent([]), context=context),
        tool_call_timeout=456,
    )

    await FunctionToolExecutor._wake_main_agent_for_background_result(
        run_context,
        task_id="task-id",
        tool_name="long_tool",
        result_text="ok",
        tool_args={},
        note="task finished",
        summary_name="BackgroundTask",
    )

    config = captured["config"]
    assert config.tool_call_timeout == 456
    assert config.streaming_response == provider_settings["stream"]
    assert config.provider_settings == provider_settings
    assert config.provider_settings["fallback_chat_models"] == ["fallback-provider"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "tool_provider_id",
        "selected_provider_id",
        "resolved_provider_id",
        "current_provider_id",
        "expect_fallback",
    ),
    [
        ("explicit-provider", None, "explicit-provider", "current-provider", False),
        (None, None, "current-provider", "current-provider", True),
    ],
)
async def test_execute_handoff_passes_fallback_providers_and_resolves_provider(
    monkeypatch: pytest.MonkeyPatch,
    tool_provider_id: str | None,
    selected_provider_id: str | None,
    resolved_provider_id: str,
    current_provider_id: str,
    expect_fallback: bool,
):
    captured: dict = {}
    fallback_helper_called = 0
    provider = MagicMock(spec=Provider)
    provider.provider_config = {"id": resolved_provider_id}
    fallback_provider = SimpleNamespace(provider_config={"id": "fallback-provider"})

    async def _fake_get_current_chat_provider_id(_umo):
        return current_provider_id

    def _fake_get_provider_by_id(provider_id):
        if provider_id == resolved_provider_id:
            return provider
        return None

    async def _fake_tool_loop_agent(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(completion_text="ok")

    def _fake_get_fallback_chat_providers(provider_arg, _ctx, provider_settings):
        nonlocal fallback_helper_called
        fallback_helper_called += 1
        assert provider_arg is provider
        assert provider_settings == {"fallback_chat_models": ["fallback-provider"]}
        return [fallback_provider]

    monkeypatch.setattr(
        "astrbot.core.astr_agent_tool_exec.get_fallback_chat_providers",
        _fake_get_fallback_chat_providers,
    )

    event = _DummyEvent(
        [],
        extras={"selected_provider": selected_provider_id}
        if selected_provider_id
        else {},
    )
    run_context = ContextWrapper(
        context=SimpleNamespace(
            event=event,
            context=SimpleNamespace(
                get_current_chat_provider_id=_fake_get_current_chat_provider_id,
                get_provider_by_id=_fake_get_provider_by_id,
                get_config=lambda **_kwargs: {
                    "provider_settings": {
                        "fallback_chat_models": ["fallback-provider"],
                    }
                },
                tool_loop_agent=_fake_tool_loop_agent,
            ),
        ),
        tool_call_timeout=120,
    )
    tool = SimpleNamespace(
        name="transfer_to_subagent",
        provider_id=tool_provider_id,
        agent=SimpleNamespace(
            name="subagent",
            tools=[],
            instructions="subagent-instructions",
            begin_dialogs=[],
            run_hooks=None,
        ),
    )

    results = []
    async for result in FunctionToolExecutor._execute_handoff(
        tool,
        run_context,
        image_urls_prepared=True,
        input="hello",
        image_urls=[],
    ):
        results.append(result)

    assert len(results) == 1
    assert captured["fallback_providers"] == (
        [fallback_provider] if expect_fallback else []
    )
    assert captured["chat_provider_id"] == resolved_provider_id
    assert captured["max_steps"] == 30
    assert captured["stream"] is False
    assert fallback_helper_called == (1 if expect_fallback else 0)


@pytest.mark.asyncio
async def test_execute_handoff_uses_runtime_provider_settings_for_fallback(
    monkeypatch: pytest.MonkeyPatch,
):
    captured: dict = {}
    provider = MagicMock(spec=Provider)
    provider.provider_config = {"id": "current-provider"}
    fallback_provider = SimpleNamespace(provider_config={"id": "runtime-fallback"})

    async def _fake_get_current_chat_provider_id(_umo):
        return "current-provider"

    def _fake_get_provider_by_id(provider_id):
        if provider_id == "current-provider":
            return provider
        return None

    async def _fake_tool_loop_agent(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(completion_text="ok")

    def _fake_get_fallback_chat_providers(provider_arg, _ctx, provider_settings):
        assert provider_arg is provider
        assert provider_settings == {
            "fallback_chat_models": ["runtime-fallback"],
            "max_agent_step": 7,
            "streaming_response": True,
        }
        return [fallback_provider]

    monkeypatch.setattr(
        "astrbot.core.astr_agent_tool_exec.get_fallback_chat_providers",
        _fake_get_fallback_chat_providers,
    )

    event = _DummyEvent([])
    run_context = ContextWrapper(
        context=SimpleNamespace(
            event=event,
            extra={
                "provider_settings": {
                    "fallback_chat_models": ["runtime-fallback"],
                    "max_agent_step": 7,
                    "streaming_response": True,
                }
            },
            context=SimpleNamespace(
                get_current_chat_provider_id=_fake_get_current_chat_provider_id,
                get_provider_by_id=_fake_get_provider_by_id,
                get_config=lambda **_kwargs: {
                    "provider_settings": {
                        "fallback_chat_models": ["global-fallback"],
                        "max_agent_step": 30,
                        "streaming_response": False,
                    }
                },
                tool_loop_agent=_fake_tool_loop_agent,
            ),
        ),
        tool_call_timeout=120,
    )
    tool = SimpleNamespace(
        name="transfer_to_subagent",
        provider_id=None,
        agent=SimpleNamespace(
            name="subagent",
            tools=[],
            instructions="subagent-instructions",
            begin_dialogs=[],
            run_hooks=None,
        ),
    )

    results = []
    async for result in FunctionToolExecutor._execute_handoff(
        tool,
        run_context,
        image_urls_prepared=True,
        input="hello",
        image_urls=[],
    ):
        results.append(result)

    assert len(results) == 1
    assert captured["fallback_providers"] == [fallback_provider]
    assert captured["max_steps"] == 7
    assert captured["stream"] is True


@pytest.mark.asyncio
async def test_collect_handoff_image_urls_filters_extensionless_file_outside_temp_root(
    monkeypatch: pytest.MonkeyPatch,
):
    async def _fake_convert_to_file_path(self):
        return "/var/tmp/astrbot-handoff-image"

    monkeypatch.setattr(Image, "convert_to_file_path", _fake_convert_to_file_path)
    monkeypatch.setattr(
        "astrbot.core.astr_agent_tool_exec.get_astrbot_temp_path", lambda: "/tmp"
    )
    monkeypatch.setattr(
        "astrbot.core.utils.image_ref_utils.os.path.exists", lambda _: True
    )

    run_context = _build_run_context([Image(file="file:///tmp/original.png")])
    image_urls = await FunctionToolExecutor._collect_handoff_image_urls(
        run_context,
        [],
    )

    assert image_urls == []
