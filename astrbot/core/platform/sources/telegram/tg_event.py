import asyncio
import hashlib
import inspect
import os
import re
from collections.abc import Callable
from typing import Any, cast

import telegramify_markdown
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputMediaPhoto,
    InputTextMessageContent,
    ReactionTypeCustomEmoji,
    ReactionTypeEmoji,
)
from telegram.constants import ChatAction
from telegram.error import BadRequest
from telegram.ext import ExtBot

from astrbot import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import (
    At,
    File,
    Image,
    Plain,
    Record,
    Reply,
    Video,
)
from astrbot.api.platform import AstrBotMessage, MessageType, PlatformMetadata
from astrbot.core.platform.astrbot_message import MessageMember
from astrbot.core.platform.sources.telegram.inline_registry import (
    TELEGRAM_INLINE_STOP_CALLBACK_DATA,
    telegram_inline_stop_registry,
)
from astrbot.core.utils.metrics import Metric


def _is_gif(path: str) -> bool:
    if path.lower().endswith(".gif"):
        return True
    try:
        with open(path, "rb") as f:
            return f.read(6) in (b"GIF87a", b"GIF89a")
    except OSError:
        return False


class TelegramPlatformEvent(AstrMessageEvent):
    # Telegram 的最大消息长度限制
    MAX_MESSAGE_LENGTH = 4096
    MAX_CAPTION_LENGTH = 1024
    MAX_MEDIA_GROUP_SIZE = 10

    SPLIT_PATTERNS = {
        "paragraph": re.compile(r"\n\n"),
        "line": re.compile(r"\n"),
        "sentence": re.compile(r"[.!?。！？]"),
        "word": re.compile(r"\s"),
    }

    # sendMessageDraft 的 draft_id 类级递增计数器
    _TELEGRAM_DRAFT_ID_MAX = 2_147_483_647
    _next_draft_id: int = 0

    @classmethod
    def _allocate_draft_id(cls) -> int:
        """分配一个递增的 draft_id，溢出时归 1。"""
        cls._next_draft_id = (
            1
            if cls._next_draft_id >= cls._TELEGRAM_DRAFT_ID_MAX
            else cls._next_draft_id + 1
        )
        return cls._next_draft_id

    # 消息类型到 chat action 的映射，用于优先级判断
    ACTION_BY_TYPE: dict[type, str] = {
        Record: ChatAction.UPLOAD_VOICE,
        Video: ChatAction.UPLOAD_VIDEO,
        File: ChatAction.UPLOAD_DOCUMENT,
        Image: ChatAction.UPLOAD_PHOTO,
        Plain: ChatAction.TYPING,
    }

    def __init__(
        self,
        message_str: str,
        message_obj: AstrBotMessage,
        platform_meta: PlatformMetadata,
        session_id: str,
        client: ExtBot,
    ) -> None:
        super().__init__(message_str, message_obj, platform_meta, session_id)
        self.client = client
        self._tool_call_status_chat_id: str | None = None
        self._tool_call_status_message_id: int | None = None
        self._tool_call_status_thread_id: str | None = None

    @staticmethod
    def _utf16_length(text: str) -> int:
        """Return text length in Telegram's UTF-16 code units.

        Args:
            text: Text to measure.

        Returns:
            The number of UTF-16 code units in ``text``.
        """
        return len(text.encode("utf-16-le")) // 2

    @classmethod
    def _split_message(cls, text: str, max_length: int | None = None) -> list[str]:
        limit = max_length or cls.MAX_MESSAGE_LENGTH
        if cls._utf16_length(text) <= limit:
            return [text]

        chunks = []
        while text:
            if cls._utf16_length(text) <= limit:
                chunks.append(text)
                break

            split_point = 0
            utf16_length = 0
            for index, char in enumerate(text):
                char_length = 2 if ord(char) > 0xFFFF else 1
                if utf16_length + char_length > limit:
                    break
                utf16_length += char_length
                split_point = index + 1

            if split_point == 0:
                split_point = 1
            segment = text[:split_point]

            for _, pattern in cls.SPLIT_PATTERNS.items():
                if matches := list(pattern.finditer(segment)):
                    last_match = matches[-1]
                    split_point = last_match.end()
                    break

            chunks.append(text[:split_point])
            text = text[split_point:].lstrip()

        return chunks

    @classmethod
    def _prepare_text_chunks(
        cls, text: str, max_length: int | None = None
    ) -> list[tuple[str, str | None]]:
        """Convert Markdown and split it without breaking Telegram entities.

        Args:
            text: Markdown source text.
            max_length: Maximum rendered UTF-16 length of each chunk.

        Returns:
            A list of ``(formatted_text, plain_text)`` pairs. ``plain_text`` is
            available as a fallback when Telegram rejects the formatted chunk;
            it is ``None`` when Markdown conversion itself failed.
        """
        if not text:
            return []

        limit = max_length or cls.MAX_MESSAGE_LENGTH
        try:
            plain_text, entities = telegramify_markdown.convert(text)
            entity_chunks = telegramify_markdown.split_entities(
                plain_text,
                entities,
                limit,
            )
            return [
                (
                    telegramify_markdown.entities_to_markdownv2(
                        chunk_text,
                        chunk_entities,
                    ),
                    chunk_text,
                )
                for chunk_text, chunk_entities in entity_chunks
            ]
        except Exception as e:
            logger.warning(
                f"Telegram Markdown conversion failed; using plain text: {e!s}"
            )
            return [(chunk, None) for chunk in cls._split_message(text, limit)]

    @classmethod
    async def _send_prepared_text_chunks(
        cls,
        client: ExtBot,
        chunks: list[tuple[str, str | None]],
        payload: dict[str, Any],
    ) -> None:
        """Send prepared chunks with MarkdownV2 and plain-text fallback.

        Args:
            client: Telegram bot client.
            chunks: Prepared ``(formatted_text, plain_text)`` pairs.
            payload: Common Telegram message parameters.
        """
        for formatted_text, plain_text in chunks:
            if not formatted_text:
                continue
            try:
                if plain_text is None:
                    await client.send_message(
                        text=formatted_text,
                        **cast(Any, payload),
                    )
                else:
                    await client.send_message(
                        text=formatted_text,
                        parse_mode="MarkdownV2",
                        **cast(Any, payload),
                    )
            except (ValueError, BadRequest) as e:
                logger.warning(
                    f"Telegram MarkdownV2 send failed; using plain text: {e!s}"
                )
                await client.send_message(
                    text=plain_text or formatted_text,
                    **cast(Any, payload),
                )

    @classmethod
    async def _send_text_chunks(
        cls,
        client: ExtBot,
        text: str,
        payload: dict[str, Any],
        max_length: int | None = None,
    ) -> None:
        """Split and send text while preserving Markdown entity boundaries.

        Args:
            client: Telegram bot client.
            text: Markdown source text.
            payload: Common Telegram message parameters.
            max_length: Maximum rendered UTF-16 length of each chunk.
        """
        await cls._send_prepared_text_chunks(
            client,
            cls._prepare_text_chunks(text, max_length),
            payload,
        )

    @classmethod
    async def _send_chat_action(
        cls,
        client: ExtBot,
        chat_id: str,
        action: ChatAction | str,
        message_thread_id: str | None = None,
    ) -> None:
        """发送聊天状态动作"""
        try:
            payload: dict[str, Any] = {"chat_id": chat_id, "action": action}
            if message_thread_id:
                payload["message_thread_id"] = message_thread_id
            await client.send_chat_action(**payload)
        except Exception as e:
            logger.warning(f"[Telegram] 发送 chat action 失败: {e}")

    @classmethod
    def _get_chat_action_for_chain(cls, chain: list[Any]) -> ChatAction | str:
        """根据消息链中的组件类型确定合适的 chat action（按优先级）"""
        for seg_type, action in cls.ACTION_BY_TYPE.items():
            if any(isinstance(seg, seg_type) for seg in chain):
                return action
        return ChatAction.TYPING

    @classmethod
    async def _send_media_with_action(
        cls,
        client: ExtBot,
        upload_action: ChatAction | str,
        send_coro,
        *,
        user_name: str,
        message_thread_id: str | None = None,
        **payload: Any,
    ) -> None:
        """发送媒体时显示 upload action，发送完成后恢复 typing"""
        effective_thread_id = message_thread_id or cast(
            str | None, payload.get("message_thread_id")
        )
        await cls._send_chat_action(
            client, user_name, upload_action, effective_thread_id
        )
        send_payload = dict(payload)
        if effective_thread_id and "message_thread_id" not in send_payload:
            send_payload["message_thread_id"] = effective_thread_id
        await send_coro(**send_payload)
        await cls._send_chat_action(
            client, user_name, ChatAction.TYPING, effective_thread_id
        )

    @classmethod
    async def _send_media_with_caption_fallback(
        cls,
        send_coro: Callable[..., Any],
        payload: dict[str, Any],
        caption_fallback: str | None,
    ) -> None:
        """Retry a formatted media caption as plain text after a parse failure.

        Args:
            send_coro: Telegram media send coroutine.
            payload: Media send parameters, including the optional caption.
            caption_fallback: Plain caption to use when MarkdownV2 is rejected.

        Raises:
            ValueError: If Telegram rejects the formatted request and no plain
                caption fallback is available.
            BadRequest: If Telegram rejects both the formatted and plain requests.
        """
        try:
            await send_coro(**cast(Any, payload))
        except (ValueError, BadRequest) as e:
            if caption_fallback is None or payload.get("parse_mode") != "MarkdownV2":
                raise
            logger.warning(
                f"Telegram media caption parsing failed; retrying as plain text: {e!s}"
            )
            fallback_payload = dict(payload)
            fallback_payload.pop("parse_mode", None)
            fallback_payload["caption"] = caption_fallback
            await send_coro(**cast(Any, fallback_payload))

    @classmethod
    async def _send_voice_with_fallback(
        cls,
        client: ExtBot,
        path: str,
        payload: dict[str, Any],
        *,
        caption: str | None = None,
        user_name: str = "",
        message_thread_id: str | None = None,
        use_media_action: bool = False,
    ) -> None:
        """Send a voice message, falling back to a document if the user's
        privacy settings forbid voice messages (``BadRequest`` with
        ``Voice_messages_forbidden``).

        When *use_media_action* is ``True`` the helper wraps the send calls
        with ``_send_media_with_action`` (used by the streaming path).
        """
        try:
            if use_media_action:
                media_payload = dict(payload)
                if message_thread_id and "message_thread_id" not in media_payload:
                    media_payload["message_thread_id"] = message_thread_id
                await cls._send_media_with_action(
                    client,
                    ChatAction.UPLOAD_VOICE,
                    client.send_voice,
                    user_name=user_name,
                    voice=path,
                    **cast(Any, media_payload),
                )
            else:
                await client.send_voice(voice=path, **cast(Any, payload))
        except BadRequest as e:
            # python-telegram-bot raises BadRequest for Voice_messages_forbidden;
            # distinguish the voice-privacy case via the API error message.
            if "Voice_messages_forbidden" not in e.message:
                raise
            logger.warning(
                "User privacy settings prevent receiving voice messages, falling back to sending an audio file. "
                "To enable voice messages, go to Telegram Settings → Privacy and Security → Voice Messages → set to 'Everyone'."
            )
            if use_media_action:
                media_payload = dict(payload)
                if message_thread_id and "message_thread_id" not in media_payload:
                    media_payload["message_thread_id"] = message_thread_id
                await cls._send_media_with_action(
                    client,
                    ChatAction.UPLOAD_DOCUMENT,
                    client.send_document,
                    user_name=user_name,
                    document=path,
                    caption=caption,
                    **cast(Any, media_payload),
                )
            else:
                await client.send_document(
                    document=path,
                    caption=caption,
                    **cast(Any, payload),
                )

    @classmethod
    def _is_photo_recoverable_error(cls, error: BadRequest) -> bool:
        message = f"{getattr(error, 'message', '')} {error!s}".lower()
        return (
            "photo_invalid_dimensions" in message
            or "too big for a photo" in message
            or "too big to send as a photo" in message
        )

    @classmethod
    async def _send_photo_with_document_fallback(
        cls,
        client: ExtBot,
        path: str,
        payload: dict[str, Any],
        photo_kwargs: dict[str, Any],
        *,
        user_name: str = "",
        message_thread_id: str | None = None,
        use_media_action: bool = False,
        caption_fallback: str | None = None,
    ) -> None:
        """Send a Telegram photo with caption and document fallbacks.

        Args:
            client: Telegram bot client.
            path: Local photo path used for document fallback logging.
            payload: Common Telegram request parameters.
            photo_kwargs: Photo request parameters.
            user_name: Target user or group ID used for chat actions.
            message_thread_id: Optional Telegram topic ID.
            use_media_action: Whether to show upload actions around the send.
            caption_fallback: Plain caption used when MarkdownV2 is rejected.
        """
        try:
            if use_media_action:
                await cls._send_media_with_action(
                    client,
                    ChatAction.UPLOAD_PHOTO,
                    client.send_photo,
                    user_name=user_name,
                    message_thread_id=message_thread_id,
                    **photo_kwargs,
                    **cast(Any, payload),
                )
            else:
                await client.send_photo(**photo_kwargs, **cast(Any, payload))
            return
        except BadRequest as e:
            if (
                caption_fallback is not None
                and payload.get("parse_mode") == "MarkdownV2"
                and not cls._is_photo_recoverable_error(e)
            ):
                fallback_payload = dict(payload)
                fallback_payload.pop("parse_mode", None)
                fallback_photo_kwargs = dict(photo_kwargs)
                fallback_photo_kwargs["caption"] = caption_fallback
                logger.warning(
                    f"Telegram photo caption parsing failed; retrying as plain text: {e!s}"
                )
                try:
                    if use_media_action:
                        await cls._send_media_with_action(
                            client,
                            ChatAction.UPLOAD_PHOTO,
                            client.send_photo,
                            user_name=user_name,
                            message_thread_id=message_thread_id,
                            **fallback_photo_kwargs,
                            **cast(Any, fallback_payload),
                        )
                    else:
                        await client.send_photo(
                            **fallback_photo_kwargs,
                            **cast(Any, fallback_payload),
                        )
                    return
                except BadRequest:
                    pass

            if not cls._is_photo_recoverable_error(e):
                raise e

        logger.warning(
            "Telegram rejected photo send for %s, falling back to document",
            path,
        )
        document_payload = dict(payload)
        if message_thread_id and "message_thread_id" not in document_payload:
            document_payload["message_thread_id"] = message_thread_id
        filename = os.path.basename(path) or "image"
        document_kwargs: dict[str, Any] = {
            "document": path,
            "filename": filename,
        }
        if caption_fallback is not None:
            document_kwargs["caption"] = caption_fallback
            document_payload.pop("parse_mode", None)
        elif caption := photo_kwargs.get("caption"):
            document_kwargs["caption"] = caption

        if use_media_action:
            await cls._send_media_with_action(
                client,
                ChatAction.UPLOAD_DOCUMENT,
                client.send_document,
                user_name=user_name,
                **document_kwargs,
                **cast(Any, document_payload),
            )
        else:
            await client.send_document(
                **document_kwargs,
                **cast(Any, document_payload),
            )

    async def _ensure_typing(
        self,
        user_name: str,
        message_thread_id: str | None = None,
    ) -> None:
        """确保显示 typing 状态"""
        await self._send_chat_action(
            self.client, user_name, ChatAction.TYPING, message_thread_id
        )

    async def send_typing(self) -> None:
        message_thread_id = None
        if self.get_message_type() == MessageType.GROUP_MESSAGE:
            user_name = self.message_obj.group_id
        else:
            user_name = self.get_sender_id()

        if "#" in user_name:
            user_name, message_thread_id = user_name.split("#")

        await self._ensure_typing(user_name, message_thread_id)

    @classmethod
    async def send_with_client(
        cls,
        client: ExtBot,
        message: MessageChain,
        user_name: str,
    ) -> None:
        has_reply = False
        reply_message_id = None
        at_user_id = None
        for i in message.chain:
            if isinstance(i, Reply):
                has_reply = True
                reply_message_id = i.id
            if isinstance(i, At):
                at_user_id = i.name

        # Handle reply_markup (inline keyboard)
        reply_markup_keyboard = None
        if hasattr(message, "reply_markup") and message.reply_markup:
            try:
                keyboard_buttons = []
                for row in message.reply_markup:
                    button_row = []
                    for button in row:
                        button_row.append(InlineKeyboardButton(**button))
                    keyboard_buttons.append(button_row)
                reply_markup_keyboard = InlineKeyboardMarkup(keyboard_buttons)
            except Exception as e:
                logger.warning(
                    f"Failed to convert reply_markup to InlineKeyboardMarkup: {e}"
                )

        at_flag = False
        message_thread_id = None
        if "#" in user_name:
            # it's a supergroup chat with message_thread_id
            user_name, message_thread_id = user_name.split("#")

        plain_texts: list[str] = []
        images: list[Image] = []
        videos: list[Video] = []
        files: list[File] = []
        records: list[Record] = []

        for i in message.chain:
            if isinstance(i, Plain):
                if at_user_id and not at_flag:
                    plain_texts.append(f"@{at_user_id} {i.text}")
                    at_flag = True
                else:
                    plain_texts.append(i.text)
            elif isinstance(i, Image):
                images.append(i)
            elif isinstance(i, Video):
                videos.append(i)
            elif isinstance(i, File):
                files.append(i)
            elif isinstance(i, Record):
                records.append(i)

        full_text = " ".join(plain_texts) if plain_texts else None

        action = cls._get_chat_action_for_chain(message.chain)
        await cls._send_chat_action(client, user_name, action, message_thread_id)

        def get_base_payload() -> dict:
            payload = {
                "chat_id": user_name,
            }
            if has_reply:
                payload["reply_to_message_id"] = str(reply_message_id)
            if message_thread_id:
                payload["message_thread_id"] = message_thread_id
            return payload

        if images:
            image_infos: list[tuple[str, bool, bool]] = []
            for img in images:
                image_path = await img.convert_to_file_path()
                image_infos.append((image_path, img.use_spoiler, _is_gif(image_path)))

            caption_chunks = cls._prepare_text_chunks(
                full_text or "",
                cls.MAX_CAPTION_LENGTH,
            )
            caption_text = caption_chunks[0][0] if caption_chunks else None
            caption_parse_mode = (
                "MarkdownV2"
                if caption_chunks and caption_chunks[0][1] is not None
                else None
            )
            caption_fallback = caption_chunks[0][1] if caption_chunks else None
            remaining_text_chunks = caption_chunks[1:]

            caption_sent = False

            def _caption_for_next_media() -> tuple[str | None, str | None, str | None]:
                nonlocal caption_sent
                if caption_sent or not caption_text:
                    return None, None, None
                caption_sent = True
                return caption_text, caption_parse_mode, caption_fallback

            async def _send_photo_batch(batch: list[tuple[str, bool, bool]]) -> None:
                if not batch:
                    return

                if len(batch) >= 2:
                    for batch_start in range(0, len(batch), cls.MAX_MEDIA_GROUP_SIZE):
                        group = batch[
                            batch_start : batch_start + cls.MAX_MEDIA_GROUP_SIZE
                        ]
                        payload = get_base_payload()
                        media: list[InputMediaPhoto] = []
                        group_caption: str | None = None
                        group_parse_mode: str | None = None
                        group_caption_fallback: str | None = None
                        if batch_start == 0:
                            (
                                group_caption,
                                group_parse_mode,
                                group_caption_fallback,
                            ) = _caption_for_next_media()

                        for idx, (img_path, use_spoiler, _) in enumerate(group):
                            photo_kwargs: dict[str, Any] = {
                                "media": img_path,
                                "has_spoiler": use_spoiler,
                            }
                            if idx == 0 and group_caption:
                                photo_kwargs["caption"] = group_caption
                                if group_parse_mode:
                                    photo_kwargs["parse_mode"] = group_parse_mode
                            media.append(InputMediaPhoto(**photo_kwargs))

                        try:
                            await client.send_media_group(
                                media=media,
                                **cast(Any, payload),
                            )
                        except Exception as e:
                            logger.warning(
                                f"send_media_group failed, fallback to single photos: {e}"
                            )
                            for idx, (img_path, use_spoiler, _) in enumerate(group):
                                photo_payload = get_base_payload()
                                photo_kwargs: dict[str, Any] = {
                                    "photo": img_path,
                                    "has_spoiler": use_spoiler,
                                }
                                if idx == 0 and group_caption:
                                    photo_kwargs["caption"] = group_caption
                                    if group_parse_mode:
                                        photo_payload["parse_mode"] = group_parse_mode
                                await cls._send_photo_with_document_fallback(
                                    client,
                                    img_path,
                                    photo_payload,
                                    photo_kwargs,
                                    caption_fallback=group_caption_fallback,
                                )
                else:
                    only_path, only_spoiler, _ = batch[0]
                    payload = get_base_payload()
                    photo_kwargs: dict[str, Any] = {
                        "photo": only_path,
                        "has_spoiler": only_spoiler,
                    }
                    (
                        single_caption,
                        single_parse_mode,
                        single_caption_fallback,
                    ) = _caption_for_next_media()
                    if single_caption:
                        photo_kwargs["caption"] = single_caption
                        if single_parse_mode:
                            payload["parse_mode"] = single_parse_mode
                    await cls._send_photo_with_document_fallback(
                        client,
                        only_path,
                        payload,
                        photo_kwargs,
                        caption_fallback=single_caption_fallback,
                    )

            pending_photos: list[tuple[str, bool, bool]] = []
            has_gif = any(is_gif for _, _, is_gif in image_infos)

            if len(image_infos) >= 2 and has_gif:
                pending_photos = [info for info in image_infos if not info[2]]
                await _send_photo_batch(pending_photos)
                for image_path, _, _ in [info for info in image_infos if info[2]]:
                    payload = get_base_payload()
                    animation_kwargs: dict[str, Any] = {"animation": image_path}
                    (
                        gif_caption,
                        gif_parse_mode,
                        gif_caption_fallback,
                    ) = _caption_for_next_media()
                    if gif_caption:
                        animation_kwargs["caption"] = gif_caption
                        if gif_parse_mode:
                            payload["parse_mode"] = gif_parse_mode
                    animation_payload = dict(payload)
                    animation_payload.update(animation_kwargs)
                    await cls._send_media_with_caption_fallback(
                        client.send_animation,
                        animation_payload,
                        gif_caption_fallback,
                    )
            elif len(image_infos) >= 2:
                await _send_photo_batch(image_infos)
            else:
                first_image_path, first_spoiler, first_is_gif = image_infos[0]
                payload = get_base_payload()

                if first_is_gif:
                    animation_kwargs: dict[str, Any] = {"animation": first_image_path}
                    (
                        gif_caption,
                        gif_parse_mode,
                        gif_caption_fallback,
                    ) = _caption_for_next_media()
                    if gif_caption:
                        animation_kwargs["caption"] = gif_caption
                        if gif_parse_mode:
                            payload["parse_mode"] = gif_parse_mode
                    animation_payload = dict(payload)
                    animation_payload.update(animation_kwargs)
                    await cls._send_media_with_caption_fallback(
                        client.send_animation,
                        animation_payload,
                        gif_caption_fallback,
                    )
                    if not caption_sent and full_text:
                        remaining_text_chunks = cls._prepare_text_chunks(full_text)
                else:
                    await _send_photo_batch([(first_image_path, first_spoiler, False)])

            if remaining_text_chunks:
                payload = get_base_payload()
                payload["disable_web_page_preview"] = True
                await cls._send_prepared_text_chunks(
                    client,
                    remaining_text_chunks,
                    payload,
                )
        elif videos:
            first_video = videos[0]
            path = await first_video.convert_to_file_path()
            payload = get_base_payload()
            caption_source = getattr(first_video, "text", None) or full_text or ""
            caption_chunks = cls._prepare_text_chunks(
                caption_source,
                cls.MAX_CAPTION_LENGTH,
            )
            caption = caption_chunks[0][0] if caption_chunks else None
            caption_fallback = caption_chunks[0][1] if caption_chunks else None
            if caption_chunks and caption_chunks[0][1] is not None:
                payload["parse_mode"] = "MarkdownV2"
            video_payload = dict(payload)
            video_payload.update(video=path, caption=caption)
            await cls._send_media_with_caption_fallback(
                client.send_video,
                video_payload,
                caption_fallback,
            )

            remaining_text_chunks = caption_chunks[1:]
            for vid in videos[1:]:
                path = await vid.convert_to_file_path()
                payload = get_base_payload()
                video_caption_source = getattr(vid, "text", None) or ""
                video_caption_chunks = cls._prepare_text_chunks(
                    video_caption_source,
                    cls.MAX_CAPTION_LENGTH,
                )
                video_caption = (
                    video_caption_chunks[0][0] if video_caption_chunks else None
                )
                video_caption_fallback = (
                    video_caption_chunks[0][1] if video_caption_chunks else None
                )
                if video_caption_chunks and video_caption_chunks[0][1] is not None:
                    payload["parse_mode"] = "MarkdownV2"
                video_payload = dict(payload)
                video_payload.update(video=path, caption=video_caption)
                await cls._send_media_with_caption_fallback(
                    client.send_video,
                    video_payload,
                    video_caption_fallback,
                )
                remaining_text_chunks.extend(video_caption_chunks[1:])

            if remaining_text_chunks:
                payload = get_base_payload()
                payload["disable_web_page_preview"] = True
                await cls._send_prepared_text_chunks(
                    client,
                    remaining_text_chunks,
                    payload,
                )
        elif full_text:
            chunks = cls._prepare_text_chunks(full_text)
            for i, chunk in enumerate(chunks):
                payload = get_base_payload()
                payload["disable_web_page_preview"] = True
                payload["reply_markup"] = reply_markup_keyboard if i == 0 else None
                await cls._send_prepared_text_chunks(client, [chunk], payload)

        for f in files:
            payload = get_base_payload()
            path = await f.get_file()
            name = f.name or os.path.basename(path)
            await client.send_document(
                document=path, filename=name, **cast(Any, payload)
            )

        for r in records:
            payload = get_base_payload()
            path = await r.convert_to_file_path()
            await cls._send_voice_with_fallback(
                client,
                path,
                payload,
                caption=r.text or None,
                use_media_action=False,
            )

    async def send(self, message: MessageChain) -> None:
        if self.get_message_type() == MessageType.GROUP_MESSAGE:
            await self.send_with_client(self.client, message, self.message_obj.group_id)
        else:
            await self.send_with_client(self.client, message, self.get_sender_id())
        await super().send(message)

    def supports_tool_call_status_update(self) -> bool:
        """Return whether Telegram can edit a sent tool-call status message."""
        return True

    async def update_tool_call_status(self, message: MessageChain) -> bool:
        """Send or edit the current tool-call status message.

        Args:
            message: The merged tool-call status message.

        Returns:
            Whether a new status message was sent.
        """
        text = message.get_plain_text(with_other_comps_mark=True)
        if not text:
            return False

        prepared_chunks = self._prepare_text_chunks(text)
        if not prepared_chunks:
            return False
        formatted_text, plain_text = prepared_chunks[0]

        if self._tool_call_status_message_id is not None:
            try:
                edit_payload: dict[str, Any] = {
                    "chat_id": self._tool_call_status_chat_id,
                    "message_id": self._tool_call_status_message_id,
                    "text": formatted_text,
                }
                if plain_text is not None:
                    edit_payload["parse_mode"] = "MarkdownV2"
                await self.client.edit_message_text(**cast(Any, edit_payload))
                return False
            except Exception as e:
                logger.warning("Failed to edit tool-call status message: %s", e)
                self.clear_tool_call_status()

        if self.get_message_type() == MessageType.GROUP_MESSAGE:
            chat_id = self.message_obj.group_id
        else:
            chat_id = self.get_sender_id()
        message_thread_id = None
        if "#" in chat_id:
            chat_id, message_thread_id = chat_id.split("#", maxsplit=1)

        payload: dict[str, Any] = {"chat_id": chat_id}
        if message_thread_id:
            payload["message_thread_id"] = message_thread_id
        try:
            send_payload = dict(payload)
            if plain_text is not None:
                send_payload["parse_mode"] = "MarkdownV2"
            sent_message = await self.client.send_message(
                text=formatted_text,
                **cast(Any, send_payload),
            )
        except Exception as e:
            logger.warning("Failed to send formatted tool-call status message: %s", e)
            sent_message = await self.client.send_message(
                text=plain_text or formatted_text,
                **cast(Any, payload),
            )

        self._tool_call_status_chat_id = chat_id
        self._tool_call_status_message_id = sent_message.message_id
        self._tool_call_status_thread_id = message_thread_id
        await super().send(message)
        return True

    def clear_tool_call_status(self) -> None:
        """Clear the current editable tool-call status message reference."""
        self._tool_call_status_chat_id = None
        self._tool_call_status_message_id = None
        self._tool_call_status_thread_id = None

    async def react(self, emoji: str | None, big: bool = False) -> None:
        """给原消息添加 Telegram 反应：
        - 普通 emoji：传入 '👍'、'😂' 等
        - 自定义表情：传入其 custom_emoji_id（纯数字字符串）
        - 取消本机器人的反应：传入 None 或空字符串
        """
        try:
            # 解析 chat_id（去掉超级群的 "#<thread_id>" 片段）
            if self.get_message_type() == MessageType.GROUP_MESSAGE:
                chat_id = (self.message_obj.group_id or "").split("#")[0]
            else:
                chat_id = self.get_sender_id()

            message_id = int(self.message_obj.message_id)

            # 组装 reaction 参数（必须是 ReactionType 的列表）
            if not emoji:  # 清空本 bot 的反应
                reaction_param = []  # 空列表表示移除本 bot 的反应
            elif emoji.isdigit():  # 自定义表情：传 custom_emoji_id
                reaction_param = [ReactionTypeCustomEmoji(emoji)]
            else:  # 普通 emoji
                reaction_param = [ReactionTypeEmoji(emoji)]

            await self.client.set_message_reaction(
                chat_id=chat_id,
                message_id=message_id,
                reaction=reaction_param,  # 注意是列表
                is_big=big,  # 可选：大动画
            )
        except Exception as e:
            logger.error(f"[Telegram] 添加反应失败: {e}")

    async def _send_message_draft(
        self,
        chat_id: str,
        draft_id: int,
        text: str,
        message_thread_id: str | None = None,
        parse_mode: str | None = None,
    ) -> None:
        """通过 Bot.send_message_draft 发送草稿消息（流式推送部分消息）。

        该 API 仅支持私聊。

        Args:
            chat_id: 目标私聊的 chat_id
            draft_id: 草稿唯一标识，非零整数；相同 draft_id 的变更会以动画展示
            text: 消息文本，1-4096 字符
            message_thread_id: 可选，目标消息线程 ID
            parse_mode: 可选，消息文本的解析模式

        """
        if not text or not text.strip():
            return

        kwargs: dict[str, Any] = {}
        if message_thread_id:
            kwargs["message_thread_id"] = int(message_thread_id)
        if parse_mode:
            kwargs["parse_mode"] = parse_mode

        try:
            logger.debug(
                f"[Telegram] sendMessageDraft: chat_id={chat_id}, draft_id={draft_id}, text_len={len(text)}"
            )
            await self.client.send_message_draft(
                chat_id=int(chat_id),
                draft_id=draft_id,
                text=text,
                **kwargs,
            )
        except Exception as e:
            logger.warning(f"[Telegram] sendMessageDraft 失败: {e!s}")

    async def _process_chain_items(
        self,
        chain: MessageChain,
        payload: dict[str, Any],
        user_name: str,
        message_thread_id: str | None,
        on_text: Callable[[str], None],
    ) -> None:
        """处理 MessageChain 中的各类组件，文本通过 on_text 回调追加，媒体直接发送。"""
        for i in chain.chain:
            if isinstance(i, Plain):
                on_text(i.text)
            elif isinstance(i, Image):
                image_path = await i.convert_to_file_path()
                if _is_gif(image_path):
                    await self._send_media_with_action(
                        self.client,
                        ChatAction.UPLOAD_VIDEO,
                        self.client.send_animation,
                        user_name=user_name,
                        animation=image_path,
                        **cast(Any, payload),
                    )
                else:
                    await self._send_photo_with_document_fallback(
                        self.client,
                        image_path,
                        payload,
                        {"photo": image_path},
                        user_name=user_name,
                        message_thread_id=message_thread_id,
                        use_media_action=True,
                    )
            elif isinstance(i, File):
                path = await i.get_file()
                name = i.name or os.path.basename(path)
                await self._send_media_with_action(
                    self.client,
                    ChatAction.UPLOAD_DOCUMENT,
                    self.client.send_document,
                    user_name=user_name,
                    document=path,
                    filename=name,
                    **cast(Any, payload),
                )
            elif isinstance(i, Record):
                path = await i.convert_to_file_path()
                await self._send_voice_with_fallback(
                    self.client,
                    path,
                    payload,
                    caption=i.text or None,
                    user_name=user_name,
                    message_thread_id=message_thread_id,
                    use_media_action=True,
                )
            elif isinstance(i, Video):
                path = await i.convert_to_file_path()
                await self._send_media_with_action(
                    self.client,
                    ChatAction.UPLOAD_VIDEO,
                    self.client.send_video,
                    user_name=user_name,
                    video=path,
                    **cast(Any, payload),
                )
            else:
                logger.warning(f"不支持的消息类型: {type(i)}")

    async def _send_final_segment(self, delta: str, payload: dict[str, Any]) -> None:
        """将累积文本作为 MarkdownV2 真实消息发送，失败时回退到纯文本。"""
        await self._send_text_chunks(self.client, delta, payload)

    async def send_streaming(self, generator, use_fallback: bool = False):
        message_thread_id = None

        if self.get_message_type() == MessageType.GROUP_MESSAGE:
            user_name = self.message_obj.group_id
        else:
            user_name = self.get_sender_id()

        if "#" in user_name:
            # it's a supergroup chat with message_thread_id
            user_name, message_thread_id = user_name.split("#")
        payload = {
            "chat_id": user_name,
        }
        if message_thread_id:
            payload["message_thread_id"] = message_thread_id

        # sendMessageDraft 仅支持私聊（显式检查 FRIEND_MESSAGE）
        is_private = self.get_message_type() == MessageType.FRIEND_MESSAGE

        if is_private:
            logger.info("[Telegram] 流式输出: 使用 sendMessageDraft (私聊)")
            await self._send_streaming_draft(
                user_name, message_thread_id, payload, generator
            )
        else:
            logger.info("[Telegram] 流式输出: 使用 edit_message_text fallback (群聊)")
            await self._send_streaming_edit(
                user_name, message_thread_id, payload, generator
            )

        # 内联父类 send_streaming 的副作用（避免传入已消费的 generator）
        asyncio.create_task(
            Metric.upload(msg_event_tick=1, adapter_name=self.platform_meta.name),
        )
        self._has_send_oper = True

    async def _send_streaming_draft(
        self,
        user_name: str,
        message_thread_id: str | None,
        payload: dict[str, Any],
        generator,
    ) -> None:
        """使用 sendMessageDraft API 进行流式推送（私聊专用）。

        流式过程中使用 sendMessageDraft 推送草稿动画，
        流式结束后发送一条真实消息保留最终内容（draft 是临时的，会消失）。
        使用信号驱动的发送循环：每次有新 token 到达时唤醒发送，
        发送频率由网络 RTT 自然限制（最多一个请求 in-flight）。
        """
        draft_id = self._allocate_draft_id()
        delta = ""
        last_sent_text = ""
        done = False  # 信号：生成器已结束
        text_changed = asyncio.Event()  # 有新 token 到达时触发

        async def _draft_sender_loop() -> None:
            """信号驱动的草稿发送循环，有新内容就发，RTT 自然限流。"""
            nonlocal last_sent_text
            while not done:
                await text_changed.wait()
                text_changed.clear()
                # 发送最新的缓冲区内容（MarkdownV2 渲染，与真实消息一致）
                if delta and delta != last_sent_text:
                    prepared_chunks = self._prepare_text_chunks(
                        delta,
                        self.MAX_MESSAGE_LENGTH,
                    )
                    if prepared_chunks:
                        draft_text, plain_text = prepared_chunks[0]
                        try:
                            await self._send_message_draft(
                                user_name,
                                draft_id,
                                draft_text,
                                message_thread_id,
                                parse_mode=(
                                    "MarkdownV2" if plain_text is not None else None
                                ),
                            )
                            last_sent_text = delta
                        except Exception:
                            try:
                                await self._send_message_draft(
                                    user_name,
                                    draft_id,
                                    plain_text or draft_text,
                                    message_thread_id,
                                )
                                last_sent_text = delta
                            except Exception as e2:
                                logger.debug(
                                    f"[Telegram] sendMessageDraft failed (ignored): {e2!s}"
                                )

        sender_task = asyncio.create_task(_draft_sender_loop())

        def _append_text(t: str) -> None:
            nonlocal delta
            delta += t
            text_changed.set()  # 唤醒发送循环

        try:
            async for chain in generator:
                if not isinstance(chain, MessageChain):
                    continue

                if chain.type == "break":
                    # 分割符：发送真实消息保留内容，重置缓冲区
                    if delta:
                        # 用 emoji 清空 draft 显示，避免 draft 和真实消息同时可见
                        await self._send_message_draft(
                            user_name,
                            draft_id,
                            "\u23f3",
                            message_thread_id,
                        )
                        await self._send_final_segment(delta, payload)
                    delta = ""
                    last_sent_text = ""
                    draft_id = self._allocate_draft_id()
                    continue

                await self._process_chain_items(
                    chain, payload, user_name, message_thread_id, _append_text
                )
        finally:
            done = True
            text_changed.set()  # 唤醒循环使其退出
            await sender_task

        # 流式结束：用 emoji 清空 draft，然后发真实消息持久化
        if delta:
            await self._send_message_draft(
                user_name,
                draft_id,
                "\u23f3",
                message_thread_id,
            )
            await self._send_final_segment(delta, payload)

    async def _send_streaming_edit(
        self,
        user_name: str,
        message_thread_id: str | None,
        payload: dict[str, Any],
        generator,
    ) -> None:
        """使用 send_message + edit_message_text 进行流式推送（群聊 fallback）。"""
        delta = ""
        current_content = ""
        message_id = None
        last_edit_time = 0  # 上次编辑消息的时间
        throttle_interval = 0.6  # 编辑消息的间隔时间 (秒)
        last_chat_action_time = 0  # 上次发送 chat action 的时间
        chat_action_interval = 0.5  # chat action 的节流间隔 (秒)

        # 发送初始 typing 状态
        await self._ensure_typing(user_name, message_thread_id)
        last_chat_action_time = asyncio.get_running_loop().time()

        def _append_text(t: str) -> None:
            nonlocal delta
            delta += t

        async for chain in generator:
            if not isinstance(chain, MessageChain):
                continue

            if chain.type == "break":
                # 分割符
                if message_id:
                    try:
                        await self.client.edit_message_text(
                            text=delta,
                            chat_id=payload["chat_id"],
                            message_id=message_id,
                        )
                    except Exception as e:
                        logger.warning(f"编辑消息失败(streaming-break): {e!s}")
                message_id = None
                delta = ""
                continue

            await self._process_chain_items(
                chain, payload, user_name, message_thread_id, _append_text
            )

            delta_length = self._utf16_length(delta)
            prepared_chunks: list[tuple[str, str | None]] | None = None
            if delta_length > self.MAX_MESSAGE_LENGTH:
                prepared_chunks = self._prepare_text_chunks(delta)
                if len(prepared_chunks) > 1:
                    chunks_to_send = prepared_chunks
                    if message_id:
                        first_formatted, first_plain = prepared_chunks[0]
                        edit_payload: dict[str, Any] = {
                            "text": first_formatted,
                            "chat_id": payload["chat_id"],
                            "message_id": message_id,
                        }
                        if first_plain is not None:
                            edit_payload["parse_mode"] = "MarkdownV2"
                        try:
                            await self.client.edit_message_text(
                                **cast(Any, edit_payload)
                            )
                        except (ValueError, BadRequest):
                            if first_plain is not None:
                                await self.client.edit_message_text(
                                    text=first_plain,
                                    chat_id=payload["chat_id"],
                                    message_id=message_id,
                                )
                        except Exception as e:
                            logger.warning(f"编辑消息失败(streaming-overflow): {e!s}")
                        chunks_to_send = prepared_chunks[1:]
                    if chunks_to_send:
                        await self._send_prepared_text_chunks(
                            self.client,
                            chunks_to_send,
                            payload,
                        )
                    message_id = None
                    current_content = ""
                    delta = ""
                    last_edit_time = asyncio.get_running_loop().time()
                    continue

            if prepared_chunks:
                display_text, display_plain_text = prepared_chunks[0]
            else:
                display_text, display_plain_text = delta, None

            # 编辑或发送消息
            if message_id and (
                delta_length <= self.MAX_MESSAGE_LENGTH or prepared_chunks
            ):
                current_time = asyncio.get_running_loop().time()
                time_since_last_edit = current_time - last_edit_time

                if time_since_last_edit >= throttle_interval:
                    current_time = asyncio.get_running_loop().time()
                    if current_time - last_chat_action_time >= chat_action_interval:
                        await self._ensure_typing(user_name, message_thread_id)
                        last_chat_action_time = current_time
                    try:
                        edit_payload = {
                            "text": display_text,
                            "chat_id": payload["chat_id"],
                            "message_id": message_id,
                        }
                        if display_plain_text is not None:
                            edit_payload["parse_mode"] = "MarkdownV2"
                        await self.client.edit_message_text(**cast(Any, edit_payload))
                        current_content = delta
                    except (ValueError, BadRequest):
                        if display_plain_text is not None:
                            try:
                                await self.client.edit_message_text(
                                    text=display_plain_text,
                                    chat_id=payload["chat_id"],
                                    message_id=message_id,
                                )
                                current_content = delta
                            except Exception as e:
                                logger.warning(f"编辑消息失败(streaming): {e!s}")
                    except Exception as e:
                        logger.warning(f"编辑消息失败(streaming): {e!s}")
                    last_edit_time = asyncio.get_running_loop().time()
            else:
                current_time = asyncio.get_running_loop().time()
                if current_time - last_chat_action_time >= chat_action_interval:
                    await self._ensure_typing(user_name, message_thread_id)
                    last_chat_action_time = current_time
                try:
                    send_payload = dict(payload)
                    if display_plain_text is not None:
                        send_payload["parse_mode"] = "MarkdownV2"
                    msg = await self.client.send_message(
                        text=display_text,
                        **cast(Any, send_payload),
                    )
                    current_content = delta
                    message_id = msg.message_id
                    last_edit_time = asyncio.get_running_loop().time()
                except (ValueError, BadRequest):
                    try:
                        msg = await self.client.send_message(
                            text=display_plain_text or display_text,
                            **cast(Any, payload),
                        )
                        current_content = delta
                        message_id = msg.message_id
                        last_edit_time = asyncio.get_running_loop().time()
                    except Exception as e:
                        logger.warning(f"发送消息失败(streaming): {e!s}")
                except Exception as e:
                    logger.warning(f"发送消息失败(streaming): {e!s}")

        try:
            if delta and current_content != delta:
                prepared_chunks = self._prepare_text_chunks(delta)
                if prepared_chunks:
                    final_text, final_plain_text = prepared_chunks[0]
                    edit_payload = {
                        "text": final_text,
                        "chat_id": payload["chat_id"],
                        "message_id": message_id,
                    }
                    if final_plain_text is not None:
                        edit_payload["parse_mode"] = "MarkdownV2"
                    try:
                        await self.client.edit_message_text(**cast(Any, edit_payload))
                    except (ValueError, BadRequest):
                        if final_plain_text is not None:
                            await self.client.edit_message_text(
                                text=final_plain_text,
                                chat_id=payload["chat_id"],
                                message_id=message_id,
                            )
                    except Exception as e:
                        logger.warning(f"编辑消息失败(streaming-final): {e!s}")
        except Exception as e:
            logger.warning(f"编辑消息失败(streaming): {e!s}")


class TelegramInlineQueryEvent(AstrMessageEvent):
    """Telegram 内联查询事件"""

    MAX_INLINE_RESULTS = 50
    MAX_INLINE_DESCRIPTION_LEN = 100
    _INLINE_ARTICLE_PARAMS: set[str] | None = None

    def __init__(
        self,
        query: str,
        from_user_id: str,
        from_username: str | None,
        inline_query_id: str,
        offset: str,
        platform_meta: PlatformMetadata,
        session_id: str,
        client: ExtBot,
    ) -> None:
        self.query = query
        """内联查询的文本"""
        self.from_user_id = from_user_id
        """发起查询的用户ID"""
        self.from_username = from_username
        """发起查询的用户名"""
        self.inline_query_id = inline_query_id
        """内联查询的唯一ID"""
        self.offset = offset
        """分页偏移量"""
        self.client = client
        self._inline_answer_seq = 0
        self._bot_thumb_url: str | None = None

        # 创建一个虚拟的 AstrBotMessage 用于兼容性
        from astrbot.api.platform import AstrBotMessage, MessageType

        message_obj = AstrBotMessage()
        message_obj.message = [Plain(query)] if query else []
        message_obj.type = MessageType.OTHER_MESSAGE  # 内联查询视为系统消息
        nickname = from_username or from_user_id
        message_obj.sender = MessageMember(user_id=from_user_id, nickname=nickname)

        super().__init__(
            message_str=query,
            message_obj=message_obj,
            platform_meta=platform_meta,
            session_id=session_id,
        )

    def get_sender_id(self) -> str:
        return self.from_user_id

    def get_sender_name(self) -> str:
        return self.from_username or self.from_user_id

    def get_message_type(self) -> MessageType:
        return MessageType.OTHER_MESSAGE

    def _chain_to_text(self, message_chain: MessageChain) -> str:
        text = message_chain.get_plain_text(with_other_comps_mark=True)
        return text.strip()

    async def _get_bot_thumb_url(self) -> str | None:
        if self._bot_thumb_url is not None:
            return self._bot_thumb_url
        try:
            me = await self.client.get_me()
            photos = await self.client.get_user_profile_photos(me.id, limit=1)
            if photos.total_count <= 0:
                self._bot_thumb_url = ""
                return None
            first_photo = photos.photos[0][-1]
            file_obj = await self.client.get_file(first_photo.file_id)
            url = getattr(file_obj, "file_path", None)
            if isinstance(url, str) and url:
                self._bot_thumb_url = url
                return url
        except Exception as e:
            logger.debug(f"获取 bot 头像失败: {e!s}")
        self._bot_thumb_url = ""
        return None

    async def _build_inline_results(self, text: str) -> list[InlineQueryResultArticle]:
        if not text:
            return []
        chunks = TelegramPlatformEvent._split_message(text)
        thumb_url = "https://raw.githubusercontent.com/AstrBotDevs/AstrBot/7c39abc6b5ce0e61c10ed2d7c41a4079d771d6cf/dashboard/src/assets/images/astrbot_logo_mini.webp"
        if self._INLINE_ARTICLE_PARAMS is None:
            try:
                self._INLINE_ARTICLE_PARAMS = set(
                    inspect.signature(InlineQueryResultArticle).parameters.keys(),
                )
            except Exception:
                self._INLINE_ARTICLE_PARAMS = set()
        results: list[InlineQueryResultArticle] = []
        for idx, chunk in enumerate(chunks[: self.MAX_INLINE_RESULTS]):
            raw_id = f"{self.inline_query_id}:{self._inline_answer_seq}:{idx}"
            hashed = hashlib.blake2b(raw_id.encode("utf-8"), digest_size=6).hexdigest()
            result_id = f"ab-{hashed}"
            title = "AstrBot"
            description = chunk.replace("\n", " ").strip()
            if len(description) > self.MAX_INLINE_DESCRIPTION_LEN:
                description = description[: self.MAX_INLINE_DESCRIPTION_LEN - 1] + "…"
            kwargs = {
                "id": result_id,
                "title": title,
                "description": description,
                "input_message_content": InputTextMessageContent(
                    message_text=chunk[: TelegramPlatformEvent.MAX_MESSAGE_LENGTH],
                ),
                "reply_markup": InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "Bot正在思考 🤖", callback_data="astrbot_thinking"
                            )
                        ]
                    ],
                ),
            }
            if thumb_url:
                if "thumb_url" in self._INLINE_ARTICLE_PARAMS:
                    kwargs["thumb_url"] = thumb_url
                elif "thumbnail_url" in self._INLINE_ARTICLE_PARAMS:
                    kwargs["thumbnail_url"] = thumb_url
            results.append(
                InlineQueryResultArticle(**kwargs),
            )
        return results

    async def _build_inline_option_results(
        self, options: list[dict[str, Any]]
    ) -> list[InlineQueryResultArticle]:
        if self._INLINE_ARTICLE_PARAMS is None:
            try:
                self._INLINE_ARTICLE_PARAMS = set(
                    inspect.signature(InlineQueryResultArticle).parameters.keys(),
                )
            except Exception:
                self._INLINE_ARTICLE_PARAMS = set()

        thumb_url = "https://raw.githubusercontent.com/AstrBotDevs/AstrBot/7c39abc6b5ce0e61c10ed2d7c41a4079d771d6cf/dashboard/src/assets/images/astrbot_logo_mini.webp"
        query_preview = self.query or "inline query"
        results: list[InlineQueryResultArticle] = []
        for option in options[: self.MAX_INLINE_RESULTS]:
            title = str(option.get("title") or "AstrBot")
            description = str(option.get("description") or "")
            if len(description) > self.MAX_INLINE_DESCRIPTION_LEN:
                description = description[: self.MAX_INLINE_DESCRIPTION_LEN - 3] + "..."
            kind = str(option.get("kind") or "")
            if kind in {"llm", "command"}:
                placeholder = query_preview
            else:
                placeholder = f"{title}: {query_preview}"
            kwargs = {
                "id": str(option["id"]),
                "title": title,
                "description": description,
                "input_message_content": InputTextMessageContent(
                    message_text=placeholder[
                        : TelegramPlatformEvent.MAX_MESSAGE_LENGTH
                    ],
                ),
                "reply_markup": InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "Stop Agent",
                                callback_data=TELEGRAM_INLINE_STOP_CALLBACK_DATA,
                            )
                        ]
                    ],
                ),
            }
            if thumb_url:
                if "thumb_url" in self._INLINE_ARTICLE_PARAMS:
                    kwargs["thumb_url"] = thumb_url
                elif "thumbnail_url" in self._INLINE_ARTICLE_PARAMS:
                    kwargs["thumbnail_url"] = thumb_url
            results.append(InlineQueryResultArticle(**kwargs))
        return results

    async def _answer_inline_query(self, text: str) -> None:
        results = await self._build_inline_results(text)
        if not results:
            logger.warning(
                "TelegramInlineQueryEvent 空结果，跳过 answer_inline_query。"
            )
            return
        self._inline_answer_seq += 1
        try:
            await self.client.answer_inline_query(
                inline_query_id=self.inline_query_id,
                results=results,
                cache_time=0,
                is_personal=True,
            )
        except Exception as e:
            logger.warning(f"answer_inline_query 失败: {e!s}")

    async def answer_inline_options(self, options: list[dict[str, Any]]) -> None:
        results = await self._build_inline_option_results(options)
        if not results:
            logger.warning("TelegramInlineQueryEvent 无可用选项，跳过响应。")
            return
        try:
            await self.client.answer_inline_query(
                inline_query_id=self.inline_query_id,
                results=results,
                cache_time=0,
                is_personal=True,
            )
        except Exception as e:
            logger.warning(f"answer_inline_query 选项响应失败: {e!s}")

    async def send_with_client(
        self, client: ExtBot, message_chain: MessageChain, user_name: str
    ) -> None:
        """内联查询通过 answer_inline_query 响应"""
        _ = client
        _ = user_name
        text = self._chain_to_text(message_chain)
        if not text:
            logger.warning("TelegramInlineQueryEvent 消息为空，跳过发送。")
            return
        await self._answer_inline_query(text)

    async def send(self, message: MessageChain) -> None:
        await self.send_with_client(self.client, message, self.get_sender_id())
        await super().send(message)

    async def send_streaming(self, generator, use_fallback: bool = False):
        if not self.inline_query_id:
            logger.warning(
                "TelegramInlineQueryEvent 缺少 inline_query_id，无法流式响应。"
            )
            return
        delta = ""
        last_send_time = 0.0
        throttle_interval = 0.6
        loop = asyncio.get_running_loop()

        async for chain in generator:
            if not isinstance(chain, MessageChain):
                continue
            if chain.type == "break":
                if delta:
                    await self._answer_inline_query(delta)
                delta = ""
                continue
            delta += chain.get_plain_text(with_other_comps_mark=True)
            now = loop.time()
            if now - last_send_time >= throttle_interval:
                await self._answer_inline_query(delta)
                last_send_time = now

        if delta:
            await self._answer_inline_query(delta)

        await super().send_streaming(generator, use_fallback)


class TelegramChosenInlineResultEvent(AstrMessageEvent):
    """Telegram 选择内联结果事件"""

    def __init__(
        self,
        result_id: str,
        from_user_id: str,
        from_username: str | None,
        query: str,
        inline_message_id: str | None,
        platform_meta: PlatformMetadata,
        session_id: str,
        client: ExtBot,
    ) -> None:
        self.result_id = result_id
        """被选择的结果ID"""
        self.from_user_id = from_user_id
        """选择结果的用户ID"""
        self.from_username = from_username
        """选择结果的用户名"""
        self.query = query
        """原始查询文本"""
        self.inline_message_id = inline_message_id
        """内联消息ID（如果适用）"""
        self.client = client

        # 创建一个虚拟的 AstrBotMessage 用于兼容性
        from astrbot.api.platform import AstrBotMessage, MessageType

        message_obj = AstrBotMessage()
        message_obj.message = [Plain(query)] if query else []
        message_obj.type = MessageType.OTHER_MESSAGE
        nickname = from_username or from_user_id
        message_obj.sender = MessageMember(user_id=from_user_id, nickname=nickname)

        super().__init__(
            message_str=query,
            message_obj=message_obj,
            platform_meta=platform_meta,
            session_id=session_id,
        )

        if self.inline_message_id:
            telegram_inline_stop_registry.register(
                self.inline_message_id,
                self.unified_msg_origin,
                self.from_user_id,
            )

    def get_sender_id(self) -> str:
        return self.from_user_id

    def get_sender_name(self) -> str:
        return self.from_username or self.from_user_id

    def get_message_type(self) -> MessageType:
        return MessageType.OTHER_MESSAGE

    def _chain_to_text(self, message_chain: MessageChain) -> str:
        parts: list[str] = []
        for comp in message_chain.chain:
            if isinstance(comp, Plain):
                parts.append(comp.text)
        return "".join(parts)

    @staticmethod
    def _get_first_inline_image(message_chain: MessageChain) -> Image | None:
        return next(
            (comp for comp in message_chain.chain if isinstance(comp, Image)), None
        )

    @staticmethod
    def _strip_markdown(text: str) -> str:
        """Strip markdown formatting from text, keeping only plain content.

        Removes all markdown syntax (bold, italic, code blocks, headers, etc.)
        from LLM responses before wrapping them in blockquotes. This prevents
        nested markdown from producing malformed MarkdownV2 entities (e.g. unclosed
        spoiler/expandable-blockquote ``||...||``) when telegramify_markdown converts
        the ``>``-prefixed blockquote structure.
        """
        # Fenced code blocks: keep inner content, drop fence markers
        text = re.sub(
            r"```[^\n]*\n(.*?)```",
            lambda m: m.group(1),
            text,
            flags=re.DOTALL,
        )
        # Inline code
        text = re.sub(r"`([^`\n]+)`", r"\1", text)
        # ATX headers
        text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
        # Bold + italic (*** or ___)
        text = re.sub(r"\*{3}(.+?)\*{3}", r"\1", text, flags=re.DOTALL)
        text = re.sub(r"_{3}(.+?)_{3}", r"\1", text, flags=re.DOTALL)
        # Bold (** or __)
        text = re.sub(r"\*{2}(.+?)\*{2}", r"\1", text, flags=re.DOTALL)
        text = re.sub(r"_{2}(.+?)_{2}", r"\1", text, flags=re.DOTALL)
        # Italic (* or _)
        text = re.sub(r"\*(.+?)\*", r"\1", text, flags=re.DOTALL)
        text = re.sub(r"_([^_\n]+?)_", r"\1", text)
        # Spoiler entities
        text = re.sub(r"\|\|(.+?)\|\|", r"\1", text, flags=re.DOTALL)
        # Strikethrough
        text = re.sub(r"~~(.+?)~~", r"\1", text, flags=re.DOTALL)
        # Links — keep display text
        text = re.sub(r"\[([^\]\n]+)\]\([^\)\n]*\)", r"\1", text)
        # Horizontal rules
        text = re.sub(r"^[ \t]*[-*_]{3,}[ \t]*$", "", text, flags=re.MULTILINE)
        return text

    @staticmethod
    def _escape_mv2(text: str) -> str:
        """Escape plain text for safe use inside Telegram MarkdownV2.

        Backslash is escaped first to avoid double-escaping. The resulting
        string can be embedded in any MarkdownV2 context (including blockquotes)
        without producing invalid entities.
        """
        text = text.replace("\\", "\\\\")
        return re.sub(r"([_*\[\]()~`>#+=|{}.!\-])", r"\\\1", text)

    def _build_mv2_blockquote(self, llm_text: str, max_length: int = 3900) -> str:
        """Build a ready-to-send Telegram MarkdownV2 string with blockquote wrapping.

        Strips markdown from the LLM reply, escapes all special characters for
        MarkdownV2, then prefixes every content line with ">" so the reply
        always renders as a blockquote in the Telegram client regardless of
        the original content. Truncates to *max_length* if needed.

        Does not rely on telegramify_markdown, so it never produces malformed
        entities (e.g. unclosed ``||`` spoiler/expandable-blockquote).
        """
        clean = self._strip_markdown(llm_text).strip()
        all_lines: list[str] = []
        if self.query:
            escaped_query = self._escape_mv2(self.query.replace("\n", " ").strip())
            all_lines.append(f">{escaped_query}")
            all_lines.append("")
            all_lines.append(self._escape_mv2("回答："))
            all_lines.append("")
        else:
            all_lines.append(self._escape_mv2("回答："))
            all_lines.append("")
        for line in clean.split("\n"):
            escaped = self._escape_mv2(line)
            all_lines.append(f">{escaped}" if escaped else ">")
        full = "\n".join(all_lines)
        if len(full) <= max_length:
            return full
        truncation = "\n>…"
        available = max_length - len(truncation)
        if available <= 0:
            return full[: max_length - 1] + "…"
        cut_text = full[:available]
        last_nl = cut_text.rfind("\n>")
        if last_nl > available * 0.5:
            cut_text = cut_text[:last_nl]
        return cut_text.rstrip() + truncation

    def _build_mv2_streaming(self, delta: str) -> str:
        """Build a MarkdownV2 blockquote for intermediate streaming display.

        Same structure as *_build_mv2_blockquote* but without length truncation,
        since in-progress streaming text is always shorter than the final reply.
        """
        clean = self._strip_markdown(delta)
        parts: list[str] = []
        if self.query:
            escaped_query = self._escape_mv2(self.query.replace("\n", " ").strip())
            parts.append(f">{escaped_query}")
            parts.append("")
            parts.append(self._escape_mv2("回答："))
            parts.append("")
        else:
            parts.append(self._escape_mv2("回答："))
            parts.append("")
        for line in clean.strip().split("\n"):
            escaped = self._escape_mv2(line)
            parts.append(f">{escaped}" if escaped else ">")
        return "\n".join(parts)

    def _format_inline_response(self, llm_text: str) -> str:
        """Format LLM response with Telegram quote blocks.

        Uses proper separators to prevent query and reply quote blocks
        from being merged into one block by Telegram.

        The reply blockquote will be converted to expandable_blockquote
        by telegramify_markdown if length > 200 characters.

        Args:
            llm_text: The LLM response text to format.

        Returns:
            Formatted text with quote blocks.

        """
        # Strip markdown from LLM text before wrapping in blockquotes.
        # Nested markdown inside ">" prefixes confuses telegramify_markdown,
        # generating unclosed ||spoiler|| entities that Telegram rejects.
        lines = self._strip_markdown(llm_text).strip().split("\n")
        quoted_lines = []

        if self.query:
            # Query quote block (short, non-expandable)
            quoted_lines.append(f"> {self.query}")
            # Empty line separator to split quote blocks
            quoted_lines.append("")
            quoted_lines.append("回答：")
            quoted_lines.append("")
        else:
            quoted_lines.append("回答：")
            quoted_lines.append("")

        # Reply blockquote (may become expandable if > 200 chars)
        for line in lines:
            quoted_lines.append(f"> {line}" if line else ">")

        return "\n".join(quoted_lines)

    def _truncate_for_telegram(self, text: str, max_length: int = 3800) -> str:
        """Truncate text for Telegram message length limit.

        Smart truncation that preserves user query part and truncates only
        the LLM response at paragraph/line boundaries. Must be called before
        markdownify to avoid breaking MarkdownV2 formatting.

        Args:
            text: Formatted inline response text (from _format_inline_response)
            max_length: Maximum allowed length (default 3800 to reserve
                       ~5-15% overhead for markdownify escaping)

        Returns:
            Truncated text if exceeded max_length, otherwise original text

        """
        if len(text) <= max_length:
            return text

        # Format: "> query\n\n回答：\n\n> reply..."
        # Find the start of reply blockquote after "回答："
        prefix_end_marker = "\n回答：\n\n>"
        prefix_end_pos = text.find(prefix_end_marker)

        truncation_indicator = "\n> …"

        if prefix_end_pos != -1:
            # Include the first ">" of reply blockquote in prefix
            prefix = text[: prefix_end_pos + len("\n回答：\n\n")]
            content = text[prefix_end_pos + len("\n回答：\n\n") :]
            available = max_length - len(prefix) - len(truncation_indicator)

            if available <= 0:
                return (
                    text[: max_length - len(truncation_indicator)]
                    + truncation_indicator
                )

            truncated_content = content[:available]
            # Try to truncate at paragraph boundary (empty quote line followed by quote)
            last_para = truncated_content.rfind("\n>\n>")
            if last_para > available * 0.7:
                truncated_content = truncated_content[:last_para]
            else:
                # Try to truncate at line boundary
                last_line = truncated_content.rfind("\n>")
                if last_line > available * 0.5:
                    truncated_content = truncated_content[:last_line]

            return prefix + truncated_content.rstrip() + truncation_indicator

        # Fallback: simple truncation
        truncated = text[: max_length - len(truncation_indicator)]
        last_line = truncated.rfind("\n")
        if last_line > max_length * 0.5:
            truncated = truncated[:last_line]

        return truncated.rstrip() + truncation_indicator

    @staticmethod
    def _utf16_len(text: str) -> int:
        """Return the length of *text* in UTF-16 code units.

        Telegram measures entity ``offset`` and ``length`` in UTF-16 code
        units, not Python ``len()`` characters.  Characters in the Basic
        Multilingual Plane (U+0000–U+FFFF, including all CJK) count as 1;
        characters outside the BMP (most emoji above U+FFFF) count as 2.
        """
        return len(text.encode("utf-16-le")) // 2

    @staticmethod
    def _short_model_name(model_name: str | None) -> str:
        """Normalize a provider/model path to its last model segment."""
        if not model_name:
            return ""
        normalized = str(model_name).strip().strip("/")
        if not normalized:
            return ""
        parts = [part for part in normalized.split("/") if part]
        return parts[-1] if parts else normalized

    def _get_inline_reply_label(self) -> str:
        """Build a compact answer label for inline chosen-result replies."""
        model_name = self._short_model_name(
            self.get_extra("current_chat_model")
            or self.get_extra("selected_model")
            or self.get_extra("selected_provider")
        )
        return f"🤖 {model_name}" if model_name else "🤖 回答"

    def _build_inline_entities(
        self, reply_text: str, max_total: int = 4096
    ) -> tuple[str, list]:
        """Build plain text and ``MessageEntity`` list for a chosen-inline reply.

        Produces the compact layout::

            ❓ query

            🤖 model
            reply          ← EXPANDABLE_BLOCKQUOTE entity containing label + reply

        Markdown is stripped from both query and reply before formatting so
        that raw entity formatting is never needed, avoiding the
        ``telegramify_markdown`` nested-entity pitfalls.

        Args:
            reply_text: Plugin or LLM reply (markdown will be stripped).
            max_total: Maximum total character count (Telegram hard limit).

        Returns:
            ``(plain_text, entities)`` ready to pass to
            ``edit_message_text(entities=…)``.

        """
        from telegram import MessageEntity

        reply_clean = self._strip_markdown(reply_text).strip()
        reply_label = self._get_inline_reply_label()

        if self.query:
            query_clean = self.query.replace("\n", " ").strip()
            query_prefix = f"❓ {query_clean}\n"
        else:
            query_clean = None
            query_prefix = ""
        answer_prefix = f"{reply_label}\n"
        prefix = query_prefix + answer_prefix

        # Truncate reply so the whole message fits within max_total characters.
        available = max_total - len(prefix)
        if available <= 0:
            reply_clean = ""
        elif len(reply_clean) > available:
            reply_clean = reply_clean[: available - 1].rstrip() + "…"

        full_text = prefix + reply_clean
        entities: list[MessageEntity] = []

        answer_length = self._utf16_len(answer_prefix + reply_clean)
        if answer_length > 0:
            entities.append(
                MessageEntity(
                    type=MessageEntity.EXPANDABLE_BLOCKQUOTE,
                    offset=self._utf16_len(query_prefix),
                    length=answer_length,
                )
            )

        return full_text, entities

    async def _edit_inline_message(
        self,
        text: str,
        parse_mode: str | None = None,
        reply_markup=None,
        entities: list | None = None,
    ) -> None:
        """Edit inline message, raising exception on failure for caller to handle.

        When *entities* is provided it takes precedence over *parse_mode*,
        allowing callers to use the native entity API (e.g. for
        ``EXPANDABLE_BLOCKQUOTE``) instead of a markup parse mode.

        Args:
            text: Message text.
            parse_mode: Parse mode (e.g. ``"MarkdownV2"``). Ignored when
                *entities* is supplied.
            reply_markup: Inline keyboard markup.
            entities: Pre-built ``MessageEntity`` list.  When set,
                ``parse_mode`` is not forwarded to the API.

        Raises:
            Exception: If edit fails, allowing caller to implement fallback.

        """
        if not self.inline_message_id:
            logger.debug(
                "TelegramChosenInlineResultEvent 缺少 inline_message_id，跳过编辑。"
            )
            return
        kwargs: dict = {
            "text": text[: TelegramPlatformEvent.MAX_MESSAGE_LENGTH],
            "inline_message_id": self.inline_message_id,
            "reply_markup": reply_markup,
        }
        if entities is not None:
            kwargs["entities"] = entities
        else:
            kwargs["parse_mode"] = parse_mode
        await self.client.edit_message_text(**kwargs)

    async def _resolve_inline_photo_media(self, image: Image) -> str | None:
        media = image.url or image.file
        if not media:
            return None
        if media.startswith(("http://", "https://")):
            return media
        if media.startswith("file://") or media.startswith("base64://"):
            try:
                return await image.register_to_file_service()
            except Exception as e:
                logger.warning(
                    "Cannot expose local inline image via file service; falling back to text edit: "
                    f"{e!s}"
                )
                return None
        if os.path.exists(media):
            try:
                return await image.register_to_file_service()
            except Exception as e:
                logger.warning(
                    "Cannot expose local inline image via file service; falling back to text edit: "
                    f"{e!s}"
                )
                return None
        # Telegram accepts existing file_id values when editing inline media.
        return media

    async def _edit_inline_photo_message(
        self,
        image: Image,
        caption: str | None,
        reply_markup=None,
    ) -> bool:
        if not self.inline_message_id:
            logger.debug(
                "TelegramChosenInlineResultEvent 缺少 inline_message_id，跳过媒体编辑。"
            )
            return False

        media = await self._resolve_inline_photo_media(image)
        if not media:
            return False

        try:
            await self.client.edit_message_media(
                inline_message_id=self.inline_message_id,
                media=InputMediaPhoto(
                    media=media,
                    caption=caption[: TelegramPlatformEvent.MAX_CAPTION_LENGTH]
                    if caption
                    else None,
                    has_spoiler=image.use_spoiler,
                ),
                reply_markup=reply_markup,
            )
            return True
        except Exception as e:
            logger.warning(f"编辑内联图片消息失败，回退到文本编辑: {e!s}")
            return False

    @staticmethod
    def _build_inline_stop_markup(existing_markup=None) -> InlineKeyboardMarkup:
        stop_button = InlineKeyboardButton(
            "Stop Agent",
            callback_data=TELEGRAM_INLINE_STOP_CALLBACK_DATA,
        )
        if existing_markup is None:
            return InlineKeyboardMarkup([[stop_button]])

        keyboard = [list(row) for row in existing_markup.inline_keyboard]
        keyboard.append([stop_button])
        return InlineKeyboardMarkup(keyboard)

    async def send_with_client(
        self, client: ExtBot, message_chain: MessageChain, user_name: str
    ) -> None:
        _ = client
        _ = user_name
        text = self._chain_to_text(message_chain)
        first_image = self._get_first_inline_image(message_chain)
        if not text.strip() and first_image is None:
            logger.warning("TelegramChosenInlineResultEvent 消息为空，跳过发送。")
            return

        reply_markup_keyboard = None
        if hasattr(message_chain, "reply_markup") and message_chain.reply_markup:
            try:
                keyboard_buttons = []
                for row in message_chain.reply_markup:
                    button_row = []
                    for button in row:
                        button_row.append(InlineKeyboardButton(**button))
                    keyboard_buttons.append(button_row)
                reply_markup_keyboard = InlineKeyboardMarkup(keyboard_buttons)
            except Exception as e:
                logger.warning(
                    f"Failed to convert reply_markup to InlineKeyboardMarkup: {e}"
                )

        if first_image is not None:
            media_caption = self._chain_to_text(message_chain)
            if await self._edit_inline_photo_message(
                first_image,
                media_caption or None,
                reply_markup=reply_markup_keyboard,
            ):
                return

        full_text, entities = self._build_inline_entities(text)
        try:
            await self._edit_inline_message(
                full_text,
                entities=entities,
                reply_markup=reply_markup_keyboard,
            )
        except Exception as e:
            logger.warning(f"编辑内联消息失败: {e!s}")

    async def send(self, message: MessageChain) -> None:
        await self.send_with_client(self.client, message, self.get_sender_id())
        await super().send(message)

    def _format_streaming_display(self, delta: str) -> str:
        """Format text for streaming display with proper quote structure.

        This method ensures consistent formatting during streaming edits,
        matching the final format structure.

        Args:
            delta: Current accumulated text from streaming.

        Returns:
            Text formatted with quote blocks for display.

        """
        # Strip markdown for the same reason as _format_inline_response:
        # incomplete markdown mid-stream is even more likely to create
        # malformed MarkdownV2 entities inside the blockquote wrapper.
        clean_delta = self._strip_markdown(delta)
        if not self.query:
            # No query, just format reply as blockquote
            lines = clean_delta.strip().split("\n")
            quoted_lines = ["回答：", ""]
            for line in lines:
                quoted_lines.append(f"> {line}" if line else ">")
            return "\n".join(quoted_lines)

        lines = clean_delta.strip().split("\n")
        quoted_lines = []

        # Query quote block (short, non-expandable)
        quoted_lines.append(f"> {self.query}")
        # Empty line separator to split quote blocks
        quoted_lines.append("")
        quoted_lines.append("回答：")
        quoted_lines.append("")

        # Reply blockquote (streaming content)
        for line in lines:
            quoted_lines.append(f"> {line}" if line else ">")

        return "\n".join(quoted_lines)

    async def send_streaming(self, generator, use_fallback: bool = False):
        if not self.inline_message_id:
            logger.warning(
                "TelegramChosenInlineResultEvent 缺少 inline_message_id，无法流式编辑消息。"
            )
            return

        delta = ""
        current_content = ""
        pending_image: Image | None = None
        pending_caption: str = ""
        last_edit_time = 0.0
        throttle_interval = 0.6
        loop = asyncio.get_running_loop()
        reply_markup_keyboard = None

        async for chain in generator:
            if not isinstance(chain, MessageChain):
                continue
            if chain.type == "break":
                delta += "\n"
                continue

            if hasattr(chain, "reply_markup") and chain.reply_markup:
                try:
                    keyboard_buttons = []
                    for row in chain.reply_markup:
                        button_row = []
                        for button in row:
                            button_row.append(InlineKeyboardButton(**button))
                        keyboard_buttons.append(button_row)
                    reply_markup_keyboard = InlineKeyboardMarkup(keyboard_buttons)
                except Exception as e:
                    logger.warning(
                        f"Failed to convert reply_markup to InlineKeyboardMarkup: {e}"
                    )

            chain_text = self._chain_to_text(chain)
            if chain_text:
                delta += chain_text
                pending_caption = delta.strip()

            if image := self._get_first_inline_image(chain):
                pending_image = image

            now = loop.time()
            if pending_image is None and now - last_edit_time >= throttle_interval:
                display_text, display_entities = self._build_inline_entities(delta)
                try:
                    await self._edit_inline_message(
                        display_text,
                        entities=display_entities,
                        reply_markup=reply_markup_keyboard,
                    )
                except Exception as e:
                    logger.debug(f"Streaming edit failed: {e!s}")
                current_content = delta
                last_edit_time = now

        try:
            if pending_image is not None:
                if await self._edit_inline_photo_message(
                    pending_image,
                    pending_caption or None,
                    reply_markup=reply_markup_keyboard,
                ):
                    await super().send_streaming(generator, use_fallback)
                    return

            if delta and current_content != delta:
                full_text, entities = self._build_inline_entities(delta)
                try:
                    await self._edit_inline_message(
                        full_text,
                        entities=entities,
                        reply_markup=reply_markup_keyboard,
                    )
                except Exception as e:
                    logger.warning(f"最终编辑失败: {e!s}")
        except Exception as e:
            logger.warning(f"编辑消息失败(streaming): {e!s}")

        await super().send_streaming(generator, use_fallback)


class TelegramCallbackQueryEvent(AstrMessageEvent):
    """Telegram 回调查询事件（键盘按钮点击）"""

    def __init__(
        self,
        callback_query_id: str,
        data: str,
        from_user_id: str,
        from_username: str | None,
        message: object | None,
        inline_message_id: str | None,
        platform_meta: PlatformMetadata,
        session_id: str,
        client: ExtBot,
    ) -> None:
        self.callback_query_id = callback_query_id
        """回调查询ID"""
        self.data = data
        """回调数据"""
        self.from_user_id = from_user_id
        """点击按钮的用户ID"""
        self.from_username = from_username
        """点击按钮的用户名"""
        self.message = message
        """消息对象（内联模式下可能为 None）"""
        self.inline_message_id = inline_message_id
        """内联消息ID（内联模式下使用）"""
        self.client = client

        # 创建一个虚拟的 AstrBotMessage 用于兼容性
        from astrbot.api.platform import AstrBotMessage, MessageType

        message_obj = AstrBotMessage()
        message_obj.message = [Plain(data)] if data else []
        message_obj.type = MessageType.FRIEND_MESSAGE
        nickname = from_username or from_user_id
        message_obj.sender = MessageMember(user_id=from_user_id, nickname=nickname)

        super().__init__(
            message_str=data,
            message_obj=message_obj,
            platform_meta=platform_meta,
            session_id=session_id,
        )

        # 阻止 LLM 处理：回调事件仅由插件处理
        self.call_llm = True

    def get_sender_id(self) -> str:
        return self.from_user_id

    def get_sender_name(self) -> str:
        return self.from_username or self.from_user_id

    def get_message_type(self) -> MessageType:
        return MessageType.FRIEND_MESSAGE

    def _chain_to_text(self, message_chain: MessageChain) -> str:
        text = message_chain.get_plain_text(with_other_comps_mark=True)
        return text.strip()

    async def answer_callback_query(
        self,
        text: str | None = None,
        show_alert: bool = False,
        url: str | None = None,
        cache_time: int | None = None,
    ) -> bool:
        """回应回调查询，可选显示提示消息

        Args:
            text: 显示给用户的文本
            show_alert: 是否显示为弹窗而非通知
            url: 打开的 URL
            cache_time: 缓存时间（秒）

        Returns:
            是否成功

        """
        try:
            await self.client.answer_callback_query(
                callback_query_id=self.callback_query_id,
                text=text,
                show_alert=show_alert,
                url=url,
                cache_time=cache_time,
            )
            return True
        except Exception as e:
            logger.warning(f"回应回调查询失败: {e!s}")
            return False

    async def set_inline_stop_requested_markup(self) -> None:
        if not self.inline_message_id:
            return
        try:
            await self.client.edit_message_reply_markup(
                inline_message_id=self.inline_message_id,
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton(
                                "Stop Requested",
                                callback_data="astrbot_inline_stop_requested",
                            )
                        ]
                    ]
                ),
            )
        except Exception as e:
            logger.warning(f"更新内联停止按钮失败: {e!s}")

    async def _edit_message(
        self, text: str, parse_mode: str | None = None, reply_markup=None
    ) -> None:
        """编辑消息（普通消息或内联消息）

        Args:
            text: 消息文本
            parse_mode: 解析模式（如 MarkdownV2）
            reply_markup: 内联键盘（InlineKeyboardMarkup）

        """
        safe_text = text
        if parse_mode is None:
            safe_text = TelegramPlatformEvent._split_message(text)[0]

        if self.inline_message_id:
            try:
                await self.client.edit_message_text(
                    text=safe_text,
                    inline_message_id=self.inline_message_id,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
                )
            except Exception as e:
                logger.warning(f"编辑内联消息失败: {e!s}")
        elif self.message:
            try:
                chat_id = self.message.chat.id
                message_id = self.message.message_id
                await self.client.edit_message_text(
                    text=safe_text,
                    chat_id=chat_id,
                    message_id=message_id,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
                )
            except Exception as e:
                logger.warning(f"编辑消息失败: {e!s}")
        else:
            logger.debug("TelegramCallbackQueryEvent 无可用消息，跳过编辑。")

    async def send_with_client(
        self, client: ExtBot, message_chain: MessageChain, user_name: str
    ) -> None:
        _ = client
        _ = user_name
        text = self._chain_to_text(message_chain)
        if not text:
            logger.warning("TelegramCallbackQueryEvent 消息为空，跳过发送。")
            return

        reply_markup_keyboard = None
        if hasattr(message_chain, "reply_markup") and message_chain.reply_markup:
            try:
                keyboard_buttons = []
                for row in message_chain.reply_markup:
                    button_row = []
                    for button in row:
                        button_row.append(InlineKeyboardButton(**button))
                    keyboard_buttons.append(button_row)
                reply_markup_keyboard = InlineKeyboardMarkup(keyboard_buttons)
            except Exception as e:
                logger.warning(
                    f"Failed to convert reply_markup to InlineKeyboardMarkup: {e}"
                )

        prepared_chunks = TelegramPlatformEvent._prepare_text_chunks(text)
        if not prepared_chunks:
            return
        formatted_text, plain_text = prepared_chunks[0]
        try:
            await self._edit_message(
                formatted_text,
                parse_mode="MarkdownV2" if plain_text is not None else None,
                reply_markup=reply_markup_keyboard,
            )
        except Exception as e:
            logger.warning(f"Markdown转换失败，使用普通文本: {e!s}")
            await self._edit_message(text, reply_markup=reply_markup_keyboard)

    async def send(self, message: MessageChain) -> None:
        await self.send_with_client(self.client, message, self.get_sender_id())
        await super().send(message)

    async def send_streaming(self, generator, use_fallback: bool = False):
        if not self.message and not self.inline_message_id:
            logger.warning(
                "TelegramCallbackQueryEvent 缺少 message 和 inline_message_id，无法流式编辑消息。"
            )
            return

        delta = ""
        current_content = ""
        last_edit_time = 0.0
        throttle_interval = 0.6
        loop = asyncio.get_running_loop()
        reply_markup_keyboard = None

        async for chain in generator:
            if not isinstance(chain, MessageChain):
                continue
            if chain.type == "break":
                delta += "\n"
                continue

            if hasattr(chain, "reply_markup") and chain.reply_markup:
                try:
                    keyboard_buttons = []
                    for row in chain.reply_markup:
                        button_row = []
                        for button in row:
                            button_row.append(InlineKeyboardButton(**button))
                        keyboard_buttons.append(button_row)
                    reply_markup_keyboard = InlineKeyboardMarkup(keyboard_buttons)
                except Exception as e:
                    logger.warning(
                        f"Failed to convert reply_markup to InlineKeyboardMarkup: {e}"
                    )

            delta += chain.get_plain_text(with_other_comps_mark=True)
            now = loop.time()
            if now - last_edit_time >= throttle_interval:
                await self._edit_message(delta)
                current_content = delta
                last_edit_time = now

        try:
            if delta and current_content != delta:
                prepared_chunks = TelegramPlatformEvent._prepare_text_chunks(delta)
                if prepared_chunks:
                    formatted_text, plain_text = prepared_chunks[0]
                    await self._edit_message(
                        formatted_text,
                        parse_mode="MarkdownV2" if plain_text is not None else None,
                        reply_markup=reply_markup_keyboard,
                    )
        except Exception as e:
            logger.warning(f"编辑消息失败(streaming): {e!s}")

        await super().send_streaming(generator, use_fallback)
