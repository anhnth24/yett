"""FakeProvider — provider deterministic cho test/dev (spec §3.1).

Kịch bản trả lời được lập trình sẵn (list ChatResult), để test core loop offline,
không gọi mạng. Đây là cách chứng minh no-egress trong test.
"""

from __future__ import annotations

from yett.provider.base import ChatResult, Message, ToolCall, ToolSchema, Usage


class FakeProvider:
    def __init__(self, script: list[ChatResult], model: str = "fake-1") -> None:
        self._script = list(script)
        self._model = model
        self.calls: list[list[Message]] = []

    def name(self) -> str:
        return "fake"

    def default_model(self) -> str:
        return self._model

    async def chat(
        self, messages: list[Message], tools: list[ToolSchema], *, stream: bool = False
    ) -> ChatResult:
        self.calls.append(list(messages))
        if not self._script:
            return ChatResult(
                text="(hết kịch bản)", tool_calls=[], usage=Usage(0, 0),
                stop_reason="end_turn", raw_model=self._model,
            )
        return self._script.pop(0)


def text_result(text: str, *, tokens: tuple[int, int] = (10, 5)) -> ChatResult:
    return ChatResult(
        text=text, tool_calls=[], usage=Usage(tokens[0], tokens[1]),
        stop_reason="end_turn", raw_model="fake-1",
    )


def tool_result(call_id: str, name: str, args: dict, *, tokens: tuple[int, int] = (10, 5)) -> ChatResult:
    return ChatResult(
        text=None, tool_calls=[ToolCall(call_id, name, args)], usage=Usage(tokens[0], tokens[1]),
        stop_reason="tool_use", raw_model="fake-1",
    )
