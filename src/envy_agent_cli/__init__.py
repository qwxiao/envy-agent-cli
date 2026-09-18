"""envy-agent-cli · 终端 Coding Agent CLI（类 Claude Code），核心引擎自研。

分层遵循 Harness 六层架构的前三层（详见 ARCHITECTURE.md）：

    LLM 客户端（传输与协议） → Agent Loop（编排） → Tool Runtime（受控执行）

三条不能破的边界：
1. 模型不能决定自己有没有权限 —— Tool Call 是候选动作，属于不可信数据；
2. Loop 不能绕过 Runtime 直接调用工具函数；
3. 模型不能看到 handler 与权限规则 —— 模型契约只暴露 name + description + input_schema。
"""

__version__ = "0.1.0"
