"""演示用 MCP server（stdio 传输，**纯标准库**）。

用途：本地验证 envy 的 MCP 接入链路，不需要装任何第三方 server。

用法——在项目根建 `.envy/mcp.json`：

```json
{
  "mcpServers": {
    "demo": {"command": "python", "args": ["scripts/demo_mcp_server.py"]}
  }
}
```

然后：

```
envy --list-mcp                     # 应当列出 demo
envy "现在几点了？"                  # 模型会调 mcp__demo__now
envy "统计一下 'hello world' 有几个词"  # 会调 mcp__demo__word_count
```

**协议本身没有魔法**：换行分隔的 JSON-RPC 2.0，读到一行就回一行。
这里只用 `json` + `sys` 实现，是为了说明"接一个 MCP server 不需要 SDK"——
也顺带证明了 `StdioTransport` 面对的确实只有管道和 JSON。

真正健壮的 server 该用官方 SDK 写（这个只实现最小子集：initialize / tools/list / tools/call）。
"""

import json
import sys
from datetime import datetime

PROTOCOL_VERSION = "2025-06-18"

#: 对外声明的工具。`annotations.readOnlyHint` 会被桥接层读成"可以免审批"——
#: 这两个工具确实只读，所以如实声明。
TOOLS = [
    {
        "name": "now",
        "description": "返回当前本地时间。",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "word_count",
        "description": "统计一段文本的词数与字符数。",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "要统计的文本"}},
            "required": ["text"],
        },
        "annotations": {"readOnlyHint": True},
    },
]


def call_tool(params: dict) -> dict:
    name = params.get("name")
    args = params.get("arguments") or {}

    if name == "now":
        text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return {"content": [{"type": "text", "text": text}]}

    if name == "word_count":
        text = str(args.get("text") or "")
        return {"content": [{"type": "text",
                             "text": f"{len(text.split())} 个词，{len(text)} 个字符"}]}

    return {"content": [{"type": "text", "text": f"未知工具：{name}"}], "isError": True}


def handle(method: str | None, params: dict | None) -> dict:
    if method == "initialize":
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "demo", "version": "1.0"},
        }
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        return call_tool(params or {})
    if method == "resources/list":
        return {"resources": []}
    return {}


def main() -> None:
    # Windows 上 stdout 默认是 GBK，中文会炸。MCP 规定传输编码是 UTF-8，显式改过来。
    # （envy 作为客户端也会注入 PYTHONIOENCODING 兜底，这里是独立运行时的保险。）
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue                       # 混进来的杂音不该让进程退出
        if "id" not in message:
            continue                       # 通知：收到即可，不回
        try:
            result = handle(message.get("method"), message.get("params"))
            response = {"jsonrpc": "2.0", "id": message["id"], "result": result}
        except Exception as exc:           # noqa: BLE001 - 协议要求错误也走应答
            response = {"jsonrpc": "2.0", "id": message["id"],
                        "error": {"code": -32603, "message": str(exc)}}
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()                 # 不 flush 对面永远收不到


if __name__ == "__main__":
    main()
