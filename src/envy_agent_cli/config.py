"""运行配置：从环境变量读取，支持用 `.env` 文件兜底。

优先级：**已存在的环境变量 > `.env` 文件 > 默认值**。
不覆盖已存在的环境变量是刻意的——十二要素应用里环境变量是权威来源，
`.env` 只是本地开发的便利，不能反过来压掉进程环境。

凭证只从环境进来，**不落任何日志、不进审计记录**。
"""

import os
from dataclasses import dataclass
from pathlib import Path

#: 默认查找 `.env` 的位置：当前工作目录
ENV_FILENAME = ".env"

#: 支持的键 → 对应字段
_KEYS = ("API_KEY", "BASE_URL", "MODEL", "PROVIDER")


@dataclass(frozen=True, slots=True)
class Settings:
    """一次运行需要的配置。frozen：配置在读完之后不该再变。"""

    api_key: str
    provider: str = "deepseek"
    model: str | None = None
    base_url: str | None = None

    def __repr__(self) -> str:
        # 防止 api_key 被日志/异常栈顺手打出来
        return (f"Settings(provider={self.provider!r}, model={self.model!r}, "
                f"base_url={self.base_url!r}, api_key=***)")


def parse_env_text(text: str) -> dict[str, str]:
    """解析 `.env` 文本：`KEY=value`，忽略空行与 `#` 注释，支持引号包裹。"""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def load_env_file(path: str | Path = ENV_FILENAME) -> dict[str, str]:
    """读取 `.env`（不存在就返回空），并把值写进 `os.environ`——**不覆盖已有值**。"""
    file = Path(path)
    if not file.is_file():
        return {}
    values = parse_env_text(file.read_text(encoding="utf8"))
    for key, value in values.items():
        os.environ.setdefault(key, value)
    return values


def load_settings(path: str | Path = ENV_FILENAME) -> Settings:
    """装配配置。

    Raises:
        ValueError: 缺 `API_KEY` —— fail loud，不要带着空凭证去发请求。
    """
    load_env_file(path)
    api_key = (os.environ.get("API_KEY") or "").strip()
    if not api_key:
        raise ValueError(
            "缺少 API_KEY。请设置环境变量，或把 .env 放在工作目录下"
            "（可参考仓库里的 .env.example）。"
        )
    return Settings(
        api_key=api_key,
        provider=(os.environ.get("PROVIDER") or "deepseek").strip(),
        model=(os.environ.get("MODEL") or "").strip() or None,
        base_url=(os.environ.get("BASE_URL") or "").strip() or None,
    )


def describe_keys(path: str | Path = ENV_FILENAME) -> list[str]:
    """列出配置里出现了哪些键（**只列键名，不返回值**），便于排查。"""
    file = Path(path)
    if not file.is_file():
        return []
    return sorted(k for k in parse_env_text(file.read_text(encoding="utf8")) if k in _KEYS)
