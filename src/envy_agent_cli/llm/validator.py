"""模块1 · 输出契约层。

**为什么单独一个文件**：
- ❌ 不塞进 Adapter：Adapter 管"把响应翻译成事件"，校验是另一件事，混在一起就是混职；
- ❌ 不共用工具层 Schema 模块：方向上不是一回事——工具层校验"**模型给的参数**"，
  这里校验"**模型给的答案**"。（底层可以共用工具函数，但语义模块必须分开。）

**职责三件**：① 定义输出契约 ② 校验 ③ 生成纠错提示文本。
**纠错重试由 Loop 驱动**——validator 自己不发起请求，保持"模块1 不决策"的边界。

三层防线，本层负责到第 3 层但**只做结构校验**：

| 层 | 本层做不做 |
|---|---|
| ① JSON mode（只保证"看起来像 JSON"） | ✅ |
| ② JSON Schema / 原生结构化输出 | ✅ |
| ③ 结构/类型护栏 | ✅ |
| ③ 业务规则（路径白名单、跨字段业务语义） | ❌ 归上层 |

判据一句话：**"这个校验需要懂业务吗？"** 需要 → 上层；不需要 → 这里。

> 骨架阶段用轻量结构校验（零依赖、可离线测）；升 Pydantic 时只改 `_check` 一处。
"""

import json
from dataclasses import dataclass, field
from typing import Any

#: JSON Schema 里我们支持校验的类型子集（够用就好，别造一个小 jsonschema）
_TYPE_MAP: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
    "null": (type(None),),
}


@dataclass(frozen=True, slots=True)
class OutputContract:
    """一份输出契约：名字 + 结构 schema（JSON Schema 子集）。"""

    name: str
    schema: dict[str, Any]
    description: str = ""


@dataclass(slots=True)
class ValidationOutcome:
    """校验结果。失败时带上**纠错提示文本**，供 Loop 回灌给模型重生成。"""

    ok: bool
    value: dict | None = None
    error: str | None = None
    correction_prompt: str = ""


def validate_output(raw_text: str, contract: OutputContract) -> ValidationOutcome:
    """校验模型的输出文本是否满足契约。

    Args:
        raw_text: 模型返回的原始文本（期望是 JSON）。
        contract: 输出契约。

    Returns:
        `ValidationOutcome`。失败时 `correction_prompt` 已生成好，Loop 直接回灌即可。
    """
    text = raw_text.strip()
    if not text:
        return _fail(contract, "输出为空")

    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        return _fail(contract, f"不是合法 JSON：{exc.msg}")

    if not isinstance(value, dict):
        return _fail(contract, f"顶层应为对象，实际是 {type(value).__name__}")

    error = _check_structure(value, contract.schema)
    if error:
        return _fail(contract, error)

    return ValidationOutcome(ok=True, value=value)


def _check_structure(value: dict, schema: dict) -> str | None:
    """结构/类型校验：必填字段、字段类型、枚举取值。**不涉及任何业务语义。**"""
    for key in schema.get("required", []):
        if key not in value:
            return f"缺少必填字段：{key}"

    properties = schema.get("properties") or {}
    for key, spec in properties.items():
        if key not in value:
            continue
        expected = spec.get("type")
        if expected in _TYPE_MAP and not isinstance(value[key], _TYPE_MAP[expected]):
            return f"字段 {key} 类型应为 {expected}，实际是 {type(value[key]).__name__}"
        if "enum" in spec and value[key] not in spec["enum"]:
            return f"字段 {key} 取值必须是 {spec['enum']} 之一，实际是 {value[key]!r}"
    return None


def _fail(contract: OutputContract, error: str) -> ValidationOutcome:
    return ValidationOutcome(ok=False, error=error,
                             correction_prompt=build_correction_prompt(contract, error))


def build_correction_prompt(contract: OutputContract, error: str) -> str:
    """把校验失败翻译成一句**给模型看**的纠错提示。

    只给"哪里不对、应该长什么样"，**不给堆栈、不复述整段原文**——
    错误信息是给模型消费的 observation，不是给人看的报错。
    """
    return (
        f"你的上一条输出不满足约定「{contract.name}」：{error}。\n"
        f"请只输出符合以下结构的 JSON，不要输出任何解释文字：\n"
        f"{json.dumps(contract.schema, ensure_ascii=False)}"
    )
