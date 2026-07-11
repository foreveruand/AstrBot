from types import SimpleNamespace

import pytest

from astrbot.core.agent.response import AgentResponse
from astrbot.core.astr_agent_run_util import run_agent
from astrbot.core.message.components import Json
from astrbot.core.message.message_event_result import MessageChain


class _FakeEvent:
    """Minimal event surface used by the agent stream bridge."""
    def __init__(self, supports_status_update: bool = False):
        self.sent_messages: list[str] = []
        self.updated_messages: list[str] = []
        self.clear_status_calls = 0
        self.trace = SimpleNamespace(record=lambda *args, **kwargs: None)
        self._result = None
        self._supports_status_update = supports_status_update

    def is_stopped(self) -> bool:
        return False

    def get_extra(self, key: str):
        _ = key
        return None

    def get_platform_id(self) -> str:
        return "test"

    def get_platform_name(self) -> str:
        return "test"

    async def send(self, message: MessageChain) -> None:
        self.sent_messages.append(message.get_plain_text())

    def supports_tool_call_status_update(self) -> bool:
        return self._supports_status_update

    async def update_tool_call_status(self, message: MessageChain) -> bool:
        self.updated_messages.append(message.get_plain_text())
        return len(self.updated_messages) == 1

    def clear_tool_call_status(self) -> None:
        self.clear_status_calls += 1

    def set_result(self, result) -> None:
        self._result = result

    def clear_result(self) -> None:
        self._result = None


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


class _FakeRunner:
    def __init__(
        self,
        event: _FakeEvent,
        responses: list[SimpleNamespace],
        streaming: bool = False,
    ):
        self.run_context = SimpleNamespace(context=SimpleNamespace(event=event))
        self.responses = responses
        self.streaming = streaming
        self.req = None
        self._done = False

    def done(self) -> bool:
        return self._done

    def request_stop(self) -> None:
        pass

    async def step(self):
        for response in self.responses:
            yield response
        self._done = True


def _tool_call(name: str, call_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        type="tool_call",
        data={"chain": MessageChain(chain=[Json(data={"id": call_id, "name": name})])},
    )


@pytest.mark.asyncio
async def test_run_agent_sends_merged_tool_status_before_next_llm_message():
    event = _FakeEvent()
    runner = _FakeRunner(
        event,
        [
            _tool_call("astrbot_execute_shell", "call-1"),
            _tool_call("astrbot_execute_shell", "call-2"),
            _tool_call("astrbot_execute_python", "call-3"),
            _tool_call("astrbot_execute_python", "call-4"),
            SimpleNamespace(
                type="llm_result",
                data={"chain": MessageChain().message("下一条 LLM 消息")},
            ),
        ],
    )

    output = [chain async for chain in run_agent(runner)]

    assert event.sent_messages == [
        "🔨 调用工具: astrbot_execute_shell 2次\n"
        "🔨 调用工具: astrbot_execute_python 2次"
    ]
    assert [chain.get_plain_text() for chain in output] == ["下一条 LLM 消息"]
    assert event.clear_status_calls == 1


@pytest.mark.asyncio
async def test_run_agent_updates_editable_tool_status_for_each_call():
    event = _FakeEvent(supports_status_update=True)
    runner = _FakeRunner(
        event,
        [
            _tool_call("astrbot_execute_shell", "call-1"),
            _tool_call("astrbot_execute_shell", "call-2"),
            _tool_call("astrbot_execute_python", "call-3"),
            SimpleNamespace(
                type="llm_result",
                data={"chain": MessageChain().message("下一条 LLM 消息")},
            ),
        ],
    )

    async for _ in run_agent(runner):
        pass

    assert event.sent_messages == []
    assert event.updated_messages == [
        "🔨 调用工具: astrbot_execute_shell",
        "🔨 调用工具: astrbot_execute_shell 2次",
        "🔨 调用工具: astrbot_execute_shell 2次\n🔨 调用工具: astrbot_execute_python",
    ]
    assert event.clear_status_calls == 1


@pytest.mark.asyncio
async def test_run_agent_flushes_merged_tool_status_before_streaming_delta():
    event = _FakeEvent()
    runner = _FakeRunner(
        event,
        [
            _tool_call("astrbot_execute_shell", "call-1"),
            _tool_call("astrbot_execute_shell", "call-2"),
            SimpleNamespace(
                type="streaming_delta",
                data={"chain": MessageChain().message("流式回复")},
            ),
        ],
        streaming=True,
    )

    output = [chain async for chain in run_agent(runner)]

    assert event.sent_messages == ["🔨 调用工具: astrbot_execute_shell 2次"]
    assert [chain.type for chain in output] == ["break", None]
    assert [chain.get_plain_text() for chain in output] == ["", "流式回复"]
