"""模块1 · 模型适配：契约（ChatModel）+ OpenAI 兼容实现。

**Adapter 是契约，不是适配层**——Loop 只依赖 `ChatModel` Protocol，不依赖任何厂商 SDK。
换模型 = 构造一个新的 Adapter 对象再注入，**Loop 代码一个字不改**。
这是开闭原则在模型层的落地，和"加工具不改 Loop"是同一设计思想的两次应用。

职责（本层做什么）：
- 拼请求体、发请求（**进流先查 status_code**）；
- 把厂商差异**归一化**成统一事件（哪个字段是正文、哪个是思维链、finish_reason 怎么映射）；
- 判定错误码与 `retryable`（**只有它掌握完整上下文**：状态码、错误体、尝试次数）。

不做什么：
- ❌ 不重试、不路由、不降级（重试归 `retry.py`，路由归 `factory.py`，降级归 Loop）
- ❌ 不渲染、不拼装工具调用、不判断任务是否结束
- ❌ 不写盘
"""

import json
from typing import Any, Callable, Iterator, Protocol, TypedDict, runtime_checkable

import httpx

from envy_agent_cli.llm.events import (
    AnyEvent,
    Error,
    LLMErrorCode,
    MessageEnd,
    ReasoningDelta,
    StopReason,
    TextDelta,
    ToolCallDelta,
    Usage,
    map_http_status,
)
from envy_agent_cli.llm.params import ChatParams
from envy_agent_cli.llm.transport import iter_sse_json
from envy_agent_cli.trace import TraceContext


class ChatMessage(TypedDict, total=False):
    """发给模型的消息。字段与 OpenAI 兼容协议一致。"""

    role: str
    content: str
    tool_calls: list[dict]
    tool_call_id: str


@runtime_checkable
class ChatModel(Protocol):
    """所有模型适配器必须满足的契约（模型契约的"对面"：这是给 Loop 看的接口）。"""

    name: str

    def stream_chat(
        self,
        messages: list[ChatMessage],
        tools: list[dict] | None = None,
        params: ChatParams = ChatParams(),
        trace: TraceContext | None = None,
    ) -> Iterator[AnyEvent]:
        """流式对话：逐条 yield 事件，直到 MessageEnd 或 Error。

        Args:
            messages: 完整对话历史（模型无状态，每轮全量重发）。
            tools: 模型契约列表（只有 name/description/input_schema 三样）。
            params: 采样参数（显式对象，可序列化、可版本化）。
            trace: 追踪上下文。Adapter 只用它两件事：**给事件打 span、记录本段耗时**。
                  不写盘、不决策——落盘归审计层。

        Yields:
            TextDelta / ReasoningDelta / ToolCallDelta / MessageEnd / Usage / Error
        """
        ...


#: 厂商默认入口。新增厂商 = 在这里加一行 + 在 factory 注册，Loop 零改动。
PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
    "deepseek": {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
}

#: 可选：把厂商特有字段名映射进来（默认按 OpenAI 兼容协议取 content / reasoning_content）
_TEXT_KEYS = ("content",)
_REASONING_KEYS = ("reasoning_content", "reasoning")


class _StreamNormalizer:
    """一次流的归一化状态机：chunk → 事件。

    有状态的原因只有一个——`ToolCallDelta.is_first` 需要记住"这个 index 见过没有"。
    显式状态机比散在函数里的隐式判断可靠，也更好测。
    """

    def __init__(self) -> None:
        self._seen_indexes: set[int] = set()

    def normalize(self, chunk: dict) -> list[AnyEvent]:
        events: list[AnyEvent] = []

        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}

            for key in _TEXT_KEYS:
                text = delta.get(key)
                if text:
                    events.append(TextDelta(text))
            for key in _REASONING_KEYS:
                reasoning = delta.get(key)
                if reasoning:
                    events.append(ReasoningDelta(reasoning))
                    break

            for tool_call in delta.get("tool_calls") or []:
                index = tool_call.get("index")
                index = 0 if index is None else index
                is_first = index not in self._seen_indexes
                self._seen_indexes.add(index)
                function = tool_call.get("function") or {}
                events.append(ToolCallDelta(
                    index=index,
                    is_first=is_first,
                    call_id=tool_call.get("id"),
                    name=function.get("name"),
                    arguments=function.get("arguments") or "",
                ))

            finish_reason = choice.get("finish_reason")
            if finish_reason:
                events.append(MessageEnd(map_finish_reason(finish_reason)))

        usage = chunk.get("usage")
        if usage:
            events.append(Usage(
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                total_tokens=usage.get("total_tokens", 0),
            ))
        return events


def map_finish_reason(reason: str) -> StopReason:
    """厂商的 finish_reason → 统一的 stop_reason（归一化只做一次，别让每层各写一份）。

    ⚠️ `length` → `max_tokens` 是**失败信号**，不是正常结束。
    """
    if reason in {"tool_calls", "tool_use"}:
        return "tool_use"
    if reason == "length":
        return "max_tokens"
    if reason == "content_filter":
        return "stop_sequence"
    return "end_turn"


class OpenAICompatAdapter:
    """满足 `ChatModel` 契约的通用适配器（OpenAI 兼容协议）。

    国内多数厂商（DeepSeek / GLM / Kimi / Step…）都提供 OpenAI 兼容协议，
    换厂商 = 换 `base_url` + 模型名 + 凭证，归一化逻辑完全复用本类。

    Args:
        provider: 厂商标识（用于默认 base_url / 模型名）。
        api_key: 凭证。**不再有全局 API_KEY**——凭证属于 Adapter 实例。
        model: 模型名，缺省用 `PROVIDER_DEFAULTS`。
        base_url: 覆盖默认入口。
        timeout: 读超时。LLM 请求要按分钟级设（首 token 延迟 + 长生成都在"读"时间里）。
        chunk_source: 可注入的传输函数（测试用；默认走 httpx）。
    """

    def __init__(
        self,
        provider: str,
        api_key: str,
        model: str | None = None,
        base_url: str | None = None,
        *,
        timeout: float = 120.0,
        chunk_source: Callable[[dict], Iterator[str]] | None = None,
    ) -> None:
        defaults = PROVIDER_DEFAULTS.get(provider, {})
        self.name = provider
        self.model = model or defaults.get("model", "")
        self.base_url = base_url or defaults.get("base_url", "")
        self._api_key = api_key
        self.timeout = timeout
        self._chunk_source = chunk_source

    # ---------- 契约实现 ----------

    def stream_chat(
        self,
        messages: list[ChatMessage],
        tools: list[dict] | None = None,
        params: ChatParams = ChatParams(),
        trace: TraceContext | None = None,
    ) -> Iterator[AnyEvent]:
        body = self._request_body(messages, tools, params)
        normalizer = _StreamNormalizer()

        # 注意：_open_stream 是生成器，连开流失败也在迭代时才抛——所以统一在一个 try 里兜。
        try:
            for chunk in iter_sse_json(self._open_stream(body)):
                yield from normalizer.normalize(chunk)
        except StreamOpenError as exc:
            yield Error.from_code(exc.code, str(exc), exc.status_code)
        except httpx.TimeoutException:
            yield Error.from_code(LLMErrorCode.TIMEOUT, f"请求超时（>{self.timeout}s）")
        except httpx.RequestError as exc:
            yield Error.from_code(LLMErrorCode.SERVER_ERROR, f"连接失败: {exc}")

    # ---------- 内部 ----------

    def _request_body(
        self,
        messages: list[ChatMessage],
        tools: list[dict] | None,
        params: ChatParams,
    ) -> dict:
        """拼请求体。`include_usage` 让服务端在流末回真实 token 用量。"""
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            **params.to_body_fields(),
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        return body

    def _open_stream(self, body: dict) -> Iterator[str]:
        """发请求并返回文本分片迭代器。**进流先查 status_code**。

        错误响应根本不是 SSE 格式——不查状态码，程序会把一坨报错文本当成
        "没有正文的正常流"静默结束。Agent 链路长，一处静默失败会把 debug 成本放大十倍。
        """
        if self._chunk_source is not None:
            # 注意：本函数是生成器，这里必须 yield from —— 写 `return iter(...)`
            # 会变成"生成器立即结束"，一条事件都吐不出来（实现时踩过）。
            yield from self._chunk_source(body)
            return

        response = httpx.stream(
            "POST", f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json=body, timeout=self.timeout,
        )
        with response as resp:
            if resp.status_code != 200:
                code = map_http_status(resp.status_code)
                detail = resp.read().decode(errors="replace")[:500]  # 错误体也要脱敏截断
                raise StreamOpenError(code, f"HTTP {resp.status_code}: {detail}", resp.status_code)
            yield from resp.iter_text()


class StreamOpenError(Exception):
    """开流阶段的错误（带错误码与状态码），由调用方转成 `Error` 事件。"""

    def __init__(self, code: LLMErrorCode, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
