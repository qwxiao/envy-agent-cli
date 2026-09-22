"""模块3 · 工作区增强工具：搜索、精确编辑、执行命令。

三个工具都不是"为了数量"加的，每一个都带出一个设计话题：

| 工具 | 引出的话题 |
|---|---|
| `grep` | 正则能力 + **大输出必须截断** |
| `edit_file` | **精确替换 vs 覆盖写**的取舍 |
| `run_command` | **命令禁止名单 + 不做 shell 解析** |

与 `builtin.py` 同一条纪律：**handler 里没有任何治理逻辑**——
不校验必填参数、不判路径权限、不做重试。全部归 `ToolRuntime`。
handler 只负责"把这件事做成"，失败就抛出自然异常。
"""

import re
import shutil
import subprocess
from pathlib import Path

from envy_agent_cli.tools.registry import register
from envy_agent_cli.tools.spec import Permission, RegisteredTool, Risk, ToolSpec

#: 单次输出的字符上限。**超过就截断并如实说明**——
#: 一条 `grep -r` 能产出几兆文本，原样回灌会把上下文一次打满，
#: 而且模型真正需要的信息大概率就在前几屏里。
MAX_OUTPUT_CHARS = 8_000

#: `grep` 默认最多返回多少条命中
DEFAULT_MAX_MATCHES = 100

#: 遍历时跳过的目录。**不是"优化"，是正确性**——
#: 不跳过 `.git` 的话，一次 grep 会把二进制对象全读一遍，既慢又全是噪音。
SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "__pycache__", "node_modules",
    ".venv", "venv", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".idea", ".vscode", "dist", "build",
})

#: **硬拒名单**：无论如何都不该由 Agent 执行的命令。
#:
#: ⚠️ 这里的判据是"**执行了就不可逆、且不属于任何正常编码任务**"。
#: `rm` 不在此列——`rm tmp.txt` 是日常操作，它走审批而不是硬拒。
#: 把日常命令也硬拒，只会逼用户把审批模式整体关掉，反而更不安全。
FORBIDDEN_COMMANDS = frozenset({
    "shutdown", "reboot", "halt", "poweroff",      # 关机/重启
    "mkfs", "mkfs.ext4", "mkfs.xfs", "format",     # 格式化
    "diskpart", "fdisk", "dd",                     # 裸写磁盘
})


def register_workspace_tools(workspace: Path | str) -> None:
    """注册搜索 / 编辑 / 命令三个工具。路径白名单以 `workspace` 为根。"""
    root = Path(workspace).resolve()

    def grep(pattern: str, path: str = ".", max_matches: int = DEFAULT_MAX_MATCHES) -> str:
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            # 抛出带原因的异常，模型能自己改正则（对接输出契约纠错）
            raise ValueError(f"正则表达式不合法：{exc}") from exc

        target = _target(root, path)
        if not target.exists():
            raise FileNotFoundError(f"路径不存在：{path}")

        hits: list[str] = []
        truncated = False
        for file in _iter_files(target):
            try:
                text = file.read_text(encoding="utf8", errors="strict")
            except (UnicodeDecodeError, OSError):
                continue                      # 二进制或读不了：跳过，不让它中断整次搜索
            for number, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    shown = line.strip()
                    if len(shown) > 200:
                        shown = shown[:200] + "…"
                    hits.append(f"{_relative(file, root)}:{number}: {shown}")
                    if len(hits) >= max_matches:
                        truncated = True
                        break
            if truncated:
                break

        if not hits:
            return f"没有匹配 {pattern!r} 的内容。"
        body = "\n".join(hits)
        tail = f"\n…（已达上限 {max_matches} 条，结果被截断）" if truncated else ""
        return body + tail

    def edit_file(path: str, old_string: str, new_string: str) -> str:
        if not old_string:
            raise ValueError("old_string 不能为空——空串会匹配到每一处")
        if old_string == new_string:
            raise ValueError("old_string 与 new_string 相同，这次编辑没有任何效果")

        target = _target(root, path)
        if not target.is_file():
            raise FileNotFoundError(f"文件不存在：{path}")

        content = target.read_text(encoding="utf8")
        occurrences = content.count(old_string)
        if occurrences == 0:
            raise ValueError(
                "没找到 old_string。注意它必须与文件内容**逐字符一致**"
                "（含缩进与换行）；建议先 read_file 看清原文再改。"
            )
        if occurrences > 1:
            # ⚠️ 多处匹配时**拒绝执行**而不是"替换第一处"——
            # 猜错一处就是一处静默的错误修改，而模型本该给出足够定位的上下文
            raise ValueError(
                f"old_string 匹配到 {occurrences} 处，无法确定改哪个。"
                f"请把它扩展到能唯一定位（多带几行上下文）。"
            )

        target.write_text(content.replace(old_string, new_string), encoding="utf8")
        return f"已修改 {_relative(target, root)}（替换 1 处）"

    def run_command(args: list[str], cwd: str = ".") -> str:
        if not args:
            raise ValueError("args 不能为空")
        program = Path(args[0]).stem.lower()
        if program in FORBIDDEN_COMMANDS:
            raise PermissionError(
                f"命令 {args[0]!r} 在禁止名单里（不可逆的系统级操作），本工具不会执行它"
            )
        executable = shutil.which(args[0])
        if executable is None:
            raise FileNotFoundError(f"找不到可执行文件：{args[0]}")

        workdir = _target(root, cwd)
        if not workdir.is_dir():
            raise FileNotFoundError(f"工作目录不存在：{cwd}")

        # ⚠️ **不走 shell**：参数以列表传入，`subprocess` 直接 exec。
        # 少了一层解析就少了一整类注入（`;`、`&&`、`` ` `` 全是普通字符）。
        # 代价是不支持管道与重定向——需要那些时，说明该用 grep 工具，而不是拼 shell。
        try:
            completed = subprocess.run(
                [executable, *args[1:]],
                cwd=workdir, capture_output=True, text=True,
                # ⚠️ **不指定 encoding**：子进程用什么编码写，我们就用什么编码读。
                # Windows 上子进程（含 python）默认按 GBK 输出，
                # 强行按 UTF-8 读会把中文全变成替换字符——解码太"讲究"反而错。
                errors="replace", timeout=60,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"命令执行超过 60 秒被中止：{args[0]}") from exc

        parts: list[str] = []
        if completed.stdout:
            parts.append(completed.stdout.rstrip())
        if completed.stderr:
            parts.append(f"[stderr]\n{completed.stderr.rstrip()}")
        body = "\n".join(parts) or "（命令没有输出）"
        if completed.returncode != 0:
            body += f"\n[退出码 {completed.returncode}]"
        return _truncate(body)

    register(RegisteredTool(
        spec=ToolSpec(
            name="grep",
            description=(
                "在文件里按正则表达式搜索内容，返回 `文件:行号: 内容` 格式的命中。"
                "适合找定义、找调用点、找某个字符串出现在哪。"
                "只搜文本文件，二进制与常见构建目录会被跳过。"
            ),
            input_model={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "正则表达式，如 `def \\w+_tool`"},
                    "path": {"type": "string", "description": "搜索起点，文件或目录，默认当前目录"},
                    "max_matches": {"type": "integer", "minimum": 1,
                                    "description": f"最多返回多少条，默认 {DEFAULT_MAX_MATCHES}"},
                },
                "required": ["pattern"],
            },
            permission=Permission(read_paths=(root,), path_args=("path",)),
            risk=Risk.LOW,
            required_keys=("pattern",),
        ),
        handler=grep, read_only=True, concurrency_safe=True,
    ))

    register(RegisteredTool(
        spec=ToolSpec(
            name="edit_file",
            description=(
                "把文件里的 old_string **精确替换**成 new_string。"
                "old_string 必须与原文逐字符一致，且**在文件里唯一**（否则会拒绝执行）。"
                "改动小的地方用这个，不要用 write_file 整篇覆盖。"
            ),
            input_model={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对工作区的文件路径"},
                    "old_string": {"type": "string", "description": "要被替换的原文，需唯一"},
                    "new_string": {"type": "string", "description": "替换成什么"},
                },
                "required": ["path", "old_string", "new_string"],
            },
            permission=Permission(write_paths=(root,), path_args=("path",)),
            risk=Risk.MEDIUM,
            required_keys=("path", "old_string", "new_string"),
            requires_approval=True,
        ),
        handler=edit_file, read_only=False, concurrency_safe=False,
    ))

    register(RegisteredTool(
        spec=ToolSpec(
            name="run_command",
            description=(
                "执行一条外部命令，参数以数组传入（如 [\"python\", \"--version\"]）。"
                "**不支持管道、重定向、通配符**——需要那些时请改用专门的工具。"
                "输出过大会被截断，退出码非零时会在末尾标出。"
            ),
            input_model={
                "type": "object",
                "properties": {
                    "args": {"type": "array", "items": {"type": "string"},
                             "description": "命令与参数，第一项是可执行文件名"},
                    "cwd": {"type": "string", "description": "工作目录，默认工作区根"},
                },
                "required": ["args"],
            },
            risk=Risk.HIGH,
            required_keys=("args",),
            requires_approval=True,
        ),
        handler=run_command, read_only=False, concurrency_safe=False, timeout=70.0,
    ))


# ---------------------------------------------------------------- 内部


def _target(root: Path, path: str) -> Path:
    """相对路径按工作区解析；绝对路径原样使用（是否越界由 Runtime 的二级鉴权判定）。"""
    candidate = Path(path)
    return candidate if candidate.is_absolute() else root / candidate


def _relative(path: Path, root: Path) -> str:
    """给模型看的路径：工作区内用相对路径，区外只给文件名（不泄露本机目录结构）。

    ⚠️ 用 `as_posix()` 而不是 `str()`：Windows 上 `str()` 给的是 `src\\a.py`，
    而模型看到的应该是 `src/a.py`——同一份代码在不同平台给出不同格式的路径，
    会让"照着返回的路径去调下一个工具"这种用法时灵时不灵。
    """
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def _iter_files(target: Path):
    """产出待搜索的文本文件。目录会递归，但跳过 `SKIP_DIRS`。"""
    if target.is_file():
        yield target
        return
    for child in sorted(target.rglob("*")):
        if any(part in SKIP_DIRS for part in child.parts):
            continue
        if child.is_file():
            yield child


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + f"\n…（输出被截断，原长 {len(text)} 字符）"
