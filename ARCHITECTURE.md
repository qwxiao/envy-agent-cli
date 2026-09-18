# 架构封版（Interface Freeze）

> **封版原则：接口封死，实现可迭代。**
> 本文档里的接口一旦签字，后续只允许"填实现"，不允许改签名。要改签名必须走一次显式的解封记录。

---

## 一、分层：Harness 六层（我们只实现前三层）

```
VENDOR STREAM → MODEL ADAPTER → AGENT LOOP → EVENT STORE → SSE GATEWAY → CLIENT
（供应商流）    （解析归一化）   （驱动模型工具）（持久化重放） （编码订阅）  （Web/CLI）
   └────────────── 本次实现 ──────────────┘   └──── 暂不实现（服务化形态）────┘
```

**核心一句话：Loop 不懂 HTTP，Client 不懂模型 SDK，Gateway 不决定任务是否完成。**

| 层 | SHOULD DO | SHOULD NOT |
|---|---|---|
| 传输（`llm/client.py`） | 发请求、收流、按协议分帧、吐 typed 事件 | ❌ 不打印 ❌ 不攒 tool_calls ❌ 不判断任务是否结束 |
| 模型适配（`llm/adapters/`） | 把厂商差异归一化成统一事件流 | ❌ 不拼 SSE ❌ 不渲染 ❌ 不含业务路由 |
| 编排（`loop/react.py`） | 消费事件、攒工具调用、决定继续/结束、存消息 | ❌ **不执行工具** ❌ 不碰 HTTP/SDK ❌ 不依赖 Request 对象 |
| 工具（`tools/`） | 查找、校验、鉴权、超时、重试、审计、归一化 | ❌ 不替业务方定义权限规则 ❌ 不假设参数天然正确 |

> 为什么不做 Event Store / SSE Gateway / Web Client：CLI 是**单进程工具**，内置适配层就够，拆服务是过度设计。
> 判断标准：出现**多应用共享 / 多租户 / 统一密钥路由限流计费**时才拆。将来要支持 Web 端再拆。

## 二、四角色边界（工具层的宪法）

| 角色 | 负责什么 | **不能替谁决定什么** |
|---|---|---|
| 模型 | 选候选工具、生成候选参数、读结果 | ❌ 不能决定自己有没有权限；❌ 不能直接执行函数 |
| 工具函数 | 完成一项确定的业务能力 | ❌ 不能假设调用参数天然正确；❌ 不负责 Loop |
| **Tool Runtime** | 查找、校验、鉴权、超时、重试、审计、归一化 | ❌ 不能替业务方定义权限和风险规则 |
| Agent Loop | 保存消息与状态、把 Tool Result 送回模型、决定继续或结束 | ❌ **不应绕过 Runtime 直接调用业务函数** |

> **模型返回的 Tool Call 只是一个候选动作，它与用户输入一样，都属于不可信数据。**
> 这句话解释了为什么必须有 Runtime——模型只能"提议"，不能"下令"。

## 三、三个契约（解耦的确切含义 = 不同消费者看到不同信息面）

```
① 工具 → Runtime（注册契约，ToolSpec 七字段）
   name / description / input_model / output_model / error_model / permission / risk

② Runtime → Loop（执行契约）
   execute(call, trace) -> ToolResult(success) | ToolError(code, retryable)

③ Loop → 模型（模型契约，只暴露三样）
   name + description + input_schema
```

| 契约 | 能看到 | **看不到** |
|---|---|---|
| 模型契约（给模型） | 名字、说明、参数结构 | ❌ Python 实现 ❌ 权限规则 ❌ 重试逻辑 ❌ 风险等级 |
| 执行契约（给 Loop） | 成功结果 / 结构化错误 | ❌ 工具内部实现 |
| 注册契约（给 Runtime） | 全部（含权限、风险、错误模型） | — |

## 四、ToolSpec 七字段（落地顺序，三批）

| 批次 | 字段 | 何时加 | 理由 |
|---|---|---|---|
| 🔴 第一批 | `name` `description` `input_model` `permission` `risk` | 现在 | 直接对应 Runtime 职责；`risk` 用来替代原来的单 bool `requires_approval` |
| 🟡 第二批 | `output_model` `error_model` | 随结果归一化一起做 | 要配合返回值从 `str` 改成结构化 `ToolResult`（破坏性改动） |
| ⚪ 不进字段 | `handler` | — | 属**实现层**，不是 schema 层；模型永远看不到 |

**`permission` 语义按本项目改写**：多用户系统里它通常是用户权限（`order:read`），
但本项目是单人 CLI、没有多用户体系，所以语义是 **"能操作哪些工作区路径"**（路径白名单），
不是"哪个用户能调"。

```
Tool = 声明层（ToolSpec：可序列化 / 可审计 / 可给模型看）
     + 实现层（handler：机器执行，模型永远看不到）
     + 执行层（ToolRuntime 的七职责）
```

## 五、结果与错误契约

```python
ToolResult(content: str, is_error: bool = False, tool_call_id: str | None = None)
ToolError(code: ErrorCode, message: str, retryable: bool, tool_call_id: str | None = None)
```

`ErrorCode`：`INVALID_ARGUMENT` / `PERMISSION_DENIED` / `REJECTED_BY_USER` / `TIMEOUT` /
`UPSTREAM_ERROR` / `NOT_FOUND` / `UNKNOWN`

**重试依据 `code` 而不是异常类型**：只有 `TIMEOUT` / `UPSTREAM_ERROR` 允许重试，
`INVALID_ARGUMENT` / `PERMISSION_DENIED` 重试一万次也没用。

**三份 Schema 不可合并**：input 拦错误调用 / output 固定成功事实 / error 驱动失败恢复。
缺一个，Loop 就得解析自然语言或猜下一步——而 Loop 的天职是**确定性编排**，一旦要"猜"就退化成另一个 LLM。

> 前提提醒：这个结论只在**输出要被程序消费**时成立；如果输出只给人看，确实不需要这么严。

## 六、链路追踪：三级粒度（P0）

| 级别 | 标识 | 对应什么 |
|---|---|---|
| 任务级 | `trace_id` | 一次完整的用户任务（`run(question)` 开始到结束） |
| 轮次级 | `span_id = f"{trace_id}:r{round_no}"` | ReAct 的一轮 |
| 调用级 | `parent_span = span_id` | 一次工具调用 |

**为什么分三级**：粒度不对就没法归因——只看 trace 不知道哪一轮出的问题，只看轮次分不清是模型的错还是工具的错。
分级之后一次失败能精确落到"第 3 轮第 2 个工具调用"。

**这是回归评测的地基**：trace 串起来的审计日志 = Trajectory = 评测数据源。没有 trace 就没有轨迹级的评测。

## 七、审计日志（JSONL）

每次工具执行落**一行** JSON（不是 print），字段固定：

```json
{"ts":"...","trace_id":"trace-xxxx","span_id":"trace-xxxx:r2","parent_span":"trace-xxxx:r2",
 "tool":"read_file","args_digest":"...","status":"ok|error","error_code":null,
 "retryable":false,"duration_ms":12.3}
```

写成结构化 JSONL 的唯一理由：**给回归评测消费**。改完 Prompt 跑回归集，能从轨迹里看出是哪一步退化了。

## 八、事件协议（模块1，已封版）

**六种事件**：`TextDelta` / `ReasoningDelta` / `ToolCallDelta` / `MessageEnd` / `Usage` / `Error`。

```python
TextDelta(text)                                     # 正文增量（产出）
ReasoningDelta(text)                                # 思维链增量（过程证据）
ToolCallDelta(index, is_first, call_id, name, arguments)  # 工具调用碎片
MessageEnd(stop_reason)                             # tool_use | end_turn | max_tokens | stop_sequence
Usage(prompt_tokens, completion_tokens, total_tokens)
Error(message, code, retryable, status_code)        # 连异常都是数据
```

**四条铁律**：
1. 流里只做 `arguments += 碎片`，**绝不中途 `json.loads`**（单片不是合法 JSON）——攒的动作在编排层。
2. 结束判定看 `stop_reason`，**不靠"tool_calls 空不空"猜**；`max_tokens` 是**失败信号**，不许"尽量解析截断的 JSON"。
3. **第一片用 `is_first` 字段标记，不新增事件类型**——避免下游 `isinstance` 分支膨胀。
4. **错误码与工具层同形状**（`(code, retryable)` 二元组），Loop 只处理一种错误形态。

### 错误码（`LLMErrorCode`）

`invalid_request`（400，本地修）/ `auth_failed`（401/403，终止）/ `rate_limited`（429，退避重试）/
`server_error`（5xx，退避重试）/ `timeout`（可重试）/ `content_filtered`（终止）/
`output_invalid`（走输出契约层纠错）/ `unknown`。

> **`retryable` 由 Adapter 判定并显式给出，Loop 不推导。** 只有 Adapter 掌握完整上下文
> （状态码、错误体、尝试次数）；让 Loop 去字符串里找 "429" 是脆弱的。

### 重试的四段分工（谁管什么，别搞混）

| 层 | 职责 |
|---|---|
| Adapter | **纯翻译**：不重试、不路由、不降级 |
| 工厂 | **路由**：选哪个 Adapter |
| `RetryPolicy` | **重试**：读 `Error.retryable` 决定再试与退避（独立成类，跨 Adapter 复用） |
| Loop | **降级**：重试预算耗尽后决定（换模型 / 缩上下文 / 转人工） |

流式重试的硬约束：**只有"还没吐出任何事件就失败"才允许重试**——流吐了一半再重来，
上层会收到重复正文/工具碎片（等价于"非幂等操作盲目重试"）。

### 输出契约层

`llm/validator.py`：① 定义输出契约 ② 结构校验 ③ 生成纠错提示文本。
**纠错重试由 Loop 驱动**——validator 自己不发起请求，保持"模块1 不决策"的边界。

判据一句话：**"这个校验需要懂业务吗？"** 需要 → 上层；不需要 → 这里（只做结构/类型，不做业务规则）。

## 九、明确不做（写清"什么条件下才做"）

| 项 | 结论 | 条件 |
|---|---|---|
| 断线续传 | ❌ 不做 | LLM 生成不可重放，协议上有 `Last-Event-ID` 但服务端无重放能力。真要恢复：前端暂存已生成内容，重连后先推存量 |
| 背压 | ❌ 不做 | 同步生成器 + 单客户端天然跟得上。留注释：未来接多客户端需在 SSE 写入层加队列 + 水位控制 |
| 独立 Gateway / Event Store | ❌ 不做 | 单进程 CLI 不需要；出现多应用共享 / 多租户 / 统一密钥路由计费时才拆 |
| `ProviderSwitch` 事件 | ❌ 不加 | 换厂商是工厂内部决策。暴露后上层会写出 `if provider == ...`，等于依赖实现细节 |
| MCP 工具接入 | 后续 | 外部 MCP 工具与内置工具走**同一个执行入口**（`ToolRuntime.execute`），届时不改 Loop、不改 Runtime |
| 摘要压缩器（模块4） | 接口待定 | 目录占位 |
