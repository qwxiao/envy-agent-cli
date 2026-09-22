"""模块5 · MCP 配置：从 JSON 文件读 server 声明。

**采用社区事实标准格式**（顶层 `mcpServers` 对象）——Claude Desktop / Claude Code /
Cursor 都用这一份。抄别人的配置能直接用，用户也不用再学一种新格式。

查找顺序（**后者覆盖同名前者**）：

1. `~/.envy/mcp.json` —— 用户级，所有项目共享（放个人常用的 server）
2. `<cwd>/.envy/mcp.json` —— 项目级，只属于这个项目（放项目专属的 server）

覆盖语义是**按 server 名**，不是整体替换：项目级只写它要改的那一个，
用户级其余的照旧生效。

变量展开：`${PROJECT_DIR}` / `${HOME}` / `${环境变量名}`。

**凭证只从环境进来**——配置文件里写 `"Authorization": "Bearer ${MY_TOKEN}"`，
不写明文。这与 `config.py`（LLM 凭证）是同一条纪律：凭证不落盘、不进版本控制。

⚠️ **本文件只负责"读出声明"，不负责连**。连不上、握手失败都是 `client.py` 的事——
配置错误的判据是"形状不对"，连接失败的判据是"谈不成"，两者不该混在一个错误里。
"""

import json
import os
import re
from pathlib import Path

from envy_agent_cli.mcp.types import (
    HTTP_TYPES,
    STDIO_TYPES,
    McpError,
    McpErrorCode,
    McpServerSpec,
)

#: 配置文件名。放在 `.envy/` 目录下，与项目其它本地配置的约定一致。
CONFIG_FILENAME = "mcp.json"

#: 项目级配置目录（相对 cwd）
PROJECT_CONFIG_DIR = ".envy"

#: 用户级配置目录
USER_CONFIG_DIR = Path.home() / ".envy"

#: 顶层键名——社区事实标准，不要改。
SERVERS_KEY = "mcpServers"

#: `${VAR}` 形式的变量引用
_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

#: 内置变量（不查环境，直接给值）
_BUILTIN_VARS = {"HOME": lambda: str(Path.home())}


def expand_vars(text: str, cwd: Path) -> str:
    """展开 `${VAR}`。

    认识内置变量（`${PROJECT_DIR}` / `${HOME}`）与环境变量。

    ⚠️ **找不到的变量原样保留**，不替换成空串——
    用户能在配置里看到 `${FOO}` 还在，立刻知道是没展开；
    换成空串则会静默变成一个错误的路径或空凭证，排查成本高得多。
    """
    builtin = {"PROJECT_DIR": str(cwd), **{k: v() for k, v in _BUILTIN_VARS.items()}}

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in builtin:
            return builtin[name]
        return os.environ.get(name, match.group(0))

    return _VAR_PATTERN.sub(replace, text)


def _expand_value(value: object, cwd: Path) -> object:
    """递归展开字符串（dict / list 里的也要展开）。"""
    if isinstance(value, str):
        return expand_vars(value, cwd)
    if isinstance(value, dict):
        return {k: _expand_value(v, cwd) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_value(v, cwd) for v in value]
    return value


def _infer_transport(raw: dict) -> str:
    """没写 `type` 时按字段推断：有 `command` 是 stdio，有 `url` 是 HTTP。

    推断而不是强制要求写 `type`，是因为社区里的配置**大多不写**——
    拒绝加载一份能用的配置，是把规范的洁癖转嫁成用户的负担。
    """
    declared = raw.get("type")
    if isinstance(declared, str) and declared.strip():
        return declared.strip().lower()
    if raw.get("command"):
        return "stdio"
    if raw.get("url"):
        return "http"
    return ""


def _to_spec(name: str, raw: object, cwd: Path) -> McpServerSpec:
    """把一条配置转成 `McpServerSpec`。形状不对就抛 `McpError`。"""
    if not isinstance(raw, dict):
        raise McpError(McpErrorCode.PROTOCOL_ERROR, f"server {name!r} 的配置必须是一个对象")

    expanded = _expand_value(raw, cwd)
    transport = _infer_transport(expanded)

    if transport in STDIO_TYPES:
        command = expanded.get("command")
        if not isinstance(command, str) or not command.strip():
            raise McpError(McpErrorCode.PROTOCOL_ERROR, f"server {name!r} 是 stdio 传输，但没写 command")
        args = expanded.get("args") or []
        if not isinstance(args, list):
            raise McpError(McpErrorCode.PROTOCOL_ERROR, f"server {name!r} 的 args 必须是数组")
        env = expanded.get("env") or {}
        if not isinstance(env, dict):
            raise McpError(McpErrorCode.PROTOCOL_ERROR, f"server {name!r} 的 env 必须是对象")
        return McpServerSpec(
            name=name,
            transport=transport,
            command=command.strip(),
            args=tuple(str(a) for a in args),
            env={str(k): str(v) for k, v in env.items()},
            timeout=_timeout_of(expanded),
            enabled=bool(expanded.get("enabled", True)),
        )

    if transport in HTTP_TYPES:
        url = expanded.get("url")
        if not isinstance(url, str) or not url.strip():
            raise McpError(McpErrorCode.PROTOCOL_ERROR, f"server {name!r} 是 HTTP 传输，但没写 url")
        headers = expanded.get("headers") or {}
        if not isinstance(headers, dict):
            raise McpError(McpErrorCode.PROTOCOL_ERROR, f"server {name!r} 的 headers 必须是对象")
        return McpServerSpec(
            name=name,
            transport=transport,
            url=url.strip(),
            headers={str(k): str(v) for k, v in headers.items()},
            timeout=_timeout_of(expanded),
            enabled=bool(expanded.get("enabled", True)),
        )

    raise McpError(
        McpErrorCode.PROTOCOL_ERROR,
        f"server {name!r} 的传输类型无法识别（{transport or '未声明'}）；"
        f"stdio 需要 command，HTTP 需要 url",
    )


def _timeout_of(raw: dict) -> float:
    """读超时。非法值退回 30s 而不是报错——超时设错不该拦住整个 server。"""
    value = raw.get("timeout")
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return 30.0
    return seconds if seconds > 0 else 30.0


def _read_servers(path: Path) -> dict[str, object]:
    """读一个配置文件，返回 `mcpServers` 对象。文件不存在返回空。"""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf8"))
    except (OSError, json.JSONDecodeError) as exc:
        # 文件级错误 fail loud：静默跳过会让用户对着"为什么我的 server 没加载"发呆
        raise McpError(McpErrorCode.PROTOCOL_ERROR, f"配置文件 {path} 解析失败: {exc}") from exc

    if not isinstance(data, dict):
        raise McpError(McpErrorCode.PROTOCOL_ERROR, f"配置文件 {path} 的顶层必须是对象")

    servers = data.get(SERVERS_KEY)
    if servers is None:
        return {}
    if not isinstance(servers, dict):
        raise McpError(McpErrorCode.PROTOCOL_ERROR, f"配置文件 {path} 的 {SERVERS_KEY} 必须是对象")
    return servers


def load_mcp_server_specs(
    cwd: str | Path | None = None,
    *,
    user_path: str | Path | None = None,
    project_path: str | Path | None = None,
) -> list[McpServerSpec]:
    """装配 server 声明列表。

    Args:
        cwd: 项目根（`${PROJECT_DIR}` 展开成它）。默认当前工作目录。
        user_path / project_path: 覆盖默认查找路径（测试用）。

    Returns:
        按名字排序的声明列表。`enabled=False` 的**不在结果里**。

    Raises:
        McpError: 配置文件语法错误，或某个 server 的声明形状不对。

    ⚠️ **单个 server 写错会让整次加载失败**（而不是跳过它）——这是刻意的：
    静默少一个 server，表现为"模型突然不会用某个工具了"，
    排查成本远高于直接报错。要临时禁用请写 `"enabled": false`，那是显式意图。
    """
    root = Path(cwd) if cwd is not None else Path.cwd()
    sources = [
        Path(user_path) if user_path is not None else USER_CONFIG_DIR / CONFIG_FILENAME,
        Path(project_path) if project_path is not None else root / PROJECT_CONFIG_DIR / CONFIG_FILENAME,
    ]

    merged: dict[str, object] = {}
    for source in sources:
        merged.update(_read_servers(source))     # 后者覆盖同名前者

    specs = [_to_spec(name, raw, root) for name, raw in merged.items()]
    return sorted((s for s in specs if s.enabled), key=lambda s: s.name)


def describe_mcp_config(cwd: str | Path | None = None) -> dict[str, list[str]]:
    """汇总配置里出现了哪些 server（**只列名字与传输，不返回值**），便于排查。

    与 `config.describe_config` 同一纪律：诊断信息不该顺手把凭证打出来。
    """
    root = Path(cwd) if cwd is not None else Path.cwd()
    try:
        specs = load_mcp_server_specs(root)
    except McpError as exc:
        return {"error": [str(exc)]}
    return {
        "stdio": [s.name for s in specs if s.is_stdio],
        "http": [s.name for s in specs if s.is_http],
    }
