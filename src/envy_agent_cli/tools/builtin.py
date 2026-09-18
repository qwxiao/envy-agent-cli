"""模块3 · 内置工具：文件域三件套。

| 工具 | 读写 | 风险 | 理由 |
|---|---|---|---|
| `read_file` | 只读 | low | 无副作用，可并发 |
| `list_dir` | 只读 | low | 无副作用，可并发 |
| `write_file` | 写 | medium | 有副作用但可恢复；碰磁盘要人工确认 |

**注意 handler 里没有任何治理逻辑**：没有必填参数检查、没有路径校验、没有 try/except 兜底。
原因：**治理只写一遍**——参数校验、权限判定、超时重试、审计全在 `ToolRuntime`。
handler 只负责"把这件事做成"，失败就抛出自然异常，由 Runtime 分类成结构化错误。

> 工具越少，schema 越省上下文、模型选错率越低——扩展能力交给 MCP，不堆内置工具。
"""

from pathlib import Path

from envy_agent_cli.tools.registry import register
from envy_agent_cli.tools.spec import Permission, RegisteredTool, Risk, ToolSpec

MAX_READ_BYTES = 200_000


def register_builtin_tools(workspace: Path | str) -> None:
    """把内置工具注册进全局注册表。

    路径白名单以 `workspace` 为根——这是 `Permission` 在本项目的实际语义（能操作哪些路径）。
    重复调用会覆盖同名的旧注册（便于测试与切换工作区）。
    """
    root = Path(workspace).resolve()

    def read_file(path: str, max_bytes: int = MAX_READ_BYTES) -> str:
        target = _target(root, path)
        data = target.read_bytes()
        text = data[:max_bytes].decode("utf8", errors="replace")
        if len(data) > max_bytes:
            text += f"\n…（已截断，原文件 {len(data)} 字节，本次读取 {max_bytes} 字节）"
        return text

    def list_dir(path: str = ".") -> str:
        target = _target(root, path)
        entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name))
        if not entries:
            return f"{path} 是空目录"
        return "\n".join(f"{e.name}/" if e.is_dir() else e.name for e in entries)

    def write_file(path: str, content: str) -> str:
        target = _target(root, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf8")
        return f"已写入 {len(content)} 个字符到 {target.name}"

    register(RegisteredTool(
        spec=ToolSpec(
            name="read_file",
            description="读取文本文件内容。",
            input_model={"type": "object",
                         "properties": {"path": {"type": "string", "description": "相对工作区的路径"},
                                        "max_bytes": {"type": "integer"}},
                         "required": ["path"]},
            permission=Permission(read_paths=(root,), path_args=("path",)),
            risk=Risk.LOW,
            required_keys=("path",),
        ),
        handler=read_file, read_only=True, concurrency_safe=True,
    ))

    register(RegisteredTool(
        spec=ToolSpec(
            name="list_dir",
            description="列出目录内容。",
            input_model={"type": "object",
                         "properties": {"path": {"type": "string"}}},
            permission=Permission(read_paths=(root,), path_args=("path",)),
            risk=Risk.LOW,
        ),
        handler=list_dir, read_only=True, concurrency_safe=True,
    ))

    register(RegisteredTool(
        spec=ToolSpec(
            name="write_file",
            description="写入文件（覆盖）。",
            input_model={"type": "object",
                         "properties": {"path": {"type": "string"},
                                        "content": {"type": "string"}},
                         "required": ["path", "content"]},
            permission=Permission(write_paths=(root,), path_args=("path",)),
            risk=Risk.MEDIUM,
            required_keys=("path", "content"),
            requires_approval=True,   # 碰磁盘默认要人工确认（auto 模式下也会问）
        ),
        handler=write_file,
        read_only=False,          # 写操作有副作用 → 串行执行
        concurrency_safe=False,
    ))


def _target(root: Path, path: str) -> Path:
    """相对路径按工作区解析；绝对路径原样使用（是否越界由 Runtime 的二级鉴权判定）。"""
    candidate = Path(path)
    return candidate if candidate.is_absolute() else root / candidate
