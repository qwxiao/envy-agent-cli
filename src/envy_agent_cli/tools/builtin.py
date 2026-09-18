"""模块3 · 内置工具：文件域三件套。

每个工具都要**声明完整**（七字段），风险分级不能靠感觉：

| 工具 | 读写 | 风险 | 理由 |
|---|---|---|---|
| `read_file` | 只读 | low | 无副作用，可并发 |
| `list_dir` | 只读 | low | 无副作用，可并发 |
| `write_file` | 写 | medium | 有副作用但可恢复；碰磁盘需人工确认 |

> 工具越少，schema 越省上下文、模型选错率越低——扩展能力交给 MCP，不堆内置工具。
"""

from pathlib import Path

from envy_agent_cli.tools.registry import register
from envy_agent_cli.tools.spec import Permission, RegisteredTool, Risk, ToolSpec


def read_file(path: str, max_bytes: int = 200_000) -> str:
    """读取文本文件内容。"""
    raise NotImplementedError


def list_dir(path: str = ".") -> str:
    """列出目录内容。"""
    raise NotImplementedError


def write_file(path: str, content: str) -> str:
    """写入文件（覆盖）。"""
    raise NotImplementedError


def register_builtin_tools(workspace: Path) -> None:
    """把内置工具注册进全局注册表。

    路径白名单以 `workspace` 为根——这是 `Permission` 在本项目的实际语义（能操作哪些路径）。
    """
    register(RegisteredTool(
        spec=ToolSpec(
            name="read_file",
            description="读取文本文件内容。",
            input_model={"type": "object", "properties": {"path": {"type": "string"},
                                                          "max_bytes": {"type": "integer"}},
                         "required": ["path"]},
            permission=Permission(read_paths=(workspace,)),
            risk=Risk.LOW,
        ),
        handler=read_file,
        read_only=True,
        concurrency_safe=True,
    ))
    register(RegisteredTool(
        spec=ToolSpec(
            name="list_dir",
            description="列出目录内容。",
            input_model={"type": "object", "properties": {"path": {"type": "string"}}},
            permission=Permission(read_paths=(workspace,)),
            risk=Risk.LOW,
        ),
        handler=list_dir,
        read_only=True,
        concurrency_safe=True,
    ))
    register(RegisteredTool(
        spec=ToolSpec(
            name="write_file",
            description="写入文件（覆盖）。",
            input_model={"type": "object", "properties": {"path": {"type": "string"},
                                                          "content": {"type": "string"}},
                         "required": ["path", "content"]},
            permission=Permission(write_paths=(workspace,)),
            risk=Risk.MEDIUM,
        ),
        handler=write_file,
        read_only=False,        # 写操作有副作用 → 串行执行
        concurrency_safe=False,
    ))
