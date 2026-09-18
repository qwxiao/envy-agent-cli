"""模块3 · 工具声明层（注册契约）。

**声明层与实现层必须分开**：

```
Tool = 声明层（ToolSpec：可序列化 / 可审计 / 可给模型看）
     + 实现层（handler：机器执行，模型永远看不到）
     + 执行层（ToolRuntime 的七职责）
```

七字段的落地顺序（见 ARCHITECTURE.md 第四节）：
- 🔴 第一批（现在）：`name` `description` `input_model` `permission` `risk`
- 🟡 第二批（随结果归一化一起做）：`output_model` `error_model`
- ⚪ 不进字段：`handler`（它不属于 schema 层）
"""

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable


class Risk(str, Enum):
    """调用前需要怎样的控制。替代原来的单 bool `requires_approval`——粗粒度会逼出硬编码。"""

    LOW = "low"        # 只读、无副作用：直接放行
    MEDIUM = "medium"  # 有副作用但可恢复（写文件）：按 HITL 策略决定
    HIGH = "high"      # 不可逆 / 影响面大（执行命令）：默认必须人工确认


@dataclass(frozen=True, slots=True)
class Permission:
    """工具的权限声明。

    ⚠️ **语义按本项目改写**：多用户系统里它通常是用户权限（`order:read`），
    但这个 CLI 是**单人工具、没有多用户体系**，所以语义是
    "**能操作哪些工作区路径**"——一份路径白名单。

    Runtime 只**执行**这份策略，不替业务方**定义**策略。
    **两个路径集合都为空 = 不做路径约束**（由 `risk` + HITL 兜底）。

    Attributes:
        path_args: 哪些参数名里装的是路径。Runtime 只检查这些参数，
            不去猜其余字符串（猜错会把普通文本当路径拦掉）。
    """

    read_paths: tuple[Path, ...] = ()
    write_paths: tuple[Path, ...] = ()
    path_args: tuple[str, ...] = ("path",)


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """注册契约：工具**声明**自己是什么。

    这份信息给 Runtime 看（含权限、风险、本地校验器），其中只有前三样会被投影成模型契约。
    """

    name: str
    description: str
    input_model: Any                      # JSON Schema dict：**给模型看**的参数结构
    permission: Permission = field(default_factory=Permission)
    risk: Risk = Risk.LOW
    output_model: Any | None = None       # 第二批：固定成功事实
    error_model: Any | None = None        # 第二批：驱动失败恢复
    requires_approval: bool = False
    """工具**自身**要求人工确认（与 `risk` 无关的另一条理由，比如"碰磁盘"）。
    声明在这里，**由调用方注入的 HITL 模式决定要不要问**——Runtime 不替业务方定义策略。"""

    required_keys: tuple[str, ...] = ()
    """必有参数名。轻量校验用——比 `validator` 低一档，两者都没声明就不做参数约束。"""

    validator: Callable[[dict], dict] | None = None
    """本地校验器（Pydantic model 的 `model_validate` 或任意 callable）。

    属**注册契约**，模型永远看不到——若把它并进 `input_model`，
    模型就会看到本地的字段约束细节，破坏三契约的信息面分离。
    校验失败要抛出带"字段名 + 期望/实际"的异常，好让模型自己改（对接输出契约纠错）。
    """


@dataclass(slots=True)
class RegisteredTool:
    """注册表里的一条记录：声明 + 实现 + 执行参数。

    执行参数（读写属性 / 并发安全 / 超时）不属七字段契约——它们是**给 Runtime 的执行提示**，
    模型看不到，也不参与 schema 投影。
    """

    spec: ToolSpec
    handler: Callable[..., Any]
    read_only: bool = True
    concurrency_safe: bool = True
    timeout: float = 30.0
    max_retries: int = 2
