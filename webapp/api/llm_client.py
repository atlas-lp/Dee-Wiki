"""
webapp/api/llm_client.py — Unified LLM client for Claude (Anthropic) and Nebius (OpenAI-compatible).

Normalises tool-call format, message history format, and streaming so that
run_wiki_llm / run_main_llm_streaming / _do_wiki_update stay provider-agnostic.
"""

import json
from dataclasses import dataclass


def _is_reasoning_unsupported(exc: Exception) -> bool:
    """Return True when the API error indicates reasoning_effort is not accepted."""
    msg = str(exc).lower()
    return any(k in msg for k in (
        "reasoning_effort",
        "unsupported parameter",
        "unknown field",
        "extra inputs are not permitted",
        "invalid_request_error",
    ))


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict  # already parsed from JSON


@dataclass
class NormalizedResponse:
    stop_reason: str          # "tool_use" | "end_turn"
    tool_calls: list          # list[ToolCall]
    text: str                 # concatenated text content
    raw: object = None        # original SDK response object (used for Claude assistant turn)


class LLMClient:
    """
    Provider-agnostic LLM client supporting Claude (Anthropic SDK) and
    Nebius / any OpenAI-compatible endpoint.

    Tool definitions are always passed in Claude format:
        {"name": X, "description": Y, "input_schema": {...}}
    and converted internally for Nebius.
    """

    def __init__(
        self,
        provider: str,
        model: str,
        api_key: str,
        base_url: str = None,
    ):
        self.provider = provider
        self.model = model
        self._claude = None
        self._openai = None

        if provider == "claude":
            from anthropic import Anthropic
            self._claude = Anthropic(api_key=api_key)
        elif provider == "nebius":
            from openai import OpenAI
            self._openai = OpenAI(
                base_url=base_url or "https://api.tokenfactory.nebius.com/v1/",
                api_key=api_key,
            )
            # Tri-state: True = supported, False = not supported, None = untested yet
            self._reasoning_supported: bool | None = None
        else:
            raise ValueError(f"Unknown provider: {provider!r}. Must be 'claude' or 'nebius'.")

    # ------------------------------------------------------------------
    # Tool format conversion
    # ------------------------------------------------------------------

    @staticmethod
    def _to_nebius_tools(claude_tools: list) -> list:
        """Convert Claude tool defs → OpenAI/Nebius function format."""
        return [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t["input_schema"],
                },
            }
            for t in claude_tools
        ]

    # ------------------------------------------------------------------
    # Message history helpers (public — called by index2.py after tool exec)
    # ------------------------------------------------------------------

    def append_assistant_turn(self, messages: list, response: NormalizedResponse) -> None:
        """Append the assistant's turn to messages in the correct provider format."""
        if self.provider == "claude":
            messages.append({"role": "assistant", "content": response.raw.content})
        else:  # nebius / openai-compatible
            if response.tool_calls:
                messages.append({
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.input),
                            },
                        }
                        for tc in response.tool_calls
                    ],
                })
            else:
                messages.append({"role": "assistant", "content": response.text})

    def append_tool_results(
        self, messages: list, tool_calls: list, results: list
    ) -> None:
        """Append tool execution results to messages in the correct provider format."""
        if self.provider == "claude":
            messages.append({
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tc.id,
                        "content": res,
                    }
                    for tc, res in zip(tool_calls, results)
                ],
            })
        else:  # nebius — one message per tool result
            for tc, res in zip(tool_calls, results):
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "name": tc.name,
                    "content": res,
                })

    # ------------------------------------------------------------------
    # Core API: non-streaming completion with optional tools
    # ------------------------------------------------------------------

    def complete_with_tools(
        self,
        system: str,
        messages: list,
        tools: list,
        max_tokens: int = 3500,
        thinking: dict = None,
    ) -> NormalizedResponse:
        """
        Single-shot (non-streaming) completion. Returns NormalizedResponse.
        The caller loops until stop_reason != "tool_use".
        - `thinking` is honoured only for Claude; silently ignored for Nebius.
        - `tools=[]` omits the tools parameter entirely (avoids provider rejection).
        """
        if self.provider == "claude":
            return self._complete_claude(system, messages, tools, max_tokens, thinking)
        else:
            return self._complete_nebius(system, messages, tools, max_tokens)

    def _complete_claude(self, system, messages, tools, max_tokens, thinking):
        kwargs = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
        if thinking:
            kwargs["thinking"] = thinking

        raw = self._claude.messages.create(**kwargs)
        tool_calls = [
            ToolCall(id=b.id, name=b.name, input=b.input)
            for b in raw.content
            if b.type == "tool_use"
        ]
        text = "".join(b.text for b in raw.content if hasattr(b, "text"))
        return NormalizedResponse(
            stop_reason=raw.stop_reason,
            tool_calls=tool_calls,
            text=text,
            raw=raw,
        )

    def _nebius_kwargs(self, system, messages, tools, max_tokens, extra=None):
        """Build base kwargs dict for a Nebius call, injecting reasoning_effort when supported."""
        kwargs = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}] + messages,
            "max_tokens": max_tokens,
        }
        if tools:
            kwargs["tools"] = self._to_nebius_tools(tools)
        if extra:
            kwargs.update(extra)
        if self._reasoning_supported is not False:
            kwargs["reasoning_effort"] = "medium"
        return kwargs

    def _complete_nebius(self, system, messages, tools, max_tokens):
        kwargs = self._nebius_kwargs(system, messages, tools, max_tokens)
        try:
            raw = self._openai.chat.completions.create(**kwargs)
            self._reasoning_supported = True
        except Exception as e:
            if self._reasoning_supported is None and _is_reasoning_unsupported(e):
                print(f"[LLMClient] reasoning_effort not supported by {self.model!r} — disabling")
                self._reasoning_supported = False
                kwargs.pop("reasoning_effort", None)
                raw = self._openai.chat.completions.create(**kwargs)
            else:
                raise

        msg = raw.choices[0].message
        tc_list = msg.tool_calls or []
        tool_calls = [
            ToolCall(
                id=tc.id,
                name=tc.function.name,
                input=json.loads(tc.function.arguments or "{}"),
            )
            for tc in tc_list
        ]
        stop = "tool_use" if tool_calls else "end_turn"
        return NormalizedResponse(
            stop_reason=stop,
            tool_calls=tool_calls,
            text=msg.content or "",
            raw=raw,
        )

    # ------------------------------------------------------------------
    # Core API: streaming completion with optional tools
    # ------------------------------------------------------------------

    def stream_text(
        self,
        system: str,
        messages: list,
        tools: list = None,
        max_tokens: int = 4096,
    ):
        """
        Generator that yields:
            ("text", str)                  — one text chunk per token
            ("final", NormalizedResponse)  — exactly once, at the end

        The caller should iterate until the "final" sentinel and inspect
        NormalizedResponse.stop_reason / .tool_calls to decide whether to loop.
        """
        if self.provider == "claude":
            yield from self._stream_claude(system, messages, tools, max_tokens)
        else:
            yield from self._stream_nebius(system, messages, tools, max_tokens)

    def _stream_claude(self, system, messages, tools, max_tokens):
        kwargs = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools

        with self._claude.messages.stream(**kwargs) as stream:
            for chunk in stream.text_stream:
                yield ("text", chunk)
            final = stream.get_final_message()

        tool_calls = [
            ToolCall(id=b.id, name=b.name, input=b.input)
            for b in final.content
            if b.type == "tool_use"
        ]
        text = "".join(b.text for b in final.content if hasattr(b, "text"))
        yield (
            "final",
            NormalizedResponse(
                stop_reason=final.stop_reason,
                tool_calls=tool_calls,
                text=text,
                raw=final,
            ),
        )

    def _stream_nebius(self, system, messages, tools, max_tokens):
        kwargs = self._nebius_kwargs(system, messages, tools, max_tokens, extra={"stream": True})

        # Tool call deltas arrive fragmented; accumulate by index.
        accumulated_tcs: dict = {}
        last_chunk = None

        try:
            stream_iter = self._openai.chat.completions.create(**kwargs)
        except Exception as e:
            if self._reasoning_supported is None and _is_reasoning_unsupported(e):
                print(f"[LLMClient] reasoning_effort not supported by {self.model!r} — disabling")
                self._reasoning_supported = False
                kwargs.pop("reasoning_effort", None)
                stream_iter = self._openai.chat.completions.create(**kwargs)
            else:
                raise
        else:
            self._reasoning_supported = True

        for chunk in stream_iter:
            last_chunk = chunk
            delta = chunk.choices[0].delta if chunk.choices else None
            if not delta:
                continue

            if delta.content:
                yield ("text", delta.content)

            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    i = tc_delta.index
                    if i not in accumulated_tcs:
                        accumulated_tcs[i] = {"id": "", "name": "", "arguments": ""}
                    if tc_delta.id:
                        accumulated_tcs[i]["id"] = tc_delta.id
                    if tc_delta.function and tc_delta.function.name:
                        accumulated_tcs[i]["name"] = tc_delta.function.name
                    if tc_delta.function and tc_delta.function.arguments:
                        accumulated_tcs[i]["arguments"] += tc_delta.function.arguments

        tool_calls = [
            ToolCall(
                id=v["id"],
                name=v["name"],
                input=json.loads(v["arguments"] or "{}"),
            )
            for v in accumulated_tcs.values()
        ]
        stop = "tool_use" if tool_calls else "end_turn"
        yield (
            "final",
            NormalizedResponse(
                stop_reason=stop,
                tool_calls=tool_calls,
                text="",
                raw=last_chunk,
            ),
        )
