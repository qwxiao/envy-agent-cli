"""模块5 · MCP 客户端：握手、发现、调用。

一次会话的完整生命周期：

```
initialize → notifications/initialized → tools/list → tools/call …
                                       ↘ resources/read …
```

**这一层不碰治理**：权限、审批、超时熔断、审计全部归模块3 的 `ToolRuntime`。
MCP 客户端只回答两个问题——"远端有哪些工具"和"调它返回了什么"。

**两种失败必须分清**（这条贯穿全模块）：

| 情形 | 表现 | 谁处理 |
|---|---|---|
| 根本没谈成（连不上、超时、协议错） | 抛 `McpError` | 桥接层翻成 `ToolError` 回灌模型 |
| 谈成了但工具自己失败 | 返回 `is_error=True` 的 `McpToolResult` | 与内置工具失败走**同一条路** |

把它们混成一个，模型就分不清"该换个工具试试"还是"该重试一次"。
"""

from typing import Any, Iterator

from envy_agent_cli import __version__
from envy_agent_cli.mcp.transport import McpTransport, build_transport
from envy_agent_cli.mcp.types import (
    McpError,
    McpErrorCode,
    McpServerSpec,
    McpTool,
    McpToolResult,
)

#: 我们声明的协议版本。服务端可以回它自己支持的版本，**客户端宽容接受**——
#: 版本不一致就拒绝连接，等于把"服务端稍旧"变成不可用，代价大于收益。
PROTOCOL_VERSION = "2025-06-18"

CLIENT_NAME = "envy-agent-cli"

#: `tools/list` 的翻页安全上限。服务端给了 cursor 却永远给不出新内容时，
#: 靠它兜底而不是死循环。
MAX_TOOL_PAGES = 20

#: JSON-RPC 的"参数无效"。**很多 server 用它表示"没有这个工具"**——
#: 规范没有专门的"工具不存在"码，这是事实约定。
_RPC_INVALID_PARAMS = -32602

#: 工具没有参数时的兜底 schema。给 `None` 会让注册表投影出非法的模型契约。
_EMPTY_SCHEMA: dict = {"type": "object", "properties": {}}


def render_content(blocks: list) -> str:
    """把 `tools/call` 返回的内容块拼成纯文本。

    非文本块（image / audio）**只留占位说明**——本项目的模型契约是纯文本的，
    展开 base64 只会把上下文塞爆。这是刻意的降级：宁可模型知道"这里有个图但没给它看"，
    也不要它收到一坨看不懂的字符。
    """
    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            parts.append(str(block.get("text") or ""))
        elif kind == "resource":
            # 内嵌资源：有文本就给文本，否则给一行说明
            resource = block.get("resource") or {}
            text = resource.get("text") if isinstance(resource, dict) else None
            uri = resource.get("uri") if isinstance(resource, dict) else ""
            parts.append(str(text) if text else f"[resource {uri}：非文本内容未展开]")
        else:
            parts.append(f"[{kind or 'unknown'} 类型内容未展开]")
    return "\n".join(p for p in parts if p)


class McpClient:
    """一个 server 的会话。

    用 `with` 管生命周期——握手与关闭都涉及外部资源，漏掉任一边都会泄漏：

        with McpClient(spec) as client:
            for tool in client.list_tools():
                ...
    """

    def __init__(self, spec: McpServerSpec, *, transport: McpTransport | None = None) -> None:
        """
        Args:
            transport: 可注入的传输（测试用）。为 `None` 时按声明自建。
        """
        self.spec = spec
        self.server = spec.name
        self._transport = transport if transport is not None else build_transport(spec)
        self._server_info: dict = {}
        self._capabilities: dict = {}

    # ---------- 生命周期 ----------

    def __enter__(self) -> "McpClient":
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def connect(self) -> None:
        """建立通道并完成握手。

        ⚠️ `initialize` 之后**必须再发一条 `notifications/initialized`**，
        服务端才算认为会话就绪。少了它，有些实现会对后续请求直接报"未初始化"——
        而报错信息通常不会告诉你是漏了这条通知。
        """
        self._transport.start()
        try:
            result = self._transport.request("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},        # 我们只消费远端能力，不提供任何能力
                "clientInfo": {"name": CLIENT_NAME, "version": __version__},
            })
        except McpError:
            self._transport.close()        # 握手失败就别留着半个连接
            raise

        self._server_info = result.get("serverInfo") if isinstance(result.get("serverInfo"), dict) else {}
        self._capabilities = result.get("capabilities") if isinstance(result.get("capabilities"), dict) else {}
        self._transport.notify("notifications/initialized")

    def close(self) -> None:
        self._transport.close()

    @property
    def closed(self) -> bool:
        return self._transport.closed

    @property
    def server_info(self) -> dict:
        """远端自报的身份（name / version）。握手前是空字典。"""
        return dict(self._server_info)

    @property
    def capabilities(self) -> dict:
        """远端自报的能力（tools / resources / prompts …）。握手前是空字典。

        用它可以**先问再调**：没声明 `resources` 的 server 就不用去试 `resources/read`。
        """
        return dict(self._capabilities)

    def supports(self, capability: str) -> bool:
        return capability in self._capabilities

    # ---------- 能力调用 ----------

    def list_tools(self) -> list[McpTool]:
        """拉取远端工具清单（自动翻页）。

        形状不对的条目**静默跳过**：一个 server 里混进一条坏数据，
        不该让整个 server 的工具全部不可用。
        """
        tools: list[McpTool] = []
        cursor: str | None = None
        for _ in range(MAX_TOOL_PAGES):
            params = {"cursor": cursor} if cursor else {}
            result = self._transport.request("tools/list", params)
            raw_tools = result.get("tools")
            for raw in raw_tools if isinstance(raw_tools, list) else []:
                tool = self._to_tool(raw)
                if tool is not None:
                    tools.append(tool)
            next_cursor = result.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                break
            cursor = next_cursor
        return tools

    def call_tool(self, name: str, arguments: dict | None = None) -> McpToolResult:
        """调用远端工具。

        Raises:
            McpError: 传输失败，或工具不存在。
                工具**执行失败不算异常**——走返回值的 `is_error`，
                与内置工具的失败路径保持一致。
        """
        try:
            result = self._transport.request("tools/call", {
                "name": name,
                "arguments": arguments or {},
            })
        except McpError as exc:
            if exc.rpc_code == _RPC_INVALID_PARAMS:
                raise McpError(
                    McpErrorCode.TOOL_NOT_FOUND,
                    f"远端没有工具 {name!r}（{exc.message}）",
                    server=self.server,
                    rpc_code=exc.rpc_code,
                ) from exc
            raise

        blocks = result.get("content")
        return McpToolResult(
            text=render_content(blocks if isinstance(blocks, list) else []),
            is_error=bool(result.get("isError")),
            raw=result,
        )

    def read_resource(self, uri: str) -> str:
        """读一个资源（`resources/read`）。

        ⚠️ **资源不进工具集**——它是"取数据"不是"做事情"。
        把每个 resource 都变成一个工具，会让模型面对一堆同质的 getter，
        选错率显著上升。什么时候读、读完怎么用，是上层的事。
        """
        result = self._transport.request("resources/read", {"uri": uri})
        contents = result.get("contents")
        parts: list[str] = []
        for item in contents if isinstance(contents, list) else []:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            parts.append(str(text) if isinstance(text, str) else f"[{item.get('mimeType') or 'binary'} 资源未展开]")
        return "\n".join(parts)

    def list_resources(self) -> list[dict]:
        """列出可用资源（未声明 `resources` 能力的 server 返回空列表，不报错）。"""
        if not self.supports("resources"):
            return []
        result = self._transport.request("resources/list", {})
        resources = result.get("resources")
        return [r for r in resources if isinstance(r, dict)] if isinstance(resources, list) else []

    # ---------- 内部 ----------

    def _to_tool(self, raw: Any) -> McpTool | None:
        """远端条目 → `McpTool`。缺名字的直接丢掉（注册表以名字为键，没有名字无法注册）。"""
        if not isinstance(raw, dict):
            return None
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip():
            return None

        annotations = raw.get("annotations")
        hint = annotations.get("readOnlyHint") if isinstance(annotations, dict) else None
        schema = raw.get("inputSchema")

        return McpTool(
            server=self.server,
            name=name.strip(),
            description=str(raw.get("description") or "").strip(),
            input_schema=schema if isinstance(schema, dict) else dict(_EMPTY_SCHEMA),
            # ⚠️ 只认真正的 bool。远端给 "true" 这类字符串时当没声明——
            # 信任边界上宁可保守（当成不可信），不能靠类型转换猜意图。
            read_only_hint=hint if isinstance(hint, bool) else None,
        )


def iter_clients(specs: list[McpServerSpec]) -> Iterator[tuple[McpServerSpec, McpClient | None, McpError | None]]:
    """逐个连接，**一个失败不影响其它**。

    产出 `(声明, 客户端或None, 错误或None)` 三元组，让调用方自己决定
    对失败的 server 是记一笔还是直接中止。这里不做决定——
    桥接层才知道"能不能少一个 server 继续跑"。
    """
    for spec in specs:
        client = McpClient(spec)
        try:
            client.connect()
        except McpError as exc:
            yield spec, None, exc
            continue
        yield spec, client, None
