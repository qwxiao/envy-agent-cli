"""配置读取测试：解析规则 + 优先级 + 不泄漏凭证。"""

import pytest

from envy_agent_cli.config import Settings, describe_keys, load_env_file, load_settings, parse_env_text


def test_parse_env_text_handles_comments_quotes_and_export():
    text = "\n".join([
        "# 注释行",
        "",
        "API_KEY=sk-abc",
        'MODEL="deepseek-chat"',
        "export BASE_URL='https://api.deepseek.com'",
        "没有等号的行",
    ])
    assert parse_env_text(text) == {
        "API_KEY": "sk-abc",
        "MODEL": "deepseek-chat",
        "BASE_URL": "https://api.deepseek.com",
    }


def test_load_env_file_does_not_override_existing_env(tmp_path, monkeypatch):
    """环境变量是权威来源，`.env` 只是本地兜底——不能反过来压掉进程环境。"""
    monkeypatch.setenv("API_KEY", "from-env")
    env_file = tmp_path / ".env"
    env_file.write_text("API_KEY=from-file\nMODEL=m1\n", encoding="utf8")

    load_env_file(env_file)

    import os
    assert os.environ["API_KEY"] == "from-env"
    assert os.environ["MODEL"] == "m1"


def test_load_env_file_missing_is_not_an_error(tmp_path):
    assert load_env_file(tmp_path / "不存在") == {}


def test_load_settings_requires_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("API_KEY", raising=False)
    with pytest.raises(ValueError, match="API_KEY"):
        load_settings(tmp_path / "不存在")


def test_load_settings_reads_all_fields(tmp_path, monkeypatch):
    for key in ("API_KEY", "PROVIDER", "MODEL", "BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("API_KEY=k\nPROVIDER=openai\nMODEL=gpt-4o-mini\n"
                        "BASE_URL=https://x/v1\n", encoding="utf8")

    settings = load_settings(env_file)

    assert settings == Settings(api_key="k", provider="openai",
                                model="gpt-4o-mini", base_url="https://x/v1")


def test_settings_repr_masks_api_key():
    assert "sk-secret" not in repr(Settings(api_key="sk-secret"))


def test_describe_keys_lists_names_only(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("API_KEY=sk-secret\n无关键=1\n", encoding="utf8")

    keys = describe_keys(env_file)

    assert keys == ["API_KEY"]
    assert all("sk-secret" not in k for k in keys)
