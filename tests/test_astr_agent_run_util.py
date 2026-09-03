import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot.core.agent.response import AgentResponse
from astrbot.core.astr_agent_run_util import _simulated_stream_tts, run_agent
from astrbot.core.message.components import Json
from astrbot.core.message.message_event_result import MessageChain


class _FakeEvent:
    """Minimal event surface used by the agent stream bridge."""

    def __init__(self) -> None:
        self.trace = SimpleNamespace(record=lambda *args, **kwargs: None)
        self.send = AsyncMock()

    def is_stopped(self) -> bool:
        return False

    def get_extra(self, key: str):
        del key
        return None

    def get_platform_name(self) -> str:
        return "test"


class _StreamingErrorRunner:
    """Agent runner that finishes with one provider error response."""

    streaming = True
    req = None

    def __init__(self, error_text: str) -> None:
        self.error_text = error_text
        self.finished = False
        self.run_context = SimpleNamespace(context=SimpleNamespace(event=_FakeEvent()))

    async def step(self):
        self.finished = True
        yield AgentResponse(
            type="err",
            data={"chain": MessageChain().message(self.error_text)},
        )

    def done(self) -> bool:
        return self.finished


class _MalformedStreamingErrorRunner(_StreamingErrorRunner):
    """Agent runner that returns an invalid provider error payload."""

    async def step(self):
        self.finished = True
        yield AgentResponse(type="err", data={})


class _ToolCallStreamingRunner:
    """Agent runner that emits LLM text before and after a tool call."""

    streaming = True
    req = None

    def __init__(self) -> None:
        self.finished = False
        self.event = _FakeEvent()
        self.run_context = SimpleNamespace(context=SimpleNamespace(event=self.event))

    async def step(self):
        yield AgentResponse(
            type="streaming_delta",
            data={"chain": MessageChain().message("Before tool.")},
        )
        yield AgentResponse(
            type="tool_call",
            data={
                "chain": MessageChain(
                    chain=[Json(data={"id": "call-1", "name": "search"})],
                ),
            },
        )
        yield AgentResponse(
            type="streaming_delta",
            data={"chain": MessageChain().message("After tool.")},
        )
        self.finished = True

    def done(self) -> bool:
        return self.finished


@pytest.mark.asyncio
async def test_run_agent_forwards_streaming_provider_error():
    error_text = (
        "LLM 响应错误: Not found the model k2.7-code-highspeed or Permission denied"
    )
    runner = _StreamingErrorRunner(error_text)

    chains = [chain async for chain in run_agent(runner)]

    assert len(chains) == 1
    assert chains[0].get_plain_text() == error_text


@pytest.mark.asyncio
async def test_run_agent_replaces_malformed_streaming_provider_error():
    runner = _MalformedStreamingErrorRunner("unused")

    chains = [chain async for chain in run_agent(runner)]

    assert len(chains) == 1
    assert chains[0].get_plain_text() == "Error occurred during AI execution."


@pytest.mark.asyncio
async def test_run_agent_breaks_stream_at_tool_call_when_tool_status_is_hidden():
    runner = _ToolCallStreamingRunner()

    chains = [chain async for chain in run_agent(runner, show_tool_use=False)]

    assert [chain.type for chain in chains] == [None, "break", None]
    assert [chain.get_plain_text() for chain in chains] == [
        "Before tool.",
        "",
        "After tool.",
    ]
    runner.event.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_simulated_stream_tts_leaves_audio_for_deferred_cleanup(tmp_path):
    audio_path = tmp_path / "speech.wav"
    audio_path.write_bytes(b"audio")

    class _TTSProvider:
        async def get_audio(self, text: str) -> str:
            assert text == "hello"
            return str(audio_path)

    text_queue: asyncio.Queue[str | None] = asyncio.Queue()
    audio_queue: asyncio.Queue[bytes | tuple[str, bytes] | None] = asyncio.Queue()
    await text_queue.put("hello")
    await text_queue.put(None)

    await _simulated_stream_tts(_TTSProvider(), text_queue, audio_queue)

    assert await audio_queue.get() == ("hello", b"audio")
    assert await audio_queue.get() is None
    assert audio_path.exists()
