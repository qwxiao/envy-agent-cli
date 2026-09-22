"""模块5 · MCP 客户端测试。

分三层：

- **纯函数**（config 解析 / JSON-RPC 拼装 / 投影规则）——不需要任何 IO
- **假传输**（`FakeTransport`）——验 client 的会话逻辑，不起进程
- **真进程**（`_FAKE_SERVER_SOURCE`）——验 StdioTransport 真的能把消息送出去再收回来

最后那层是**唯一能证明传输可用**的测试。前两层用假对象验的是"我们以为协议长这样"，
只有真起一个进程跑一遍，才知道 `readline` 阻塞、编码、flush 这些事有没有做对。
"""

import json
import sys
from pathlib import Path

import pytest

from envy_agent_cli.mcp.bridge import (
    McpToolFailure,
    connect_mcp_servers,
    to_registered_tool,
    translate_error,
)
from envy_agent_cli.mcp.client import McpClient, render_content
from envy_agent_cli.mcp.config import expand_vars, load_mcp_server_specs
from envy_agent_cli.mcp.transport import (
    StdioTransport,
    StreamableHttpTransport,
    build_notification,
    build_request,
    build_transport,
    parse_rpc_response,
)
from envy_agent_cli.mcp.types import (
    McpError,
    McpErrorCode,
    McpServerSpec,
    McpTool,
)
from envy_agent_cli.tools import registry

# ---------------------------------------------------------------- 工具

#: 一个最小的 MCP server，用 python -c 直接跑。
#: 只实现三个方法，够验完传输层的收发。
_FAKE_SERVER_SOURCE = r"""
import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    method = msg.get("method")
    if "id" not in msg:            # 通知：不回
        continue
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18",
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": "fake", "version": "9.9"}}
    elif method == "tools/list":
        result = {"tools": [
            {"name": "echo", "description": "回显参数",
             "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
            {"name": "peek", "description": "只读探针",
             "inputSchema": {"type": "object"},
             "annotations": {"readOnlyHint": True}},
        ]}
    elif method == "tools/call":
        params = msg.get("params") or {}
        result = {"content": [{"type": "text",
                               "text": "echo:" + json.dumps(params.get("arguments"), ensure_ascii=False)}]}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\n")
    sys.stdout.flush()
"""


def fake_spec(name: str = "fake", **kwargs) -> McpServerSpec:
    """指向那个假 server 的声明。"""
    defaults = dict(
        name=name,
        transport="stdio",
        command=sys.executable,
        args=("-c", _FAKE_SERVER_SOURCE),
        timeout=15.0,
    )
    defaults.update(kwargs)
    return McpServerSpec(**defaults)  # type: ignore[arg-type]


class FakeTransport:
    """记录请求、按脚本回答的假传输。"""

    def __init__(self, responses: dict[str, list]) -> None:
        self.server = "fake"
        self.sent: list[tuple[str, str, dict]] = []
        self._responses = {k: list(v) for k, v in responses.items()}
        self.started = False
        self._closed = False

    def start(self) -> None:
        self.started = True

    def close(self) -> None:
        self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed

    def request(self, method: str, params: dict | None = None) -> dict:
        self.sent.append(("request", method, params or {}))
        queued = self._responses.get(method)
        if not queued:
            raise McpError(McpErrorCode.PROTOCOL_ERROR, f"没为 {method} 准备应答")
        answer = queued.pop(0)
        if isinstance(answer, McpError):
            raise answer
        return answer

    def notify(self, method: str, params: dict | None = None) -> None:
        self.sent.append(("notify", method, params or {}))

    def methods(self) -> list[str]:
        return [m for _, m, _ in self.sent]


@pytest.fixture
def clean_registry():
    """备份并还原全局注册表——注册表是进程级单例，测试之间会互相污染。"""
    saved = dict(registry._REGISTRY)
    yield
    registry._REGISTRY.clear()
    registry._REGISTRY.update(saved)


# ---------------------------------------------------------------- config


def test_load_specs_from_project_file(tmp_path):
    config = tmp_path / ".envy" / "mcp.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"mcpServers": {
        "fs": {"command": "npx", "args": ["-y", "pkg"]},
        "remote": {"type": "http", "url": "https://example.com/mcp"},
    }}), encoding="utf8")

    specs = load_mcp_server_specs(tmp_path, user_path=tmp_path / "none.json")

    assert [s.name for s in specs] == ["fs", "remote"]
    assert specs[0].is_stdio and specs[0].args == ("-y", "pkg")
    assert specs[1].is_http and specs[1].url == "https://example.com/mcp"


def test_project_overrides_user_level(tmp_path):
    user = tmp_path / "user.json"
    user.write_text(json.dumps({"mcpServers": {
        "a": {"command": "old"},
        "b": {"command": "keep"},
    }}), encoding="utf8")
    project = tmp_path / "proj.json"
    project.write_text(json.dumps({"mcpServers": {
        "a": {"command": "new"},
    }}), encoding="utf8")

    specs = load_mcp_server_specs(tmp_path, user_path=user, project_path=project)
    by_name = {s.name: s for s in specs}

    assert by_name["a"].command == "new"      # 项目级覆盖
    assert by_name["b"].command == "keep"     # 用户级的其它条目不受影响


def test_disabled_server_is_skipped(tmp_path):
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({"mcpServers": {
        "on": {"command": "x"},
        "off": {"command": "y", "enabled": False},
    }}), encoding="utf8")

    specs = load_mcp_server_specs(tmp_path, user_path=tmp_path / "none.json", project_path=config)
    assert [s.name for s in specs] == ["on"]


def test_transport_inferred_without_type_field(tmp_path):
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({"mcpServers": {
        "by_command": {"command": "x"},
        "by_url": {"url": "https://e.com"},
    }}), encoding="utf8")

    specs = load_mcp_server_specs(tmp_path, user_path=tmp_path / "none.json", project_path=config)
    assert {s.name: s.transport for s in specs} == {"by_command": "stdio", "by_url": "http"}


@pytest.mark.parametrize("raw, keyword", [
    ({"type": "stdio"}, "command"),          # stdio 缺 command
    ({"type": "http"}, "url"),               # HTTP 缺 url
    ({"type": "carrier-pigeon"}, "传输类型"),
])
def test_bad_server_declaration_raises(tmp_path, raw, keyword):
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({"mcpServers": {"bad": raw}}), encoding="utf8")

    with pytest.raises(McpError) as exc:
        load_mcp_server_specs(tmp_path, user_path=tmp_path / "none.json", project_path=config)
    assert keyword in str(exc.value)


def test_broken_json_fails_loud(tmp_path):
    """文件级语法错误必须报错——静默跳过会让用户对着"server 没加载"发呆。"""
    config = tmp_path / "mcp.json"
    config.write_text("{ not json", encoding="utf8")

    with pytest.raises(McpError):
        load_mcp_server_specs(tmp_path, user_path=tmp_path / "none.json", project_path=config)


def test_expand_vars_keeps_unknown_intact(tmp_path):
    text = expand_vars("${PROJECT_DIR}/a ${HOME}/b ${NOT_SET_XYZ}/c", tmp_path)
    assert str(tmp_path) in text
    assert "${NOT_SET_XYZ}" in text        # 找不到就原样留着，便于发现


# ---------------------------------------------------------------- types / transport 纯函数


def test_qualified_name_has_namespace():
    tool = McpTool(server="fs", name="read", description="d", input_schema={})
    assert tool.qualified_name == "mcp__fs__read"


def test_repr_hides_headers():
    """headers 可能装凭证，不该被 repr 顺手打出来。"""
    spec = McpServerSpec(name="s", transport="http", url="u",
                         headers={"Authorization": "Bearer SECRET"})
    assert "SECRET" not in repr(spec)
    assert "Authorization" in repr(spec)   # 键名可以露，值不行


def test_build_notification_has_no_id():
    assert "id" not in build_notification("notifications/initialized", None)


def test_build_request_has_id_and_params():
    message = build_request(7, "tools/list", None)
    assert message == {"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": {}}


def test_parse_rpc_response_ignores_other_ids():
    assert parse_rpc_response({"id": 99, "result": {}}, 1, "m", "s") is None
    assert parse_rpc_response({"method": "notifications/x"}, 1, "m", "s") is None


def test_parse_rpc_response_keeps_raw_error_code():
    with pytest.raises(McpError) as exc:
        parse_rpc_response(
            {"id": 1, "error": {"code": -32602, "message": "Unknown tool"}}, 1, "tools/call", "s")
    assert exc.value.rpc_code == -32602


def test_build_transport_picks_implementation():
    assert isinstance(build_transport(fake_spec()), StdioTransport)
    assert isinstance(
        build_transport(McpServerSpec(name="h", transport="http", url="https://e.com")),
        StreamableHttpTransport)


def test_build_transport_rejects_unknown():
    with pytest.raises(McpError):
        build_transport(McpServerSpec(name="x", transport="pigeon"))


# ---------------------------------------------------------------- client（假传输）


def test_connect_sends_initialize_then_initialized():
    """⚠️ 少发那条通知，有些 server 会对后续请求直接报"未初始化"。"""
    transport = FakeTransport({"initialize": [{"serverInfo": {"name": "s"}}]})
    McpClient(fake_spec(), transport=transport).connect()

    assert transport.methods() == ["initialize", "notifications/initialized"]
    assert transport.sent[1][0] == "notify"     # 第二条必须是通知，不是请求


def test_connect_closes_transport_when_handshake_fails():
    transport = FakeTransport({"initialize": [McpError(McpErrorCode.CONNECT_FAILED, "boom")]})
    with pytest.raises(McpError):
        McpClient(fake_spec(), transport=transport).connect()
    assert transport.closed is True             # 失败不留半个连接


def test_client_exposes_server_capabilities():
    transport = FakeTransport({"initialize": [{
        "serverInfo": {"name": "s", "version": "1"},
        "capabilities": {"tools": {}, "resources": {}},
    }]})
    client = McpClient(fake_spec(), transport=transport)
    client.connect()

    assert client.supports("resources") is True
    assert client.supports("prompts") is False
    assert client.server_info["version"] == "1"


def test_list_tools_maps_remote_fields():
    transport = FakeTransport({
        "initialize": [{}],
        "tools/list": [{"tools": [{
            "name": "peek", "description": " 探针 ",
            "inputSchema": {"type": "object"},
            "annotations": {"readOnlyHint": True},
        }]}],
    })
    client = McpClient(fake_spec(), transport=transport)
    client.connect()

    tool, = client.list_tools()
    assert (tool.name, tool.description, tool.read_only_hint) == ("peek", "探针", True)


def test_list_tools_follows_cursor():
    transport = FakeTransport({
        "initialize": [{}],
        "tools/list": [
            {"tools": [{"name": "a", "inputSchema": {}}], "nextCursor": "p2"},
            {"tools": [{"name": "b", "inputSchema": {}}]},
        ],
    })
    client = McpClient(fake_spec(), transport=transport)
    client.connect()

    assert [t.name for t in client.list_tools()] == ["a", "b"]


def test_list_tools_skips_entries_without_name():
    """一条坏数据不该让整个 server 的工具全不可用。"""
    transport = FakeTransport({
        "initialize": [{}],
        "tools/list": [{"tools": [
            {"description": "没有名字"},
            {"name": "good", "inputSchema": {}},
        ]}],
    })
    client = McpClient(fake_spec(), transport=transport)
    client.connect()

    assert [t.name for t in client.list_tools()] == ["good"]


def test_read_only_hint_only_accepts_bool():
    """远端给字符串 "true" 时当没声明——信任边界上不靠类型转换猜意图。"""
    transport = FakeTransport({
        "initialize": [{}],
        "tools/list": [{"tools": [{"name": "x", "inputSchema": {},
                                   "annotations": {"readOnlyHint": "true"}}]}],
    })
    client = McpClient(fake_spec(), transport=transport)
    client.connect()

    assert client.list_tools()[0].read_only_hint is None


def test_call_tool_returns_text():
    transport = FakeTransport({
        "initialize": [{}],
        "tools/call": [{"content": [{"type": "text", "text": "结果"}]}],
    })
    client = McpClient(fake_spec(), transport=transport)
    client.connect()

    result = client.call_tool("echo", {"a": 1})
    assert (result.text, result.is_error) == ("结果", False)
    assert transport.sent[-1][2] == {"name": "echo", "arguments": {"a": 1}}


def test_call_tool_is_error_is_not_an_exception():
    """工具自己失败 vs 根本没谈成，是两条路——前者靠返回值，后者才抛。"""
    transport = FakeTransport({
        "initialize": [{}],
        "tools/call": [{"content": [{"type": "text", "text": "炸了"}], "isError": True}],
    })
    client = McpClient(fake_spec(), transport=transport)
    client.connect()

    result = client.call_tool("echo", {})
    assert result.is_error is True and result.text == "炸了"


def test_call_tool_maps_invalid_params_to_tool_not_found():
    transport = FakeTransport({
        "initialize": [{}],
        "tools/call": [McpError(McpErrorCode.PROTOCOL_ERROR, "Unknown tool: x", rpc_code=-32602)],
    })
    client = McpClient(fake_spec(), transport=transport)
    client.connect()

    with pytest.raises(McpError) as exc:
        client.call_tool("x", {})
    assert exc.value.code is McpErrorCode.TOOL_NOT_FOUND


def test_session_expiry_triggers_one_reconnect():
    """会话失效 → 重连后重试一次，而不是把失败直接抛给调用方。"""
    transport = FakeTransport({
        "initialize": [{}, {}],                    # 初次握手 + 重连各一次
        "tools/call": [
            McpError(McpErrorCode.SESSION_EXPIRED, "会话没了"),
            {"content": [{"type": "text", "text": "好了"}]},
        ],
    })
    client = McpClient(fake_spec(), transport=transport)
    client.connect()

    assert client.call_tool("echo", {}).text == "好了"
    assert transport.methods().count("initialize") == 2      # 确实重新握了手


def test_reconnect_gives_up_after_one_retry():
    """只试一次：重连后还失败说明问题不在会话，继续重试只是把一次失败拖成多次。"""
    transport = FakeTransport({
        "initialize": [{}, {}],
        "tools/call": [
            McpError(McpErrorCode.SESSION_EXPIRED, "没了"),
            McpError(McpErrorCode.SESSION_EXPIRED, "还是没了"),
        ],
    })
    client = McpClient(fake_spec(), transport=transport)
    client.connect()

    with pytest.raises(McpError) as exc:
        client.call_tool("echo", {})
    assert exc.value.code is McpErrorCode.SESSION_EXPIRED


def test_non_session_errors_are_not_retried():
    """其它错误**不该**触发重连——那会把确定性的失败变成一次多余的握手。"""
    transport = FakeTransport({
        "initialize": [{}],
        "tools/call": [McpError(McpErrorCode.CALL_FAILED, "远端炸了")],
    })
    client = McpClient(fake_spec(), transport=transport)
    client.connect()

    with pytest.raises(McpError):
        client.call_tool("echo", {})
    assert transport.methods().count("initialize") == 1


@pytest.mark.parametrize("blocks, expected", [
    ([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}], "a\nb"),
    ([{"type": "image"}], "[image 类型内容未展开]"),
    ([{"type": "resource", "resource": {"uri": "f://x"}}], "[resource f://x：非文本内容未展开]"),
])
def test_render_content_degrade(blocks, expected):
    assert render_content(blocks) == expected


# ---------------------------------------------------------------- bridge 投影


def test_unannotated_tool_requires_approval():
    """未声明 ≠ 安全。Unknown 比 False 更危险。"""
    tool = McpTool(server="s", name="t", description="d", input_schema={})
    registered = to_registered_tool(tool, McpClient(fake_spec()))

    assert registered.read_only is False
    assert registered.concurrency_safe is False
    assert registered.spec.requires_approval is True
    assert registered.spec.name == "mcp__s__t"


def test_readonly_hint_relaxes_governance():
    tool = McpTool(server="s", name="t", description="d", input_schema={}, read_only_hint=True)
    registered = to_registered_tool(tool, McpClient(fake_spec()))

    assert registered.read_only is True
    assert registered.spec.requires_approval is False


def test_projection_keeps_description_prefix_and_schema():
    schema = {"type": "object", "properties": {"p": {"type": "string"}}}
    tool = McpTool(server="fs", name="read", description="读文件", input_schema=schema)
    registered = to_registered_tool(tool, McpClient(fake_spec()))

    assert registered.spec.description.startswith("[MCP:fs]")
    assert registered.spec.input_model is schema      # 透传同一对象，不重造


def test_projection_carries_required_keys():
    """必填字段要透传给 Runtime——否则模型漏参数会白跑一趟远端进程。"""
    schema = {"type": "object", "properties": {"p": {"type": "string"}}, "required": ["p"]}
    tool = McpTool(server="fs", name="read", description="d", input_schema=schema)
    registered = to_registered_tool(tool, McpClient(fake_spec()))

    assert registered.spec.required_keys == ("p",)


def test_projection_tolerates_missing_or_malformed_required():
    for schema in ({"type": "object"}, {"type": "object", "required": "p"}, {"type": "object", "required": [1, "ok"]}):
        tool = McpTool(server="s", name="t", description="d", input_schema=schema)
        keys = to_registered_tool(tool, McpClient(fake_spec())).spec.required_keys
        assert keys in ((), ("ok",))


@pytest.mark.parametrize("code, expected", [
    (McpErrorCode.TIMEOUT, TimeoutError),
    (McpErrorCode.CONNECT_FAILED, ConnectionError),
    (McpErrorCode.TRANSPORT_CLOSED, ConnectionError),
    (McpErrorCode.SESSION_EXPIRED, ConnectionError),
    (McpErrorCode.PROTOCOL_ERROR, RuntimeError),
    (McpErrorCode.TOOL_NOT_FOUND, RuntimeError),
])
def test_translate_error_to_native(code, expected):
    """翻译成标准异常，Runtime 的 _classify 才能原样工作——它不该知道 MCP 存在。"""
    with pytest.raises(expected):
        translate_error(McpError(code, "boom"))


def test_tool_failure_is_not_retryable_by_classification():
    """McpToolFailure 继承 RuntimeError → 归 UNKNOWN（不重试）。
    若继承 OSError 会被归为 UPSTREAM_ERROR 而进重试队列，但远端已明说失败。"""
    assert issubclass(McpToolFailure, RuntimeError)
    assert not issubclass(McpToolFailure, OSError)


# ---------------------------------------------------------------- 真进程集成


def test_stdio_transport_real_process_roundtrip():
    """唯一能证明传输可用的测试：真起一个进程，发请求、收应答、关闭。"""
    transport = StdioTransport(fake_spec())
    transport.start()
    try:
        result = transport.request("initialize", {"protocolVersion": "2025-06-18"})
        assert result["serverInfo"]["name"] == "fake"

        listing = transport.request("tools/list")
        assert [t["name"] for t in listing["tools"]] == ["echo", "peek"]

        call = transport.request("tools/call", {"name": "echo", "arguments": {"text": "hi"}})
        assert call["content"][0]["text"] == 'echo:{"text": "hi"}'
    finally:
        transport.close()
    assert transport.closed is True


def test_stdio_transport_reports_missing_command():
    transport = StdioTransport(McpServerSpec(
        name="nope", transport="stdio", command="definitely-not-a-real-binary-xyz"))
    with pytest.raises(McpError) as exc:
        transport.start()
    assert exc.value.code is McpErrorCode.CONNECT_FAILED


def test_connect_mcp_servers_registers_prefixed_tools(clean_registry):
    session = connect_mcp_servers([fake_spec("fake")])
    try:
        assert session.tool_count == 2
        assert session.errors == []
        assert "mcp__fake__echo" in registry.all_names()

        registered = registry.get("mcp__fake__peek")
        assert registered is not None and registered.read_only is True
    finally:
        session.close()


def test_connect_mcp_servers_isolates_failure(clean_registry):
    """一个 server 连不上，不影响另一个——本地开发少一个 server 也该能跑。"""
    bad = McpServerSpec(name="bad", transport="stdio", command="definitely-not-a-real-binary-xyz")
    session = connect_mcp_servers([bad, fake_spec("good")])
    try:
        assert session.tool_count == 2                       # good 的工具照常注册
        assert [e.server for e in session.errors] == ["bad"]
        assert "mcp__good__echo" in registry.all_names()
    finally:
        session.close()


def test_registered_tool_handler_calls_remote(clean_registry):
    """端到端：模型看到的工具名 → handler → 远端进程 → 结果文本。"""
    session = connect_mcp_servers([fake_spec("fake")])
    try:
        registered = registry.get("mcp__fake__echo")
        assert registered is not None
        assert registered.handler(**{"text": "hi"}) == 'echo:{"text": "hi"}'
    finally:
        session.close()
