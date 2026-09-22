"""模块5 · MCP 传输层：同一条 JSON-RPC 消息的两种走法。

传输层只解决一件事：**把一条 JSON-RPC 消息送出去，把应答收回来**。
它不认 method 名、不认 params 结构——那是 `client.py` 的事。

两种实现对上层是**同一个 Protocol**（`McpTransport`）：

- `StdioTransport`：起一个子进程，走 stdin/stdout，换行分隔的 JSON
- `StreamableHttpTransport`：POST 到远端，应答可能是 JSON 也可能是 SSE 流

**复用模块1 的 SSE 分帧**：MCP 的 Streamable HTTP 与 LLM 的流式响应是同一个协议，
`iter_sse_json` 一行不改就能用。这是"层按能力切"而非"按功能切"的收益——
当初把 SSE 分帧独立成 `llm/transport.py`，今天第二个消费者就来了。

**为什么 stdio 必须开线程读**：管道上的 `readline()` 会永久阻塞，
一个卡住的 server 能把整个 CLI 挂死。Windows 的 `select` 不支持管道，
所以只能"后台线程读 → 队列 → 主线程带超时取"。这是跨平台唯一可靠的做法。
"""

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from collections import deque
from typing import Any, Protocol

import httpx

from envy_agent_cli.llm.transport import iter_sse_json
from envy_agent_cli.mcp.types import (
    McpError,
    McpErrorCode,
    McpServerSpec,
)

#: 保留多少行 server 的 stderr 用于诊断。多了没用，少了看不懂。
_STDERR_KEEP = 20

#: 队列哨兵：reader 线程读到进程结束时放它，好让等待方立刻知道"通道没了"。
_CLOSED = object()


class McpTransport(Protocol):
    """传输契约。上层只依赖这四个方法，不关心底下是管道还是 HTTP。"""

    server: str

    def start(self) -> None:
        """建立通道。失败抛 `McpError(CONNECT_FAILED)`。"""
        ...

    def request(self, method: str, params: dict | None = None) -> dict:
        """发一条请求并等应答，返回 `result`。"""
        ...

    def notify(self, method: str, params: dict | None = None) -> None:
        """发一条通知，不等应答。"""
        ...

    def close(self) -> None:
        """关闭通道。可重复调用。"""
        ...

    @property
    def closed(self) -> bool:
        ...


def resolve_command(command: str) -> str:
    """把命令名解析成可执行路径。

    ⚠️ Windows 上 `npx` / `uvx` 实际是 `npx.cmd` / `uvx.exe`——
    直接 `Popen(["npx"])` 会 FileNotFoundError。`shutil.which` 会走 PATHEXT 规则，
    解析不出来就原样返回（交给 Popen 去报它自己的错，错误信息更具体）。
    """
    return shutil.which(command) or command


def parse_rpc_response(message: Any, request_id: int, method: str, server: str) -> dict | None:
    """从一条 JSON-RPC 消息里取出属于 `request_id` 的结果。

    Returns:
        命中的结果字典；**不是发给我们的**消息（别的 id、服务端主动通知）返回 `None`。

    Raises:
        McpError: 命中但对方回了 `error` 对象。
    """
    if not isinstance(message, dict):
        return None
    if message.get("id") != request_id:
        return None      # 服务端通知（无 id）或串行场景下不该出现的错位应答
    error = message.get("error")
    if error:
        detail = error.get("message") if isinstance(error, dict) else str(error)
        raw_code = error.get("code") if isinstance(error, dict) else None
        raise McpError(
            McpErrorCode.PROTOCOL_ERROR,
            f"{method} 被拒绝: {detail}",
            server=server,
            rpc_code=raw_code if isinstance(raw_code, int) else None,
        )
    result = message.get("result")
    return result if isinstance(result, dict) else {}


def build_request(request_id: int, method: str, params: dict | None) -> dict:
    """按 JSON-RPC 2.0 拼一条请求。"""
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}


def build_notification(method: str, params: dict | None) -> dict:
    """按 JSON-RPC 2.0 拼一条通知（**没有 id 字段**——有 id 就成了请求）。"""
    message: dict = {"jsonrpc": "2.0", "method": method}
    if params:
        message["params"] = params
    return message


class StdioTransport:
    """子进程 + 标准输入输出。

    请求与应答是**串行**的（一个连接同时只等一条应答），所以 reader 线程只需
    把消息原样丢进队列，匹配 id 的工作留给等待方——这样队列本身不需要任何排序逻辑。
    """

    def __init__(self, spec: McpServerSpec) -> None:
        self.spec = spec
        self.server = spec.name
        self._proc: subprocess.Popen[str] | None = None
        self._queue: queue.Queue[Any] = queue.Queue()
        self._stderr: deque[str] = deque(maxlen=_STDERR_KEEP)
        self._next_id = 0
        self._closed = True

    # ---------- 生命周期 ----------

    def start(self) -> None:
        # ⚠️ MCP 的 stdio 传输规定用 UTF-8，但 **Python 写的 server 在 Windows 上
        # 默认按系统编码（GBK）写 stdout**——一返回中文就乱码，或者直接
        # UnicodeEncodeError 把 server 打挂。客户端必须主动把这个前提补上，
        # 不能指望每个 server 作者都记得处理。
        # 这两个变量只对 Python server 有效，对 Node/Go 写的无害（它们本就是 UTF-8）。
        env = {
            **os.environ,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            **self.spec.env,          # 用户配置的优先级最高
        }
        command = resolve_command(self.spec.command or "")
        try:
            self._proc = subprocess.Popen(
                [command, *self.spec.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                text=True,
                encoding="utf8",
                errors="replace",
                bufsize=1,
            )
        except OSError as exc:
            raise McpError(
                McpErrorCode.CONNECT_FAILED,
                f"启动失败（{command}）: {exc}",
                server=self.server,
            ) from exc

        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        self._closed = False

    def close(self) -> None:
        self._closed = True
        proc, self._proc = self._proc, None
        if proc is None:
            return
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass

    @property
    def closed(self) -> bool:
        return self._closed

    # ---------- 收发 ----------

    def request(self, method: str, params: dict | None = None) -> dict:
        self._ensure_open(method)
        self._next_id += 1
        request_id = self._next_id
        self._write(build_request(request_id, method, params))
        return self._await(request_id, method)

    def notify(self, method: str, params: dict | None = None) -> None:
        self._ensure_open(method)
        self._write(build_notification(method, params))

    # ---------- 内部 ----------

    def _ensure_open(self, method: str) -> None:
        if self._closed or self._proc is None or self._proc.stdin is None:
            raise McpError(McpErrorCode.TRANSPORT_CLOSED, f"通道已关闭，无法发送 {method}", server=self.server)
        if self._proc.poll() is not None:
            raise McpError(
                McpErrorCode.TRANSPORT_CLOSED,
                f"子进程已退出（code={self._proc.returncode}）{self._stderr_hint()}",
                server=self.server,
            )

    def _write(self, message: dict) -> None:
        """写一行。**必须 flush**——管道是块缓冲的，不 flush 对面永远收不到。"""
        assert self._proc is not None and self._proc.stdin is not None
        try:
            self._proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise McpError(
                McpErrorCode.TRANSPORT_CLOSED,
                f"写入失败: {exc}{self._stderr_hint()}",
                server=self.server,
            ) from exc

    def _await(self, request_id: int, method: str) -> dict:
        deadline = time.monotonic() + self.spec.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise McpError(
                    McpErrorCode.TIMEOUT,
                    f"{method} 超时（>{self.spec.timeout}s）",
                    server=self.server,
                )
            try:
                message = self._queue.get(timeout=remaining)
            except queue.Empty:
                raise McpError(
                    McpErrorCode.TIMEOUT,
                    f"{method} 超时（>{self.spec.timeout}s）",
                    server=self.server,
                ) from None

            if message is _CLOSED:
                raise McpError(
                    McpErrorCode.TRANSPORT_CLOSED,
                    f"{method} 等待应答时进程结束了{self._stderr_hint()}",
                    server=self.server,
                )

            result = parse_rpc_response(message, request_id, method, self.server)
            if result is not None:
                return result

    def _read_stdout(self) -> None:
        """后台读 stdout：一行一条 JSON-RPC 消息。

        非 JSON 的行**直接丢掉**——server 往 stdout 里混日志是常见现象，
        为它崩掉整条通道不值得。真要看诊断，去看 `_stderr`。
        """
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    self._queue.put(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except (OSError, ValueError):
            pass
        finally:
            self._queue.put(_CLOSED)

    def _read_stderr(self) -> None:
        """后台收 stderr 的最后若干行，供失败时附在错误信息里。

        不直接 DEVNULL 的原因：MCP server 起不来时，**原因几乎总在 stderr 里**
        （Python 导入失败、端口占用、npx 包不存在）。丢掉它等于让人对着
        "连不上"三个字猜。收着但不往外吐，只在出错时给一次。
        """
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for line in proc.stderr:
                text = line.strip()
                if text:
                    self._stderr.append(text)
        except (OSError, ValueError):
            pass

    def _stderr_hint(self) -> str:
        """把收着的 stderr 拼成一小段提示。没有就不加。"""
        if not self._stderr:
            return ""
        tail = " | ".join(list(self._stderr)[-3:])
        return f"；server stderr: {tail}"


class StreamableHttpTransport:
    """POST + 可选 SSE 应答。

    与 stdio 的关键差异：**连接是懒建立的**——`start()` 不做任何网络动作，
    第一次 `request()` 才发出去。因为 HTTP 场景下"服务在不在"只有发了才知道，
    提前握手反而多一次失败点。

    `Mcp-Session-Id` 由服务端在 initialize 的应答头里给出，之后每个请求都要带回去。
    """

    def __init__(self, spec: McpServerSpec) -> None:
        self.spec = spec
        self.server = spec.name
        self._client: httpx.Client | None = None
        self._session_id: str | None = None
        self._next_id = 0
        self._closed = True

    def start(self) -> None:
        self._client = httpx.Client(timeout=self.spec.timeout)
        self._closed = False

    def close(self) -> None:
        self._closed = True
        client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except OSError:
                pass

    @property
    def closed(self) -> bool:
        return self._closed

    def request(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        request_id = self._next_id
        message = build_request(request_id, method, params)
        for payload in self._post(message, method):
            result = parse_rpc_response(payload, request_id, method, self.server)
            if result is not None:
                return result
        raise McpError(
            McpErrorCode.PROTOCOL_ERROR,
            f"{method} 的应答里没有匹配 id={request_id} 的结果",
            server=self.server,
        )

    def notify(self, method: str, params: dict | None = None) -> None:
        # 通知同样要 POST，但不需要读应答体——服务端通常回 202
        try:
            self._post(build_notification(method, params), method, read_body=False)
        except McpError:
            # 通知失败不该拖垮调用方：MCP 规范里通知本就是"发了就算"
            pass

    # ---------- 内部 ----------

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            # 两个都要声明：服务端可以选回 JSON 也可以选回 SSE 流
            "Accept": "application/json, text/event-stream",
            **self.spec.headers,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    def _post(self, message: dict, method: str, *, read_body: bool = True):
        """发一条消息，按响应类型产出 JSON-RPC 载荷。"""
        if self._closed or self._client is None:
            raise McpError(McpErrorCode.TRANSPORT_CLOSED, f"通道已关闭，无法发送 {method}", server=self.server)
        url = self.spec.url or ""
        try:
            with self._client.stream("POST", url, json=message, headers=self._headers()) as response:
                session_id = response.headers.get("mcp-session-id")
                if session_id:
                    self._session_id = session_id

                if response.status_code >= 400:
                    detail = response.read().decode(errors="replace")[:300]
                    code = McpErrorCode.CONNECT_FAILED if response.status_code in (401, 403, 404) else McpErrorCode.PROTOCOL_ERROR
                    raise McpError(
                        code,
                        f"HTTP {response.status_code}: {detail}",
                        server=self.server,
                    )

                if not read_body:
                    return

                content_type = response.headers.get("content-type", "")
                if "text/event-stream" in content_type:
                    # 复用模块1 的分帧：SSE 就是 SSE，跟是不是大模型无关
                    yield from iter_sse_json(response.iter_text())
                    return

                response.read()
                body = response.text.strip()
                if body:
                    try:
                        yield json.loads(body)
                    except json.JSONDecodeError as exc:
                        raise McpError(
                            McpErrorCode.PROTOCOL_ERROR,
                            f"{method} 的应答不是合法 JSON: {body[:200]}",
                            server=self.server,
                        ) from exc
        except httpx.TimeoutException as exc:
            raise McpError(McpErrorCode.TIMEOUT, f"{method} 超时（>{self.spec.timeout}s）", server=self.server) from exc
        except httpx.RequestError as exc:
            raise McpError(McpErrorCode.CONNECT_FAILED, f"请求失败: {exc}", server=self.server) from exc


def build_transport(spec: McpServerSpec) -> McpTransport:
    """按声明挑一种传输实现。**未启动**——调用方负责 `start()`。"""
    if spec.is_stdio:
        return StdioTransport(spec)
    if spec.is_http:
        return StreamableHttpTransport(spec)
    raise McpError(
        McpErrorCode.CONNECT_FAILED,
        f"无法识别的传输类型: {spec.transport!r}",
        server=spec.name,
    )
