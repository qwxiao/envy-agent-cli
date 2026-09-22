"""模块5 · 桥接层：把远端工具变成"注册表里的一条记录"。

这一层是整个 MCP 接入的**唯一投影点**——远端契约（`McpTool`）在这里变成内部契约
（`RegisteredTool`），之后的一切（鉴权、审批、超时、熔断、审计）全部复用模块3，
**一行都不用改**。这是 ARCHITECTURE.md 里那句架构承诺的落地：

> 外部 MCP 工具与内置工具走同一个执行入口（`ToolRuntime.execute`），
> 届时不改 Loop、不改 Runtime。

**信任边界**（本层最重要的一条规则）：

| 远端声明 | read_only | risk | 审批 |
|---|---|---|---|
| `readOnlyHint=True` | ✅ True | LOW | 不问 |
| 其它任何情况（含未声明） | ❌ False | MEDIUM | **必问** |

判据是"**谁来声明**"：内置工具是我们自己写的，声明可信；
远端工具是别人写的，它说自己是只读**只是提示，不是保证**。
所以只有它明确说只读时才放宽，其余一律按"可能有副作用"处理——
Unknown 比 False 更危险，未声明不能当没问题。
"""

from typing import Any, Callable, NoReturn

from envy_agent_cli.mcp.client import McpClient, iter_clients
from envy_agent_cli.mcp.types import McpError, McpErrorCode, McpServerSpec, McpTool
from envy_agent_cli.tools.registry import register
from envy_agent_cli.tools.spec import Permission, RegisteredTool, Risk, ToolSpec


class McpToolFailure(RuntimeError):
    """远端工具执行失败（应答里的 `isError=true`）。

    继承 `RuntimeError` 而**不是** `OSError`，是为了让 `ToolRuntime._classify`
    把它归到 `UNKNOWN`（不重试）。若继承 `OSError` 会被归为 `UPSTREAM_ERROR` 而进入重试——
    但远端已经明确说这次执行失败了，重试同一个调用只会再失败一次。
    """


def translate_error(exc: McpError) -> NoReturn:
    """MCP 错误 → Python 标准异常，让 Runtime 的分类逻辑原样工作。

    这是桥接层存在的核心理由之一：**Runtime 不该知道 MCP 的存在**。
    与其在 `_classify` 里加一个 `isinstance(exc, McpError)` 分支
    （那会让工具执行层反向依赖协议客户端层），不如在这里把语义翻译成它已经认识的东西。

    映射表：

    | MCP 码 | 翻成 | Runtime 归类 | 可重试 |
    |---|---|---|---|
    | `TIMEOUT` | `TimeoutError` | `TIMEOUT` | ✅ |
    | `CONNECT_FAILED` / `TRANSPORT_CLOSED` | `ConnectionError` | `UPSTREAM_ERROR` | ✅ |
    | `PROTOCOL_ERROR` / `TOOL_NOT_FOUND` / `CALL_FAILED` | `RuntimeError` | `UNKNOWN` | ❌ |

    ⚠️ 可重试**不等于会重试**——Runtime 还有第二条纪律：只重试只读工具。
    MCP 工具默认非只读，所以这张表的实际收益是**错误码准确**（审计能分开统计，
    模型看到的提示也不同），而不是"自动重试"。
    """
    if exc.code is McpErrorCode.TIMEOUT:
        raise TimeoutError(exc.message) from exc
    if exc.code in (McpErrorCode.CONNECT_FAILED, McpErrorCode.TRANSPORT_CLOSED,
                    McpErrorCode.SESSION_EXPIRED):
        raise ConnectionError(exc.message) from exc
    raise RuntimeError(exc.message) from exc


def make_handler(client: McpClient, remote_name: str) -> Callable[..., str]:
    """造一个调用远端工具的 handler。

    ⚠️ handler 里**没有治理逻辑**（与 `builtin.py` 同一条纪律）：
    不校验参数、不判权限、不做重试。它只回答"把这次调用送出去、把文本拿回来"。
    """

    def handler(**kwargs: Any) -> str:
        try:
            result = client.call_tool(remote_name, kwargs)
        except McpError as exc:
            translate_error(exc)      # 翻成标准异常后由 Runtime 分类
        if result.is_error:
            raise McpToolFailure(result.text or f"工具 {remote_name} 执行失败")
        return result.text

    return handler


def required_keys_of(schema: dict) -> tuple[str, ...]:
    """从远端 JSON Schema 里提取必填字段，交给 Runtime 做轻量校验。

    ⚠️ **直接读远端的 `required`**，不在本地另立一套——
    另立等于把"什么是必填"存了两份，两边迟早不一致；
    而远端改了 schema 我们却不知道时，两份的矛盾会变成线上故障。

    有了它，模型漏参数的调用会被 Runtime 当场拦下并回一条结构化错误，
    **不用白跑一趟进程或网络**——这个往返在慢 server 上很贵。
    """
    required = schema.get("required")
    if not isinstance(required, list):
        return ()
    return tuple(str(key) for key in required if isinstance(key, str))


def to_registered_tool(tool: McpTool, client: McpClient) -> RegisteredTool:
    """`McpTool` → `RegisteredTool`（本模块的核心投影）。"""
    # 只有明确声明只读才放宽；未声明（None）与声明不可读走同一条保守路径
    declared_read_only = tool.read_only_hint is True

    return RegisteredTool(
        spec=ToolSpec(
            name=tool.qualified_name,
            # 前缀让模型知道这个工具来自进程外——来源不同，出错时的处置也不同
            description=f"[MCP:{tool.server}] {tool.description}".strip(),
            # schema **直接透传**，不重新造：重造一遍等于把远端约束翻译一次，
            # 翻译错了两边都不认，而且远端升级 schema 时我们要跟着改
            input_model=tool.input_schema,
            # 两个路径集合都为空 = 不做路径约束。远端工具的参数不是本机路径，
            # 本地无从判起——由 risk + HITL 兜底
            permission=Permission(),
            risk=Risk.LOW if declared_read_only else Risk.MEDIUM,
            requires_approval=not declared_read_only,
            # 必填字段透传给 Runtime，本地先拦一道（省一次远端往返）
            required_keys=required_keys_of(tool.input_schema),
        ),
        handler=make_handler(client, tool.name),
        read_only=declared_read_only,
        concurrency_safe=declared_read_only,
        timeout=client.spec.timeout,
    )


class McpSession:
    """一组 MCP 连接的持有者。

    工具注册进的是**全局注册表**，但连接是有生命周期的对象——
    所以必须有人持有它们，否则连接会在函数返回时泄漏。
    `McpSession` 就是这个持有者：`with` 进出，或显式 `close()`。
    """

    def __init__(
        self,
        clients: list[McpClient],
        errors: list[McpError],
        tools: list[str] | None = None,
    ) -> None:
        self._clients = clients
        self.tools = list(tools or [])
        """已注册的工具名（带命名空间）。排查"模型怎么不会用那个工具"时先看它。"""
        self.errors = errors
        """连接的失败清单。**不抛异常**——上层自己决定是记一笔还是中止
        （CI 里可能要求零失败，本地开发少一个 server 也该能跑）。"""

    @property
    def clients(self) -> list[McpClient]:
        return list(self._clients)

    @property
    def tool_count(self) -> int:
        return len(self.tools)

    def close(self) -> None:
        for client in self._clients:
            client.close()
        self._clients = []

    def __enter__(self) -> "McpSession":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __repr__(self) -> str:
        names = [c.server for c in self._clients]
        return f"McpSession(connected={names}, failed={[e.server for e in self.errors]})"


def connect_mcp_servers(specs: list[McpServerSpec]) -> McpSession:
    """连接所有 server，把它们的工具注册进全局注册表。

    **一个 server 挂掉不影响其它**：连不上、握手失败、`tools/list` 报错，
    都只记进 `session.errors` 并关掉那一个连接，其余照常。

    Returns:
        持有全部连接的会话。**调用方必须保证它被关闭**（用 `with` 最省心）。
    """
    clients: list[McpClient] = []
    errors: list[McpError] = []
    registered: list[str] = []

    for spec, client, connect_error in iter_clients(specs):
        if client is None:
            if connect_error is not None:
                errors.append(connect_error)
            continue
        try:
            tools = client.list_tools()
        except McpError as exc:
            errors.append(exc)
            client.close()          # 发现不了工具就没必要占着进程
            continue

        for tool in tools:
            register(to_registered_tool(tool, client))
            registered.append(tool.qualified_name)
        clients.append(client)

    return McpSession(clients=clients, errors=errors, tools=registered)
