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
| 传输（`llm/transport.py`） | 发请求、收流、按协议分帧、吐 typed 事件 | ❌ 不打印 ❌ 不攒 tool_calls ❌ 不判断任务是否结束 |
| 模型适配（`llm/adapter.py`） | 把厂商差异归一化成统一事件流 | ❌ 不拼 SSE ❌ 不渲染 ❌ 不含业务路由 |
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
ToolResult(content: str, is_error: bool = False, tool_call_id: str | None = None,
           executed: bool = True)          # executed=False：未注册 / 参数非法 / 权限拒绝
ToolError(code: ErrorCode, message: str, retryable: bool, tool_call_id: str | None = None)
```

**批量入口的调用契约**（编排层 → Runtime）：

```python
runtime.execute_all(calls, trace) -> list[ToolResult]      # 返回与入参一一对应
runtime.execute(call, trace)      -> ToolResult
# calls 元素：{"seq": int, "id", "name", "arguments": dict}
#             或 {"seq": int, "id", "name", "arguments": None, "parse_error": str}
```

`seq` 是全局调用序号（跨轮累加），供 Runtime 生成调用级 span 时排序与追溯。

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

一条记录一行 JSON（不是 print），用 `kind` 区分来源：

| `kind` | 谁写 | 载荷 |
|---|---|---|
| `tool` | `ToolRuntime` | `tool` / `args_digest` / `status` / `error_code` / `retryable` / `executed` / `duration_ms` |
| `reasoning` | 编排层 | `detail.text`（推理内容；默认开，可配开关） |
| `stream_error` | 编排层 | `detail.planned_calls` / `complete_calls` / `partial_calls` / `error_code` |
| `stop` | 编排层 | `detail.stop_reason` / `truncated` / `iterations` / `tokens` / `tool_calls` |

```json
{"kind":"tool","ts":"...","trace_id":"trace-xxxx","span_id":"trace-xxxx:r2","parent_span_id":"...",
 "tool":"read_file","args_digest":"{\"path\":\"a.txt\"}","status":"ok","error_code":null,
 "retryable":false,"executed":true,"attempt":1,"duration_ms":12.3,"detail":null}
```

`executed` 区分"**执行了但失败**"与"**根本没执行**"（未注册 / 参数非法 / 权限拒绝）——这是两个不同的指标。

写成结构化 JSONL 的唯一理由：**给回归评测消费**。改完 Prompt 跑回归集，能从轨迹里看出是哪一步退化了。
所以审计要能回答这些数字：产生过几次非法调用、有几个调用是残缺的、推理摘要是什么、为什么终止。
`AuditLogger.trace_lines(trace_id)` 可按一次任务捞出完整轨迹。

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

### 厂商差异（真实 API 实测）

同一份适配器接不同厂商，**差异是实测出来的，不是假设的**：

| 观察项 | DeepSeek | GLM |
|---|---|---|
| 思维链字段 | `reasoning_content` | `reasoning_content`（同一字段名，归一化可复用） |
| 思维链体量 | 约 240 字符 | 约 400 tokens，**且计入 `max_tokens`** |
| 小 `max_tokens` 的后果 | 仍能输出正文 | **正文为空且不报错** |

GLM-5.3-Flash 同一个问题的实测：

| max_tokens | 思维链片数 | 正文片数 | stop_reason |
|---|---|---|---|
| 128 | 127 | **0** | `max_tokens` |
| 512 | 362 | 35 | `end_turn` |
| 2048 | 336 | 35 | `end_turn` |

**结论：推理型模型的 `max_tokens` 同时覆盖思维链与正文。** 给小了会得到"空答案且无异常"，
而 `stop_reason == max_tokens` 是这条链路上**唯一**能识别它的信号——这就是它必须被当成
失败信号（而不是正常结束）的实证理由。调用方给推理型模型留预算时，要按"思维链 + 正文"算。

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

## 九、编排层（模块2）

### 四重预算（单一轮数上限拦不住"20 轮 × 每轮 10 个调用"）

| 优先级 | 预算 | 默认值 | 防什么 |
|---|---|---|---|
| 1 | **token 预算** | 200k（从服务端 `Usage` 累计真实值） | 成本失控 |
| 2 | **轮数上限** | 20 | 轮次打转 |
| 3 | **工具调用数** | 50 | 单轮巨量 |
| 4 | **时长** | 300s | 卡死 |

四个预算耗尽后统一用 `stop_reason = "budget_exhausted"`，**具体哪一种由 `AgentResult.detail` 说明**。
工具调用数预算**先执行再判**——保证 `tool` 消息与其 `assistant` 调用成对，历史不留残缺。

### 终止原因（八种，都是数据不是异常）

| 终止原因 | `stop_reason` | `truncated` | 谁决定下一步 |
|---|---|---|---|
| 模型说完了 | `end_turn` | False | — |
| 模型要调工具 | `tool_use` | — | 执行 → 结果回灌 → 继续循环 |
| 某种预算耗尽 | `budget_exhausted` | True | 调用方（看 `detail` 区分是哪一种） |
| 轮数打满 | `max_rounds` | True | 调用方（换模型 / 拆任务 / 转人工） |
| 原地打转 | `no_progress` | True | 调用方 |
| 输出被截断 | `max_tokens` | True | 调用方（扩大预算重生成） |
| 契约未满足 | `output_contract_failed` | True | 调用方（拿原文 + 最后一次校验错误自行降级） |
| 流内错误 | `error` | True | 调用方（可重试则重试） |

**触顶不抛异常**：跑满预算是需要优雅收尾的正常情况，不是崩溃。

### 打转检测：两级指纹 + 二次机会

| 级 | 指纹 | 触发 |
|---|---|---|
| 一级 | `(工具名, 参数)` | 连续 2 轮相同 → **注入提示**，给模型换策略的机会 |
| 二级 | `(工具名, 参数)` + `(结果前 100 字)` | 连续 3 轮相同 → 确认打转，终止 |

**只看调用会误判**（外部状态变了，重试是有意义的）；**只看结果也会误判**（不同调用可能返回同样的错误）。
两级组合才既避免误判、又识别真打转。**打转也是反馈，先提示再终止**——直接终止等于剥夺模型的自我修正机会。
终止时把重复轮数、指纹、起始轮次写进 `AgentResult.detail` 便于归因。

### 输出契约纠错：独立预算

纠错次数**独立于轮数**（默认 2 次）。共用轮数会造成**归因错误**——
"模型一直不遵守契约"会表现成 `max_rounds`，看起来像"任务太复杂"，评测时会误判问题来源。
耗尽后返回 `output_contract_failed` + **原始文本** + 最后一次校验错误（调用方可自行降级）。
纠错提示必须**指出具体哪里不对**（"字段 X 类型应为 int，实际 str"），不是笼统的"请重新输出"。

### 四条容易做错的细节

1. **推理不入历史，但必须落审计**：`ReasoningDelta` 不写进 messages（推理不是对话内容，
   回传污染上下文且部分厂商会报错），**但要写审计**——否则这个事件全链路无人消费，等于死事件。
   审计默认开，可配开关。
2. **参数解析的边界**：碎片拼装与 `json.loads` 同属"把碎片变成可用结构"，留在编排层；
   **语义校验**（必填项、类型、权限、风险）归 `ToolRuntime` 的七职责。
   **Runtime 的输入契约是"结构化的调用"**，`arguments` 必须是 dict——字符串不是它的语言。
3. **参数不合法也不跳过**：照常进批次并标 `parse_error`，由 Runtime 产出结构化错误结果
   （`executed=False`）。**不派发不等于不记账**——审计要能回答"模型产生过几次非法调用"。
   顺带收益：模型连续产生非法调用时指纹相同 → 触发打转检测，不会烧光预算。
4. **错误轮次不入历史，但审计补记**：流内出错那轮不写半截 assistant 消息（否则后续每轮都带着残缺的调用意图），
   但**审计要记下"这轮产出了 N 个调用（完整 x 个 / 残缺 y 个）"**——不写历史是"不污染上下文"，
   记审计是"信息不丢"，两者不矛盾。

### 跨层纪律：渲染注入，不硬编码

编排层不 `print`，渲染走注入的 `Renderer`（`text` / `reasoning` / `notice` 三个动作）。
终端要打字机、日志要纯文本、将来接 UI 要事件——渲染注定多变，焊死在编排层等于把展示方式写进业务逻辑。
**这条纪律和"客户端不 print"是同一条**：全链路没有任何一层知道"怎么显示"。
工程收益也是实的：测试注入 `NullRenderer`，断言不被 stdout 干扰，113 个用例 0.2 秒跑完。

## 十、明确不做（写清"什么条件下才做"）

| 项 | 结论 | 条件 |
|---|---|---|
| 断线续传 | ❌ 不做 | LLM 生成不可重放，协议上有 `Last-Event-ID` 但服务端无重放能力。真要恢复：前端暂存已生成内容，重连后先推存量 |
| 背压 | ❌ 不做 | 同步生成器 + 单客户端天然跟得上。留注释：未来接多客户端需在 SSE 写入层加队列 + 水位控制 |
| 独立 Gateway / Event Store | ❌ 不做 | 单进程 CLI 不需要；出现多应用共享 / 多租户 / 统一密钥路由计费时才拆 |
| `ProviderSwitch` 事件 | ❌ 不加 | 换厂商是工厂内部决策。暴露后上层会写出 `if provider == ...`，等于依赖实现细节 |
| MCP 工具接入 | 后续 | 外部 MCP 工具与内置工具走**同一个执行入口**（`ToolRuntime.execute`），届时不改 Loop、不改 Runtime |
| 摘要压缩器（模块4） | 接口待定 | 目录占位 |
