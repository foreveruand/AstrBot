import asyncio
import importlib
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import astrbot.api.message_components as Comp
from astrbot.api.event import MessageChain
from tests.fixtures.helpers import (
    NoopAwaitable,
    create_mock_file,
    create_mock_update,
    make_platform_config,
)
from tests.fixtures.mocks.telegram import (
    MockTelegramBuilder,
    MockTelegramNetworkError,
    create_mock_telegram_modules,
)

_TELEGRAM_PLATFORM_ADAPTER = None
_TELEGRAM_PLATFORM_EVENT = None
_TELEGRAM_MODULES: dict[str, object] = {}


def _build_telegram_patched_modules():
    mocks = create_mock_telegram_modules()
    return {
        "telegram": mocks["telegram"],
        "telegram.constants": mocks["telegram"].constants,
        "telegram.error": mocks["telegram"].error,
        "telegram.ext": mocks["telegram.ext"],
        "telegramify_markdown": mocks["telegramify_markdown"],
        "apscheduler": mocks["apscheduler"],
        "apscheduler.schedulers": mocks["apscheduler"].schedulers,
        "apscheduler.schedulers.asyncio": mocks["apscheduler"].schedulers.asyncio,
        "apscheduler.schedulers.background": mocks["apscheduler"].schedulers.background,
    }


def _load_telegram_module(module_name: str):
    module = _TELEGRAM_MODULES.get(module_name)
    if module is not None:
        return module

    with patch.dict(sys.modules, _build_telegram_patched_modules()):
        sys.modules.pop(module_name, None)
        module = importlib.import_module(module_name)

    sys.modules[module_name] = module
    _TELEGRAM_MODULES[module_name] = module
    return module


def _load_telegram_adapter():
    global _TELEGRAM_PLATFORM_ADAPTER
    if _TELEGRAM_PLATFORM_ADAPTER is not None:
        return _TELEGRAM_PLATFORM_ADAPTER

    module = _load_telegram_module("astrbot.core.platform.sources.telegram.tg_adapter")
    _TELEGRAM_PLATFORM_ADAPTER = module.TelegramPlatformAdapter
    return _TELEGRAM_PLATFORM_ADAPTER


def _load_telegram_platform_event():
    global _TELEGRAM_PLATFORM_EVENT
    if _TELEGRAM_PLATFORM_EVENT is not None:
        return _TELEGRAM_PLATFORM_EVENT

    module = _load_telegram_module("astrbot.core.platform.sources.telegram.tg_event")
    _TELEGRAM_PLATFORM_EVENT = module.TelegramPlatformEvent
    return _TELEGRAM_PLATFORM_EVENT


def _build_context() -> MagicMock:
    context = MagicMock()
    context.bot.username = "test_bot"
    context.bot.id = 12345678
    return context


@pytest.mark.asyncio
async def test_telegram_document_caption_populates_message_text_and_plain():
    TelegramPlatformAdapter = _load_telegram_adapter()
    adapter = TelegramPlatformAdapter(
        make_platform_config("telegram"),
        {},
        asyncio.Queue(),
    )
    document = create_mock_file("https://api.telegram.org/file/test/report.md")
    document.file_name = "report.md"
    mention = MagicMock(type="mention", offset=0, length=6)
    update = create_mock_update(
        message_text=None,
        document=document,
        caption="@alice 请总结这份文档",
        caption_entities=[mention],
    )

    result = await adapter.convert_message(update, _build_context())

    assert result is not None
    assert result.message_str == "@alice 请总结这份文档"
    assert any(isinstance(component, Comp.File) for component in result.message)
    assert any(
        isinstance(component, Comp.Plain) and component.text == "@alice 请总结这份文档"
        for component in result.message
    )
    assert any(
        isinstance(component, Comp.At) and component.qq == "alice"
        for component in result.message
    )


@pytest.mark.asyncio
async def test_telegram_video_caption_populates_message_text_and_plain():
    TelegramPlatformAdapter = _load_telegram_adapter()
    adapter = TelegramPlatformAdapter(
        make_platform_config("telegram"),
        {},
        asyncio.Queue(),
    )
    video = create_mock_file("https://api.telegram.org/file/test/lesson.mp4")
    video.file_name = "lesson.mp4"
    update = create_mock_update(
        message_text=None,
        video=video,
        caption="这段视频讲了什么",
    )

    result = await adapter.convert_message(update, _build_context())

    assert result is not None
    assert result.message_str == "这段视频讲了什么"
    assert any(isinstance(component, Comp.Video) for component in result.message)
    assert any(
        isinstance(component, Comp.Plain) and component.text == "这段视频讲了什么"
        for component in result.message
    )


@pytest.mark.asyncio
async def test_telegram_voice_message_creates_record_component(tmp_path):
    TelegramPlatformAdapter = _load_telegram_adapter()
    adapter = TelegramPlatformAdapter(
        make_platform_config("telegram"),
        {},
        asyncio.Queue(),
    )
    voice = create_mock_file("https://api.telegram.org/file/test/voice.oga")
    update = create_mock_update(
        message_text=None,
        voice=voice,
    )
    wav_path = tmp_path / "voice.oga.wav"
    convert_message_globals = adapter.convert_message.__func__.__globals__

    with patch.dict(
        convert_message_globals,
        {
            "get_astrbot_temp_path": MagicMock(return_value=str(tmp_path)),
            "download_file": AsyncMock(),
            "convert_audio_to_wav": AsyncMock(return_value=str(wav_path)),
        },
    ):
        result = await adapter.convert_message(update, _build_context())

    assert result is not None
    assert len(result.message) == 1
    assert isinstance(result.message[0], Comp.Record)
    assert result.message[0].file == str(wav_path)
    assert result.message[0].path == str(wav_path)
    assert result.message[0].url == str(wav_path)


@pytest.mark.asyncio
async def test_telegram_final_segment_splits_long_markdown_messages():
    TelegramPlatformEvent = _load_telegram_platform_event()
    client = MagicMock()
    client.send_message = AsyncMock()
    event = TelegramPlatformEvent("msg", MagicMock(), MagicMock(), "session", client)

    delta = "A" * (TelegramPlatformEvent.MAX_MESSAGE_LENGTH + 32)
    payload = {"chat_id": "123456"}

    await event._send_final_segment(delta, payload)

    assert client.send_message.await_count == 2
    first_call = client.send_message.await_args_list[0].kwargs
    second_call = client.send_message.await_args_list[1].kwargs
    assert len(first_call["text"]) == TelegramPlatformEvent.MAX_MESSAGE_LENGTH
    assert len(second_call["text"]) == 32
    assert first_call["parse_mode"] == "MarkdownV2"
    assert second_call["parse_mode"] == "MarkdownV2"


@pytest.mark.asyncio
async def test_telegram_final_segment_splits_long_plaintext_when_markdown_fails():
    TelegramPlatformEvent = _load_telegram_platform_event()
    client = MagicMock()
    client.send_message = AsyncMock()
    event = TelegramPlatformEvent("msg", MagicMock(), MagicMock(), "session", client)

    delta = "B" * (TelegramPlatformEvent.MAX_MESSAGE_LENGTH + 18)
    payload = {"chat_id": "123456"}

    with patch(
        "astrbot.core.platform.sources.telegram.tg_event.telegramify_markdown.markdownify",
        side_effect=Exception("boom"),
    ):
        await event._send_final_segment(delta, payload)

    assert client.send_message.await_count == 2
    first_call = client.send_message.await_args_list[0].kwargs
    second_call = client.send_message.await_args_list[1].kwargs
    assert len(first_call["text"]) == TelegramPlatformEvent.MAX_MESSAGE_LENGTH
    assert len(second_call["text"]) == 18
    assert "parse_mode" not in first_call
    assert "parse_mode" not in second_call


@pytest.mark.asyncio
async def test_telegram_send_with_client_batches_images_into_media_groups():
    TelegramPlatformEvent = _load_telegram_platform_event()
    client = MagicMock()
    client.send_chat_action = AsyncMock()
    client.send_media_group = AsyncMock()
    client.send_photo = AsyncMock()
    client.send_animation = AsyncMock()
    client.send_message = AsyncMock()

    message = MessageChain().message("A" * 1100)
    for idx in range(11):
        img = Comp.Image.fromURL(f"https://example.com/{idx}.jpg")
        message.chain.append(img)

    image_paths = [f"/tmp/{idx}.jpg" for idx in range(11)]
    with (
        patch.object(
            Comp.Image,
            "convert_to_file_path",
            AsyncMock(side_effect=image_paths),
        ),
        patch(
            "astrbot.core.platform.sources.telegram.tg_event.InputMediaPhoto",
            new=MagicMock(),
        ) as input_media_photo,
    ):
        await TelegramPlatformEvent.send_with_client(client, message, "123456")

        assert client.send_media_group.await_count == 2
        assert client.send_photo.await_count == 0

        first_group = client.send_media_group.await_args_list[0].kwargs
        second_group = client.send_media_group.await_args_list[1].kwargs
        assert len(first_group["media"]) == 10
        assert len(second_group["media"]) == 1
        assert "caption" not in first_group
        assert "parse_mode" not in first_group
        assert "caption" not in second_group

        assert input_media_photo.call_count == 11
        first_photo_kwargs = input_media_photo.call_args_list[0].kwargs
        second_batch_first_photo_kwargs = input_media_photo.call_args_list[10].kwargs
        assert len(first_photo_kwargs["caption"]) == 1024
        assert first_photo_kwargs["parse_mode"] == "MarkdownV2"
        assert "caption" not in second_batch_first_photo_kwargs

        assert client.send_message.await_count == 1
        followup = client.send_message.await_args_list[0].kwargs
        assert len(followup["text"]) == 76
        assert followup["parse_mode"] == "MarkdownV2"


@pytest.mark.asyncio
async def test_telegram_send_with_client_single_image_keeps_send_photo():
    TelegramPlatformEvent = _load_telegram_platform_event()
    client = MagicMock()
    client.send_chat_action = AsyncMock()
    client.send_media_group = AsyncMock()
    client.send_photo = AsyncMock()
    client.send_animation = AsyncMock()
    client.send_message = AsyncMock()

    message = MessageChain().message("hello")
    img = Comp.Image.fromURL("https://example.com/single.jpg")
    message.chain.append(img)

    with patch.object(
        Comp.Image,
        "convert_to_file_path",
        AsyncMock(return_value="/tmp/single.jpg"),
    ):
        await TelegramPlatformEvent.send_with_client(client, message, "123456")

    assert client.send_photo.await_count == 1
    assert client.send_media_group.await_count == 0
    assert client.send_message.await_count == 0


@pytest.mark.asyncio
async def test_telegram_send_with_client_fallbacks_to_single_photo_on_media_group_error():
    TelegramPlatformEvent = _load_telegram_platform_event()
    client = MagicMock()
    client.send_chat_action = AsyncMock()
    client.send_media_group = AsyncMock(side_effect=Exception("media group failed"))
    client.send_photo = AsyncMock()
    client.send_animation = AsyncMock()
    client.send_message = AsyncMock()

    message = MessageChain().message("hello")
    for idx in range(2):
        img = Comp.Image.fromURL(f"https://example.com/{idx}.jpg")
        message.chain.append(img)

    image_paths = ["/tmp/0.jpg", "/tmp/1.jpg"]
    with (
        patch.object(
            Comp.Image,
            "convert_to_file_path",
            AsyncMock(side_effect=image_paths),
        ),
        patch(
            "astrbot.core.platform.sources.telegram.tg_event.InputMediaPhoto",
            new=MagicMock(),
        ),
    ):
        await TelegramPlatformEvent.send_with_client(client, message, "123456")

    assert client.send_media_group.await_count == 1
    assert client.send_photo.await_count == 2


@pytest.mark.asyncio
async def test_telegram_polling_error_requests_rebuild_after_threshold():
    TelegramPlatformAdapter = _load_telegram_adapter()
    adapter = TelegramPlatformAdapter(
        make_platform_config("telegram"),
        {},
        asyncio.Queue(),
    )
    adapter._loop = asyncio.get_running_loop()

    assert not adapter._polling_recovery_requested.is_set()

    for _ in range(adapter._polling_recovery_threshold):
        adapter._on_polling_error(MockTelegramNetworkError("proxy disconnected"))

    await asyncio.sleep(0)

    assert adapter._polling_recovery_requested.is_set()


@pytest.mark.asyncio
async def test_telegram_run_rebuilds_application_after_repeated_polling_errors():
    TelegramPlatformAdapter = _load_telegram_adapter()
    module_globals = TelegramPlatformAdapter.__init__.__globals__
    app_one = MockTelegramBuilder.create_application()
    app_one.updater.running = True
    app_two = MockTelegramBuilder.create_application()
    app_two.updater.running = True
    created_apps = [app_one, app_two]

    builder = MagicMock()
    builder.token.return_value = builder
    builder.base_url.return_value = builder
    builder.base_file_url.return_value = builder
    builder.build.side_effect = created_apps

    adapter = None

    def start_polling_side_effect(*args, **kwargs):
        nonlocal adapter
        error_callback = kwargs["error_callback"]
        assert adapter is not None

        async def _emit_errors():
            await asyncio.sleep(0)
            for _ in range(adapter._polling_recovery_threshold):
                error_callback(MockTelegramNetworkError("proxy disconnected"))

        asyncio.create_task(_emit_errors())
        return NoopAwaitable()

    app_one.updater.start_polling.side_effect = start_polling_side_effect

    async def second_start_polling(*args, **kwargs):
        assert adapter is not None
        adapter._terminating = True

    app_two.updater.start_polling.side_effect = second_start_polling

    with patch.dict(
        module_globals,
        {
            "ApplicationBuilder": MagicMock(return_value=builder),
            "AsyncIOScheduler": MagicMock(
                return_value=MockTelegramBuilder.create_scheduler()
            ),
        },
    ):
        adapter = TelegramPlatformAdapter(
            make_platform_config("telegram"),
            {},
            asyncio.Queue(),
        )
        await adapter.run()

    assert builder.build.call_count == 2
    app_one.updater.stop.assert_awaited()
    app_one.bot.delete_my_commands.assert_not_awaited()
    app_one.stop.assert_awaited()
    app_one.shutdown.assert_awaited()
    app_two.initialize.assert_awaited()
    app_two.start.assert_awaited()


@pytest.mark.asyncio
async def test_telegram_recreate_application_is_skipped_during_termination():
    TelegramPlatformAdapter = _load_telegram_adapter()
    adapter = TelegramPlatformAdapter(
        make_platform_config("telegram"),
        {},
        asyncio.Queue(),
    )
    adapter._terminating = True
    adapter._polling_recovery_requested.set()

    await adapter._recreate_application()

    assert not adapter._polling_recovery_requested.is_set()


@pytest.mark.asyncio
async def test_telegram_run_rebuilds_fresh_application_after_recreate_init_failure():
    TelegramPlatformAdapter = _load_telegram_adapter()
    module_globals = TelegramPlatformAdapter.__init__.__globals__
    app_one = MockTelegramBuilder.create_application()
    app_one.updater.running = True
    app_two = MockTelegramBuilder.create_application()
    app_three = MockTelegramBuilder.create_application()
    app_three.updater.running = True
    created_apps = [app_one, app_two, app_three]

    builder = MagicMock()
    builder.token.return_value = builder
    builder.base_url.return_value = builder
    builder.base_file_url.return_value = builder
    builder.build.side_effect = created_apps

    adapter = None

    def first_start_polling(*args, **kwargs):
        nonlocal adapter
        error_callback = kwargs["error_callback"]
        assert adapter is not None

        async def _emit_errors():
            await asyncio.sleep(0)
            for _ in range(adapter._polling_recovery_threshold):
                error_callback(MockTelegramNetworkError("proxy disconnected"))

        asyncio.create_task(_emit_errors())
        return NoopAwaitable()

    app_one.updater.start_polling.side_effect = first_start_polling
    app_two.initialize.side_effect = TimeoutError("init timeout")

    async def final_start_polling(*args, **kwargs):
        assert adapter is not None
        adapter._terminating = True

    app_three.updater.start_polling.side_effect = final_start_polling

    with patch.dict(
        module_globals,
        {
            "ApplicationBuilder": MagicMock(return_value=builder),
            "AsyncIOScheduler": MagicMock(
                return_value=MockTelegramBuilder.create_scheduler()
            ),
        },
    ):
        adapter = TelegramPlatformAdapter(
            make_platform_config(
                "telegram",
                telegram_polling_restart_delay=0.1,
            ),
            {},
            asyncio.Queue(),
        )
        await adapter.run()

    assert builder.build.call_count == 3
    app_two.stop.assert_awaited()
    app_two.shutdown.assert_awaited()
    app_three.initialize.assert_awaited()
    app_three.start.assert_awaited()
