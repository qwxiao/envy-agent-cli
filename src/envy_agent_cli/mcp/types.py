"""模块5 · MCP 客户端的数据契约（纯数据，不含行为）。

MCP（Model Context Protocol）把"工具的来源"从**进程内**扩展到**进程外**：
工具由外部 server 提供，本进程只负责**发现**与**调用**。

本文件只定义**数据形状**——连接声明、远端工具、失败分类。
真正的行为在 `transport.py`（怎么连）与 `client.py`（怎么谈），
投影成内部工具那一步在 `bridge.py`。

**为什么单独分一层**：MCP 的原始数据（远端 JSON Schema、annotations）
与我们的 `ToolSpec` 是**两套契约**。中间隔一层数据类，
投影规则才能单独演进——远端协议加字段时不会波及注册表。
"""

from dataclasses import dataclass, field
from enum import Enum

#: stdio 传输的写法。不同 Host 的配置文件里叫法不一，全部收下。
STDIO_TYPES = frozenset({"stdio", "local"})

#: HTTP 传输的写法。`streamable_http` 是规范里的正式名，
#: `http` 是配置里常见的简写，连字符版本是手滑——三种都认。
HTTP_TYPES = frozenset({"http", "streamable_http", "streamable-http"})

#: 工具名的命名空间分隔符。三下划线：`mcp__<server>__<tool>`。
#: 选它的理由是**不可能与内置工具撞名**——内置工具名全是单个下划线以内。
NAMESPACE_SEP = "__"
NAMESPACE_PREFIX = "mcp"


class McpErrorCode(str, Enum):
    """MCP 层的结构化失败。

    与 `tools.result.ErrorCode` 是**两套错误码**，刻意不合并：
    这里描述"与外部 server 的交互出了什么问题"，
    那边描述"工具执行出了什么问题"。前者要经 `bridge.py` 翻译成后者，
    翻译是一次**有损映射**——`PROTOCOL_ERROR` 到底算 `UPSTREAM_ERROR` 还是 `UNKNOWN`，
    是投影层的判断，不该由数据层预先决定。
    """

    CONNECT_FAILED = "CONNECT_FAILED"        # 起不来连不上（命令不存在 / URL 不通）
    PROTOCOL_ERROR = "PROTOCOL_ERROR"        # 连上了但握手/应答不符合协议
    TIMEOUT = "TIMEOUT"                      # 单次请求超时
    TOOL_NOT_FOUND = "TOOL_NOT_FOUND"        # 远端没有这个工具
    CALL_FAILED = "CALL_FAILED"              # 工具执行失败（远端报错）
    TRANSPORT_CLOSED = "TRANSPORT_CLOSED"    # 通道已关闭还发请求
    SESSION_EXPIRED = "SESSION_EXPIRED"      # HTTP 会话失效（服务端重启或超时）


class McpError(Exception):
    """MCP 层的失败。带错误码，好让投影层翻译成 `ToolError`。

    ⚠️ **消息里不能带凭证**——这个异常会一路冒到模型看的结果里。
    """

    def __init__(
        self,
        code: McpErrorCode,
        message: str,
        *,
        server: str | None = None,
        rpc_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.server = server
        self.rpc_code = rpc_code
        """JSON-RPC 的原始错误码，**原样保留**。

        自定义码与协议码是两层信息：前者给我们的代码做分支，后者给排查用。
        合并成一个会丢掉原文——而原文恰恰是回头查规范时的唯一线索
        （比如 `-32602` 是"参数无效"，很多 server 用它表示"没有这个工具"）。
        """

    def __str__(self) -> str:  # pragma: no cover - 仅用于日志
        where = f"[{self.server}] " if self.server else ""
        return f"{where}{self.code.value}: {self.message}"


@dataclass(frozen=True, slots=True)
class McpServerSpec:
    """一个 MCP server 的连接声明（配置文件反序列化后的结果）。

    **两种传输共用这一个类**，而不是拆成两个——
    因为对上层来说"有一个叫 X 的 server"是同一件事实，
    配置文件的 `mcpServers` 也是一个对象装两种形态。拆开会逼调用方先判断类型再取字段。

    Attributes:
        name: 命名空间用的 server 名。**工具名里会带上它**，所以要短、要稳。
        transport: 传输类型，取值在 `STDIO_TYPES` / `HTTP_TYPES` 里。
        command / args / env: stdio 三件套——起什么进程、带什么参数、补什么环境变量。
        url / headers: HTTP 两件套——连哪里、带什么头。
        timeout: 单次请求超时（秒）。**不是连接超时**——stdio 起进程可能要几秒。
        enabled: 关掉的 server 会被加载器跳过，但配置保留（便于临时禁用排查）。
    """

    name: str
    transport: str
    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    timeout: float = 30.0
    enabled: bool = True

    @property
    def is_stdio(self) -> bool:
        return self.transport in STDIO_TYPES

    @property
    def is_http(self) -> bool:
        return self.transport in HTTP_TYPES

    def __repr__(self) -> str:
        # headers 可能装凭证，不进 repr——异常栈与日志会顺手打印对象
        return (f"McpServerSpec(name={self.name!r}, transport={self.transport!r}, "
                f"command={self.command!r}, url={self.url!r}, "
                f"headers={sorted(self.headers)}, timeout={self.timeout})")


@dataclass(frozen=True, slots=True)
class McpTool:
    """远端 server 声明的一个工具（**远端原文**，未做任何投影）。

    Attributes:
        input_schema: **直接透传**的远端 JSON Schema。
            不重新造一遍——重造等于把远端的约束翻译一次，
            翻译错了两边都不认，而且远端升级 schema 时我们要跟着改。
        read_only_hint: 来自 `annotations.readOnlyHint`。
            ⚠️ **这是 server 的自我声明，不是保证**——它是"提示"不是"承诺"，
            所以投影层只把它当作"可以放宽到什么程度"的上限，见 `bridge.py`。
            `None` 表示远端压根没声明。
    """

    server: str
    name: str
    description: str
    input_schema: dict
    read_only_hint: bool | None = None

    @property
    def qualified_name(self) -> str:
        """带命名空间的工具名——注册进注册表时用它。

        ⚠️ 前缀不是装饰：内置工具与远端工具**在注册表里是平权的**，
        不加前缀就可能被远端同名工具**静默覆盖**（`register()` 是同名覆盖语义）。
        """
        return f"{NAMESPACE_PREFIX}{NAMESPACE_SEP}{self.server}{NAMESPACE_SEP}{self.name}"


@dataclass(slots=True)
class McpToolResult:
    """`tools/call` 的归一化结果。

    Attributes:
        text: 把远端返回的**文本块**拼起来的字符串。
            非文本块（image / audio / resource）**暂不展开**，
            只留一行占位说明——因为本项目的模型契约是纯文本的，
            展开成 base64 只会把上下文塞爆。这是刻意的降级，不是遗漏。
        is_error: 远端标的 `isError`。**注意它与传输失败是两回事**：
            `is_error=True` 表示"工具跑了但结果是错的"（通道正常），
            抛 `McpError` 表示"根本没谈成"。两者在审计里必须分开统计。
    """

    text: str
    is_error: bool = False
    raw: dict | None = None
    """远端原始结果。留一份以备排查——**不进模型可见面**。"""
