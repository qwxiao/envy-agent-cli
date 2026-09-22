"""REPL 渲染：Rich 实现，满足 `loop.Renderer` 契约。

**流式正文直接往终端写，不用 `Live`。**

这里踩过一个坑，值得写下来：最初用 `Live(transient=True)` 做流式预览——
它的机制是"光标回到起点 → 清除 → 重画"。**但内容一旦超过一屏高度，
光标就没法再往上移动，重画退化成不断向下追加**：同一句话会在屏幕上出现几百遍。
中文折行让宽度计算更不准，雪上加霜。短回答看不出来，长回答直接雪崩。

结论：**`Live` 适合"小范围原地刷新"（进度条、状态栏），不适合"不断增长的长文本"。**
流式正文只能用最朴素的办法——写出去就不再动它。

代价说明白：**流式正文不做 Markdown 渲染**。想看渲染效果，内容得先完整、
再一次性交给解析器；而"边流边渲染"必然遇到半截语法（`**加粗` 打到一半时，
解析器会渲染成一个孤零零的星号）。两者不可兼得，这里选可靠的那个。

非流式的内容（命令输出、帮助文本）**仍然走 Markdown 渲染**——那些是完整文本，
不存在半截问题。

⚠️ `flush()` **不在 `Renderer` 契约里**：契约只有 `text` / `reasoning` / `notice`
（Loop 只认这三个，它不知道"一轮结束了"这回事——那是编排层的事实）。
"""

from rich.console import Console
from rich.markdown import Markdown


class RichRenderer:
    """把事件流写到终端。

    Args:
        show_reasoning: 是否展示思维链。**默认关**——推理是过程证据不是产出，
            默认显示会把正文挤下去。
    """

    def __init__(self, console: Console | None = None, *, show_reasoning: bool = False) -> None:
        self.console = console if console is not None else Console()
        self.show_reasoning = show_reasoning
        self._text = ""
        self._reasoning = ""
        self._mid_line = False        # 当前光标是否停在半行上
        self._in_reasoning = False

    # ---------- Renderer 契约 ----------

    def text(self, chunk: str) -> None:
        self._text += chunk
        if self._in_reasoning:
            # 从思维链切回正文：断开，免得两段连成一句
            self._end_line()
            self._in_reasoning = False
        self._write(chunk)

    def reasoning(self, chunk: str) -> None:
        if not self.show_reasoning:
            return
        self._reasoning += chunk
        self._in_reasoning = True
        self._write(chunk, style="dim italic")

    def notice(self, message: str) -> None:
        """提示是"事件"不是"内容"——先收尾半行，免得跟正文粘在一起。"""
        self._end_line()
        self.console.print(f"[dim]{message}[/dim]")
        self._in_reasoning = False

    # ---------- 非契约：一轮结束 ----------

    def flush(self) -> None:
        """一轮结束：只收个尾。

        **不重绘**——正文已经一个字一个字写在屏幕上了，重绘等于打印第二遍
        （这正是当初用 `Live` 时发生的事）。`transient` 那种"先显示再擦掉换成
        渲染版"的把戏，在长内容上不成立。
        """
        self._end_line()
        self._text = ""
        self._reasoning = ""
        self._in_reasoning = False

    # ---------- 非流式内容 ----------

    def print_markdown(self, text: str) -> None:
        """渲染一段**完整**的 Markdown（命令输出、帮助等）。"""
        self._end_line()
        self.console.print(Markdown(text))

    def print_plain(self, text: str, style: str = "") -> None:
        self._end_line()
        self.console.print(text, style=style or None)

    # ---------- 内部 ----------

    def _write(self, chunk: str, style: str | None = None) -> None:
        """直接写出，**不做任何重绘**。

        `markup=False`：正文里的 `[xxx]` 是内容不是标记，不能让 Rich 当标签解析。
        `highlight=False`：流式下逐片高亮既慢又会在片边界处出错。
        `soft_wrap=False`：按终端宽度正常折行，不挤压空白。
        """
        self._mid_line = True
        self.console.print(chunk, end="", style=style, markup=False,
                           highlight=False, soft_wrap=False)

    def _end_line(self) -> None:
        """若光标停在半行上，补一个换行。"""
        if self._mid_line:
            self.console.print()
            self._mid_line = False
