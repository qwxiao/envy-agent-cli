"""运行配置：从环境变量读取，支持用 `.env` 文件兜底。

优先级：**已存在的环境变量 > `.env` 文件 > 默认值**。
不覆盖已存在的环境变量是刻意的——十二要素应用里环境变量是权威来源，
`.env` 只是本地开发的便利，不能反过来压掉进程环境。

**多厂商约定**：凭证按 `<厂商名>_API_KEY` 命名（如 `DEEPSEEK_API_KEY` / `GLM_API_KEY`），
`load_settings` 会自动把它们全部收进 `Settings.api_keys`——所以接新厂商时这里一行都不用改，
加个环境变量就行。可选覆盖项同理：`<厂商名>_MODEL` / `<厂商名>_BASE_URL`。

凭证只从环境进来，**不落任何日志、不进审计记录**。
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

#: 默认查找 `.env` 的位置：当前工作目录
ENV_FILENAME = ".env"

#: 未显式指定时用哪个厂商
DEFAULT_PROVIDER = "deepseek"

#: 凭证环境变量的后缀
_API_KEY_SUFFIX = "_API_KEY"


@dataclass(frozen=True, slots=True)
class Settings:
    """一次运行需要的配置。frozen：配置在读完之后不该再变。"""

    provider: str
    api_keys: dict[str, str] = field(default_factory=dict)
    model: str | None = None
    base_url: str | None = None

    @property
    def api_key(self) -> str:
        """当前厂商的凭证。"""
        return self.api_keys[self.provider]

    def api_key_for(self, provider: str) -> str:
        """取指定厂商的凭证；没单独配就退回当前厂商的（单厂商场景下两者相同）。"""
        return self.api_keys.get(provider) or self.api_keys[self.provider]

    def __repr__(self) -> str:
        # 防止 api_key 被日志/异常栈顺手打出来
        return (f"Settings(provider={self.provider!r}, model={self.model!r}, "
                f"base_url={self.base_url!r}, api_keys={sorted(self.api_keys)})")


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


def collect_api_keys() -> dict[str, str]:
    """扫描环境里所有 `<厂商名>_API_KEY`，再加上通用的 `API_KEY`。"""
    keys: dict[str, str] = {}
    for name, value in os.environ.items():
        if not name.endswith(_API_KEY_SUFFIX) or name == "API_KEY" or not value.strip():
            continue
        keys[name[: -len(_API_KEY_SUFFIX)].lower()] = value.strip()

    generic = (os.environ.get("API_KEY") or "").strip()
    if generic:
        keys.setdefault(DEFAULT_PROVIDER, generic)   # 单独配的优先，通用的兜底
    return keys


def _resolve(provider: str, suffix: str) -> str | None:
    """按 `<厂商>_<后缀> > <后缀>` 的顺序取值。"""
    value = os.environ.get(f"{provider.upper()}_{suffix}") or os.environ.get(suffix)
    return value.strip() if value and value.strip() else None


def load_settings(path: str | Path = ENV_FILENAME, provider: str | None = None) -> Settings:
    """装配配置。

    Args:
        path: `.env` 文件位置。
        provider: 指定当前厂商（优先于 `PROVIDER` 环境变量）。

    Raises:
        ValueError: 当前厂商没有可用凭证 —— fail loud，不要带着空凭证去发请求。
    """
    load_env_file(path)

    active = (provider or os.environ.get("PROVIDER") or DEFAULT_PROVIDER).strip().lower()
    api_keys = collect_api_keys()
    if active not in api_keys:
        known = "、".join(sorted(api_keys)) or "（一个都没有）"
        raise ValueError(
            f"厂商 {active!r} 没有凭证。请在环境变量或 .env 中配置 "
            f"{active.upper()}_API_KEY；当前已配置的厂商：{known}"
            "（可参考仓库里的 .env.example）。"
        )

    return Settings(
        provider=active,
        api_keys=api_keys,
        model=_resolve(active, "MODEL"),
        base_url=_resolve(active, "BASE_URL"),
    )


def describe_config(path: str | Path = ENV_FILENAME) -> dict[str, list[str]]:
    """汇总配置里出现了什么（**只列键名，不返回值**），便于排查。"""
    file = Path(path)
    from_file = sorted(parse_env_text(file.read_text(encoding="utf8"))) if file.is_file() else []
    return {"from_file": from_file, "providers_with_key": sorted(collect_api_keys())}
