import importlib
import importlib.util
import sqlite3
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PIL import Image as PILImage

import astrbot.api.message_components as Comp
from astrbot.api.event import MessageChain
from tests.fixtures.mocks.telegram import create_mock_telegram_modules

_PLUGIN_ROOT = Path("data/plugins/astrbot_plugin_airss").resolve()
_PLUGIN_DB = Path("data/plugin_data/astrbot_plugin_airss/rss.db").resolve()
_PLUGIN_PACKAGE = "tests_airss_plugin"
_TELEGRAM_EVENT_CLASS = None


def _load_airss_module(module_name: str):
    if _PLUGIN_PACKAGE not in sys.modules:
        pkg = types.ModuleType(_PLUGIN_PACKAGE)
        pkg.__path__ = [str(_PLUGIN_ROOT)]
        sys.modules[_PLUGIN_PACKAGE] = pkg

    full_name = f"{_PLUGIN_PACKAGE}.{module_name}"
    loaded = sys.modules.get(full_name)
    if loaded is not None:
        return loaded

    spec = importlib.util.spec_from_file_location(
        full_name,
        _PLUGIN_ROOT / f"{module_name}.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


def _build_telegram_patched_modules() -> dict[str, object]:
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


def _load_telegram_platform_event():
    global _TELEGRAM_EVENT_CLASS
    if _TELEGRAM_EVENT_CLASS is not None:
        return _TELEGRAM_EVENT_CLASS

    with patch.dict(sys.modules, _build_telegram_patched_modules()):
        sys.modules.pop("astrbot.core.platform.sources.telegram.tg_event", None)
        module = importlib.import_module("astrbot.core.platform.sources.telegram.tg_event")
    _TELEGRAM_EVENT_CLASS = module.TelegramPlatformEvent
    return _TELEGRAM_EVENT_CLASS


@pytest.mark.asyncio
async def test_airss_build_article_message_keeps_all_images_in_one_chain():
    scheduler_module = _load_airss_module("scheduler")
    models_module = _load_airss_module("models")

    scheduler = scheduler_module.RSSScheduler(
        context=MagicMock(),
        db=MagicMock(),
        fetcher=MagicMock(),
        config={"fetch_config": {"max_image_number": 0}},
    )

    article = models_module.RSSArticle(
        title="title",
        content="content",
        link="https://example.com/article",
        image_urls=[
            "https://example.com/1.jpg",
            "https://example.com/2.jpg",
            "https://example.com/3.jpg",
        ],
    )
    subscription = models_module.RSSSubscription(name="feed")

    chain = scheduler._build_article_message(article, subscription)

    assert isinstance(chain, MessageChain)
    assert any(isinstance(component, Comp.Plain) for component in chain.chain)
    assert sum(isinstance(component, Comp.Image) for component in chain.chain) == 3
    assert not any(isinstance(component, Comp.File) for component in chain.chain)


@pytest.mark.asyncio
async def test_telegram_album_with_gif_content_is_sent_as_media_group(tmp_path):
    TelegramPlatformEvent = _load_telegram_platform_event()

    jpg_path_a = tmp_path / "photo_a.jpg"
    jpg_path_b = tmp_path / "photo_b.jpg"
    gif_path = tmp_path / "anim.gif"
    PILImage.new("RGB", (8, 8), color=(255, 0, 0)).save(jpg_path_a, format="JPEG")
    PILImage.new("RGB", (8, 8), color=(255, 255, 0)).save(jpg_path_b, format="JPEG")
    PILImage.new("RGB", (8, 8), color=(0, 255, 0)).save(gif_path, format="GIF")

    message = MessageChain().message("album")
    message.chain.append(Comp.Image.fromURL("https://example.com/photo_a.jpg"))
    message.chain.append(Comp.Image.fromURL("https://example.com/photo_b.jpg"))
    message.chain.append(Comp.Image.fromURL("https://example.com/anim.gif"))

    client = MagicMock()
    client.send_chat_action = AsyncMock()
    client.send_media_group = AsyncMock()
    client.send_photo = AsyncMock()
    client.send_animation = AsyncMock()
    client.send_message = AsyncMock()

    with (
        patch.object(
            Comp.Image,
            "convert_to_file_path",
            AsyncMock(side_effect=[str(jpg_path_a), str(jpg_path_b), str(gif_path)]),
        )
    ):
        await TelegramPlatformEvent.send_with_client(client, message, "123456")

    assert client.send_media_group.await_count == 1
    assert client.send_animation.await_count == 1
    assert client.send_photo.await_count == 0
    media_group_payload = client.send_media_group.await_args_list[0].kwargs
    assert len(media_group_payload["media"]) == 2


@pytest.mark.asyncio
@pytest.mark.skipif(not _PLUGIN_DB.exists(), reason="airss runtime db not found")
async def test_airss_real_db_article_mixed_images_no_attachment_default(tmp_path):
    scheduler_module = _load_airss_module("scheduler")
    models_module = _load_airss_module("models")
    TelegramPlatformEvent = _load_telegram_platform_event()

    with sqlite3.connect(_PLUGIN_DB) as conn:
        row = conn.execute(
            """
            SELECT id, title, content, link, image_urls
            FROM articles
            WHERE image_urls IS NOT NULL
              AND image_urls != ''
              AND lower(image_urls) LIKE '%gif%'
              AND image_urls LIKE '%|||%'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

    if row is None:
        pytest.skip("no mixed gif/photo article found in airss db")

    _, title, content, link, image_urls = row
    urls = [u for u in (image_urls or "").split("|||") if u.strip()][:5]
    if len(urls) < 2:
        pytest.skip("not enough image urls in selected article")

    scheduler = scheduler_module.RSSScheduler(
        context=MagicMock(),
        db=MagicMock(),
        fetcher=MagicMock(),
        config={"fetch_config": {"max_image_number": 0}},
    )
    article = models_module.RSSArticle(
        title=title or "title",
        content=(content or "")[:200],
        link=link or "https://example.com/article",
        image_urls=urls,
    )
    subscription = models_module.RSSSubscription(name="feed")
    message = scheduler._build_article_message(article, subscription)

    assert (
        sum(isinstance(component, Comp.Image) for component in message.chain)
        == len(urls)
    )
    assert not any(isinstance(component, Comp.File) for component in message.chain)

    local_paths: list[str] = []
    for idx, url in enumerate(urls):
        lower = url.lower()
        if lower.endswith(".gif"):
            path = tmp_path / f"sample_{idx}.gif"
            PILImage.new("RGB", (8, 8), color=(0, 255, 0)).save(path, format="GIF")
        elif lower.endswith(".png"):
            path = tmp_path / f"sample_{idx}.png"
            PILImage.new("RGB", (8, 8), color=(0, 0, 255)).save(path, format="PNG")
        else:
            path = tmp_path / f"sample_{idx}.jpg"
            PILImage.new("RGB", (8, 8), color=(255, 0, 0)).save(path, format="JPEG")
        local_paths.append(str(path))

    client = MagicMock()
    client.send_chat_action = AsyncMock()
    client.send_media_group = AsyncMock()
    client.send_photo = AsyncMock()
    client.send_animation = AsyncMock()
    client.send_document = AsyncMock()
    client.send_message = AsyncMock()

    with patch.object(
        Comp.Image,
        "convert_to_file_path",
        AsyncMock(side_effect=local_paths),
    ):
        await TelegramPlatformEvent.send_with_client(client, message, "123456")

    non_gif_count = sum(not path.lower().endswith(".gif") for path in local_paths)
    gif_count = sum(path.lower().endswith(".gif") for path in local_paths)

    if non_gif_count >= 2:
        assert client.send_media_group.await_count >= 1
    if gif_count >= 1:
        assert client.send_animation.await_count >= 1
    assert client.send_document.await_count == 0
