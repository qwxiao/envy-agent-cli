# envy-agent-cli

终端 Coding Agent CLI（类 Claude Code）——**核心引擎自研**。

一个跑在终端的 AI 编程体：LLM 流式客户端 + ReAct 主循环 + 受控的工具执行系统（Tool Runtime）。
分层、契约、边界都在 [ARCHITECTURE.md](ARCHITECTURE.md) 里封版。

## 当前状态

| 模块 | 状态 | 说明 |
|---|---|---|
| 模块1 LLM 流式客户端 | 🟢 **实现完成** | 事件协议 / 传输层（SSE 分帧）/ Adapter 归一化 / 工厂 / RetryPolicy / 输出契约层 |
| 模块2 ReAct 主循环 | 🟢 **实现完成** | 四重预算 / 两级打转检测 / 契约纠错独立预算 / 七种终止状态 / 渲染注入 |
| 模块3 工具层（Tool Runtime） | 🟢 **实现完成** | 八步执行顺序 / 两级鉴权 / HITL + 拒绝升级 / 只读并发 / 超时熔断 / 审计 / 脱敏 |
| 模块4 上下文压缩器 | 🟢 **实现完成** | 工具包边界切分 / 双阈值双触发 / system 永不进摘要 / 两级收缩 / 摘要失败回退 |

**现在是一个能真干活的 Agent**：

```bash
uv run envy --hitl never "看看当前目录有哪些文件，再读一下 README 的前 20 行"
```

模型自己决定调哪个工具、看结果、再决定下一步；每次调用都过 Runtime 的八步治理，
审计轨迹落在 `audit/cli.jsonl`（用 trace_id 可串起一次完整任务）。

长任务会撑爆窗口，所以每轮发请求前先过压缩器：超预算就把旧轮次摘要掉。

```bash
uv run envy --context-window 8000 "把这个项目的每个源文件都读一遍，然后总结架构"   # 小窗口逼出压缩
uv run envy --no-compact "..."                                                     # 关掉压缩（对照）
```

> **为什么先封接口再写实现**：架构没定就写，写多少废多少。接口定了，实现怎么写都不会跑偏。

## 目录结构

```
src/envy_agent_cli/
├── llm/                模块1 · 传输 + 协议 + 适配
│   ├── transport.py      SSE 分帧（字节 → 事件块，只认协议不认模型）
│   ├── events.py         事件协议（六种，Error 带 code/retryable）
│   ├── params.py         ChatParams（显式参数对象，可序列化可版本化）
│   ├── adapter.py        ChatModel 契约 + OpenAI 兼容实现（厂商差异归一化）
│   ├── factory.py        工厂（路由 / fallback 的唯一发生地）
│   ├── retry.py          RetryPolicy（跨 Adapter 复用，独立成类）
│   └── validator.py      输出契约层（结构校验 + 纠错提示）
├── loop/               模块2 · 编排
│   └── react.py          ReAct 主循环（消费事件、攒调用、决定继续或结束）
├── tools/              模块3 · 受控执行
│   ├── spec.py           ToolSpec 七字段（声明层）
│   ├── registry.py       注册表 + 模型契约生成
│   ├── result.py         ToolResult / ToolError（执行契约）
│   ├── runtime.py        ToolRuntime —— 唯一执行入口（七职责）
│   └── builtin.py        内置工具（read_file / list_dir / write_file）
├── context/            模块4 · 上下文预算与压缩
│   ├── compactor.py      预算 / 切分红线 / 两级收缩（纯计算，零 IO）
│   ├── summary_rule.py   规则摘要引擎（默认档）
│   └── summary_llm.py    LLM 摘要引擎（可选档，唯一碰模型 IO 的地方）
├── trace.py            三级链路追踪（跨层共享的叶子模块）
└── audit/              审计日志（JSONL，给评测当数据源）
```

> `trace.py` 放在顶层而不是 `loop/` 下：追踪上下文是跨层共享的（循环生成、工具消费、审计落盘）。
> 放 `loop/` 会让 `tools` 反向依赖 `loop`，既造成循环 import，也破坏分层方向——**依赖只能自上而下**。

## 三条不能破的边界

1. **模型不能决定自己有没有权限** —— Tool Call 是候选动作，和用户输入一样属于不可信数据。
2. **Loop 不能绕过 Runtime 直接调用工具函数** —— 绕过去，校验/权限/审计全部失效。
3. **模型不能看到 handler 和权限规则** —— 模型契约只暴露 `name + description + input_schema`。

## 跑测试

```bash
python -m pytest tests/ -q
```

骨架阶段的测试做两件事：**接口存在性**（契约没被人偷偷改掉）和**边界守卫**
（静态检查 `react.py` 里没有直接调用工具 handler）。
