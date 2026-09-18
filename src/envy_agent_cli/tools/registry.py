"""模块3 · 注册表：管"有哪些工具"。

**加工具不改 Loop**——注册表是开闭原则的落地，相当于 Java 里的 ApplicationContext：
Loop 按名字查表，永远不知道表里具体有哪些实现。

**投影规则（模型契约）**：`to_model_schemas()` 只暴露 **name + description + input_schema** 三样。
工具的 Python 实现、权限规则、风险等级、重试逻辑，**模型永远看不到**——
"解耦"在接口层面的确切含义就是：不同的消费者看到不同的信息面。
"""

from envy_agent_cli.tools.spec import RegisteredTool

_REGISTRY: dict[str, RegisteredTool] = {}


def register(tool: RegisteredTool) -> None:
    """注册一个工具。同名覆盖（便于测试替换）。"""
    _REGISTRY[tool.spec.name] = tool


def get(name: str) -> RegisteredTool | None:
    """按名查找。找不到返回 None——由 Runtime 转成结构化错误回灌模型，而不是抛异常。"""
    return _REGISTRY.get(name)


def all_names() -> list[str]:
    """已注册工具名（用于"未知工具"时给模型列出可选项，让它自我修正）。"""
    return sorted(_REGISTRY)


def all_tools() -> list[RegisteredTool]:
    return list(_REGISTRY.values())


def to_model_schemas() -> list[dict]:
    """投影成模型契约（OpenAI 兼容的 tools 参数）。

    这是"模型能看到什么"的**唯一定义处**——`name` + `description` + `input_schema` 三样。
    权限、风险等级、重试策略、Python 实现，模型一律看不到。
    """
    return [
        {
            "type": "function",
            "function": {
                "name": tool.spec.name,
                "description": tool.spec.description,
                "parameters": tool.spec.input_model,
            },
        }
        for tool in _REGISTRY.values()
    ]
