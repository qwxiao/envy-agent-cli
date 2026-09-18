"""模块3 场景演示：Tool Runtime 的三条验收标准 + 六项治理能力。

三条验收标准（裁决定的）：
  ① **当场加新工具不改 Loop** ② **治理只写一遍** ③ **审计串起完整轨迹**

六项治理能力：路径白名单脱敏 / HITL 拒绝升级 / 只读并发 / 写串行 / 超时熔断 / 未注册提示。

全部离线可跑（不需要 API Key）：用临时工作区 + 临时工具，不碰真实项目文件。

用法：
    uv run python scripts/demo_m3_runtime.py            # 跑全部
    uv run python scripts/demo_m3_runtime.py add_tool   # 跑单个
    uv run python scripts/demo_m3_runtime.py --list
"""

import hashlib
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from envy_agent_cli.audit.logger import AuditLogger
from envy_agent_cli.llm.retry import RetryPolicy
from envy_agent_cli.tools import registry
from envy_agent_cli.tools.builtin import register_builtin_tools
from envy_agent_cli.tools.runtime import ToolRuntime
from envy_agent_cli.tools.spec import Permission, RegisteredTool, Risk, ToolSpec
from envy_agent_cli.trace import new_trace, round_span

ROOT = Path(__file__).resolve().parents[1]
LOOP_FILE = ROOT / "src" / "envy_agent_cli" / "loop" / "react.py"


def trace():
    return round_span(new_trace(), 1)


def call(name, arguments=None, call_id="c1", seq=0):
    return {"seq": seq, "id": call_id, "name": name, "arguments": arguments or {}}


def workspace() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="m3-demo-"))
    (tmp / "a.txt").write_text("hello world", encoding="utf8")
    (tmp / "sub").mkdir()
    (tmp / "sub" / "b.txt").write_text("nested", encoding="utf8")
    return tmp


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


# ---------------------------------------------------------------- 三条验收标准

def scenario_add_tool() -> None:
    """① 当场加新工具不改 Loop：注册 10 行，立刻可用，且 Loop 文件哈希不变。"""
    ws = workspace()
    register_builtin_tools(ws)
    before = file_hash(LOOP_FILE)

    # ↓↓↓ 这就是"加一个新工具"的全部成本：声明 + 实现 + 注册，Loop 一行不动
    def count_lines(path: str = ".") -> str:
        target = ws / path if not Path(path).is_absolute() else Path(path)
        files = [p for p in target.rglob("*") if p.is_file()]
        return f"{len(files)} 个文件，共 {sum(len(p.read_text(encoding='utf8').splitlines()) for p in files)} 行"

    registry.register(RegisteredTool(
        spec=ToolSpec(name="count_lines", description="统计目录下文件数与总行数。",
                      input_model={"type": "object", "properties": {"path": {"type": "string"}}},
                      permission=Permission(read_paths=(ws,), path_args=("path",)), risk=Risk.LOW),
        handler=count_lines, read_only=True, concurrency_safe=True))
    # ↑↑↑ 没有任何"在 Loop 里加 if/else"的动作

    runtime = ToolRuntime(workspace=ws, hitl_mode="never")
    result = runtime.execute(call("count_lines", {"path": "."}), trace())
    after = file_hash(LOOP_FILE)

    print(f"  新工具返回：{result.content.strip()}")
    print(f"  loop/react.py 哈希：{before} → {after}  {'未改动 ✅' if before == after else '被改动了 ❌'}")


def scenario_governance_once() -> None:
    """② 治理只写一遍：两个工具都没写校验代码，却都拿到了同样的结构化错误。"""
    ws = workspace()

    def tool_a(x: str) -> str:
        return f"a 收到 {x}"          # 没有参数检查

    def tool_b(x: str) -> str:
        return f"b 收到 {x}"          # 没有参数检查

    for name, handler in (("tool_a", tool_a), ("tool_b", tool_b)):
        registry.register(RegisteredTool(
            spec=ToolSpec(name=name, description=f"{name} 演示工具",
                          input_model={"type": "object"},
                          required_keys=("x",)),                    # 约束声明在这里
            handler=handler, read_only=True))

    runtime = ToolRuntime(workspace=ws, hitl_mode="never")
    for name in ("tool_a", "tool_b"):
        result = runtime.execute(call(name, {}), trace())
        print(f"  {name}（handler 里零校验代码）→ {result.content}")

    hits = [p.as_posix() for p in (ROOT / "src").rglob("*.py")
            if "缺少必填参数" in p.read_text(encoding="utf8")]
    print(f"  「缺少必填参数」这句话在源码里出现在：{hits}")
    print(f"  → 校验逻辑只写了一遍 {'✅' if len(hits) == 1 else '❌'}")

    import ast
    builtin_tree = ast.parse((ROOT / "src/envy_agent_cli/tools/builtin.py").read_text(encoding="utf8"))
    guards = [n for n in ast.walk(builtin_tree) if isinstance(n, ast.Try)]
    print(f"  工具实现里的 try/except 数量：{len(guards)} "
          f"{'✅（失败直接抛，分类交给 Runtime）' if not guards else '❌（工具里又兜了一层）'}")


def scenario_audit_trail() -> None:
    """③ 审计串起完整轨迹：一次任务里成功/越界/未注册三种调用都能按 trace 捞出来。"""
    ws = workspace()
    register_builtin_tools(ws)
    log_path = Path("audit") / "demo_m3.jsonl"
    logger = AuditLogger(log_path)
    runtime = ToolRuntime(workspace=ws, hitl_mode="never", audit=logger)
    ctx = trace()

    calls = [
        call("read_file", {"path": "a.txt"}, call_id="c1", seq=0),
        call("read_file", {"path": str(ws.parent / "secret.txt")}, call_id="c2", seq=1),
        call("不存在的工具", {}, call_id="c3", seq=2),
    ]
    runtime.execute_all(calls, ctx)

    print(f"  审计文件：{log_path.as_posix()}")
    print("  （按 seq 还原真实调用次序——只读工具是并发执行的，落盘顺序不等于调用顺序）")
    for record in sorted(logger.trace_lines(ctx.trace_id), key=lambda r: r.seq or 0):
        print(f"    seq={record.seq} tool={record.tool:<12} status={record.status:<6} "
              f"executed={str(record.executed):<6} code={record.error_code} retryable={record.retryable}")


# ---------------------------------------------------------------- 六项治理能力

def scenario_path_escape() -> None:
    """路径白名单 + 脱敏：越界拒绝，且**只给文件名**不给绝对路径。"""
    ws = workspace()
    register_builtin_tools(ws)
    runtime = ToolRuntime(workspace=ws, hitl_mode="never")

    inside = runtime.execute(call("read_file", {"path": "sub/b.txt"}), trace())
    outside = runtime.execute(call("read_file", {"path": str(ws.parent / "secret.txt")}), trace())

    print(f"  工作区内：{inside.content.splitlines()[1][:40]}（executed={inside.executed}）")
    print(f"  工作区外：{outside.content}（executed={outside.executed}）")
    print(f"  泄露绝对路径了吗：{'是 ❌' if str(ws.parent) in outside.content else '否 ✅'}")


def scenario_hitl() -> None:
    """HITL：写文件要确认；连续拒绝 2 次升级为"请用户介入"。"""
    ws = workspace()
    register_builtin_tools(ws)
    runtime = ToolRuntime(workspace=ws, hitl_mode="auto", confirm=lambda tool, args: False)

    first = runtime.execute(call("write_file", {"path": "x.txt", "content": "1"}), trace())
    second = runtime.execute(call("write_file", {"path": "y.txt", "content": "2"}), trace())
    read = runtime.execute(call("read_file", {"path": "a.txt"}), trace())

    print(f"  第 1 次拒绝：{first.content}")
    print(f"  第 2 次拒绝：{second.content}")
    print(f"  文件真的没被写：{'是 ✅' if not (ws / 'x.txt').exists() else '否 ❌'}")
    print(f"  只读工具不问人：{'是 ✅' if not read.is_error else '否 ❌'}")


def scenario_concurrency() -> None:
    """只读并发 / 写串行：用耗时对比说话。"""
    ws = workspace()

    def sleepy(name, seconds, read_only):
        def handler(**kwargs):
            time.sleep(seconds)
            return f"{name} 完成"
        registry.register(RegisteredTool(
            spec=ToolSpec(name=name, description=name, input_model={"type": "object"}),
            handler=handler, read_only=read_only, concurrency_safe=read_only, timeout=5.0))

    for i in range(3):
        sleepy(f"slow_read_{i}", 0.2, True)
    for i in range(2):
        sleepy(f"slow_write_{i}", 0.2, False)

    reads = [call(f"slow_read_{i}", call_id=f"r{i}") for i in range(3)]
    writes = [call(f"slow_write_{i}", call_id=f"w{i}") for i in range(2)]

    parallel = ToolRuntime(workspace=ws, hitl_mode="never", max_concurrent_read=4)
    started = time.monotonic(); parallel.execute_all(reads, trace()); p = time.monotonic() - started

    serial = ToolRuntime(workspace=ws, hitl_mode="never", max_concurrent_read=1)
    started = time.monotonic(); serial.execute_all(reads, trace()); s = time.monotonic() - started

    started = time.monotonic(); parallel.execute_all(writes, trace()); w = time.monotonic() - started

    print(f"  3 个只读（各 0.2s）：并发 {p:.2f}s vs 串行 {s:.2f}s  → {'并发更快 ✅' if p < s else '没跑起来 ❌'}")
    print(f"  2 个写（各 0.2s）：{w:.2f}s（应当接近 0.4s，因为写必须串行）")


def scenario_breaker() -> None:
    """超时熔断：连续超时 2 次后，该工具本次会话不再真执行（防线程泄漏）。"""
    ws = workspace()
    ran = []

    def hang(**kwargs):
        ran.append(1)
        time.sleep(0.3)
        return "永远等不到"

    registry.register(RegisteredTool(
        spec=ToolSpec(name="hang_read", description="会卡住的只读工具",
                      input_model={"type": "object"}),
        handler=hang, read_only=True, concurrency_safe=True, timeout=0.05))

    runtime = ToolRuntime(workspace=ws, hitl_mode="never",
                          retry_policy=RetryPolicy(max_attempts=1, jitter_ratio=0),
                          timeout_breaker=2)
    for i in range(1, 4):
        result = runtime.execute(call("hang_read", call_id=f"c{i}"), trace())
        print(f"  第 {i} 次：{result.content}（实际执行次数 {len(ran)}）")


SCENARIOS = {
    "add_tool": scenario_add_tool,
    "governance_once": scenario_governance_once,
    "audit_trail": scenario_audit_trail,
    "path_escape": scenario_path_escape,
    "hitl": scenario_hitl,
    "concurrency": scenario_concurrency,
    "breaker": scenario_breaker,
}


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    args = sys.argv[1:]
    if args and args[0] == "--list":
        print("可用场景：\n  " + "\n  ".join(SCENARIOS))
        return 0

    for name in (args or list(SCENARIOS)):
        if name not in SCENARIOS:
            print(f"未知场景：{name}（用 --list 看全部）")
            return 2
        print(f"\n{'=' * 72}\n【{name}】")
        SCENARIOS[name]()
    return 0


if __name__ == "__main__":
    sys.exit(main())
