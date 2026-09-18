"""模块1 · 采样参数对象。

**为什么用显式 dataclass 而不是裸 `**kwargs`**：
1. **可序列化、可版本化**——"参数版本跟测试集绑定"，改 Prompt 跑回归时参数得能固化下来；
2. **拼错立刻报错**——`temporature=0.2` 这种手滑不会静默生效；
3. **可审计**——审计日志里能落一份确定的参数快照。

参数口径（设计方拍板）：
- 默认 `temperature=0.2`，保证稳定可复现、回归可批；
- **一次只调一个采样维度**——`temperature` 与 `top_p` 二选一，同时设置会在文档里警告；
- 参数不是"质量旋钮"：调高温度救不了"缺上下文/缺证据/schema 不对"，
  质量问题先查 目标 → 上下文 → 模型能力 → 工具证据 → 输出约束，参数永远排在这五项后面。
"""

from dataclasses import asdict, dataclass, field


@dataclass(frozen=True, slots=True)
class ChatParams:
    """一次模型调用的采样参数。frozen：请求发出去后参数不该再被改。"""

    temperature: float = 0.2
    max_tokens: int | None = None
    top_p: float | None = None          # 与 temperature 二选一，别同时设
    stop: tuple[str, ...] | None = None
    extra: dict | None = field(default=None)  # 厂商特有参数（显式字段，不用裸 **kwargs）

    def to_body_fields(self) -> dict:
        """转成请求体里要带的字段（去掉 None）。"""
        body = {k: v for k, v in asdict(self).items() if v is not None}
        extra = body.pop("extra", None)
        if extra:
            # 厂商特有参数平铺进请求体；重复键以显式字段为准
            body = {**extra, **body}
        if "stop" in body:
            body["stop"] = list(body["stop"])
        return body
