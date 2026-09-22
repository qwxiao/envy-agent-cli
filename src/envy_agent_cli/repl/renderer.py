"""REPL 渲染：Rich 实现，满足 `loop.Renderer` 契约。

**流式与 Markdown 的矛盾**（本模块唯一有难度的地方）：

流式输出是"半截语法"——`**加粗` 打到一半时，Markdown 解析器会把它渲染成
一个孤零零的星号加几个字。逐块刷 Markdown 必然错乱，而且越刷越乱。

解法是**两阶段**：

1. 流式阶段用 `Live(transient=True)` 显示**纯文本**——用户能看到字一个个出来
2. 一轮结束时 `flush()`：先 `stop()` 掉 Live（`transient` 会把这块内容从屏幕上**清掉**），
   再用 `console.print(Markdown(...))` 整块渲染一次

结果：过程是流式的，成品是渲染好的，而且**不会出现两遍内容**——
`transient=True` 是这里的关键，没有它就会先看到一坨纯文本、再看到一份渲染版。

⚠️ `flush()` **不在 `Renderer` 契约里**：契约只有 `text` / `reasoning` / `notice` 三个方法
（Loop 只认这三个，它不知道"一轮结束了"这回事——那是编排层的事实）。
把 `flush()` 留在实现类上，是对契约的尊重，不是遗漏。
"""

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.text import Text


class RichRenderer:
    """把事件流渲染成终端上好看的样子。

    Args:
        show_reasoning: 是否展示思维链。**默认关**——推理是过程证据不是产出，
            默认显示会把正文挤下去；要看时一个开关的事。
    """

    def __init__(self, console: Console | None = None, *, show_reasoning: bool = False) -> None:
        self.console = console if console is not None else Console()
        self.show_reasoning = show_reasoning
        self._text = ""
        self._reasoning = ""
        self._live: Live | None = None

    # ---------- Renderer 契约 ----------

    def text(self, chunk: str) -> None:
        self._text += chunk
        self._refresh()

    def reasoning(self, chunk: str) -> None:
        if not self.show_reasoning:
            return
        self._reasoning += chunk
        self._refresh()

    def notice(self, message: str) -> None:
        """提示是"事件"不是"内容"——先把 Live 收掉，免得两处输出打架。"""
        self._stop_live()
        self.console.print(f"[dim]{message}[/dim]")

    # ---------- 非契约：一轮结束 ----------

    def flush(self) -> None:
        """撤掉流式预览，把正文整块渲染成 Markdown。

        幂等：没有内容时什么都不做（空回答不该在屏幕上留个空行）。
        """
        self._stop_live()
        if self._text.strip():
            self.console.print(Markdown(self._text))
        self._text = ""
        self._reasoning = ""

    def print_markdown(self, text: str) -> None:
        """直接渲染一段 Markdown（非流式内容，如命令输出）。"""
        self._stop_live()
        self.console.print(Markdown(text))

    def print_plain(self, text: str, style: str = "") -> None:
        self._stop_live()
        self.console.print(text, style=style or None)

    # ---------- 内部 ----------

    def _refresh(self) -> None:
        if self._live is None:
            # auto_refresh=False：手动控制刷新时机。
            # 让 Live 自己起刷新线程的话，同步代码里会出现"刷新时刚好在改 buffer"的竞态。
            self._live = Live(console=self.console, transient=True, auto_refresh=False)
            self._live.start()
        self._live.update(self._preview())
        self._live.refresh()

    def _preview(self) -> Text:
        """流式预览的内容：思维链（暗色）在上，正文在下。"""
        if not self._reasoning:
            return Text(self._text)
        block = Text()
        block.append(self._reasoning, style="dim italic")
        block.append("\n")
        block.append(self._text)
        return block

    def _stop_live(self) -> None:
        if self._live is not None:
            self._live.stop()      # transient=True → 这块内容从屏幕上消失
            self._live = None
