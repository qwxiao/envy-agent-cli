"""配置读取测试：解析规则 + 优先级 + 多厂商凭证 + 不泄漏。"""

import pytest

from envy_agent_cli.config import (
    Settings,
    collect_api_keys,
    describe_config,
    load_env_file,
    load_settings,
    parse_env_text,
)

ALL_KEYS = ("API_KEY", "PROVIDER", "MODEL", "BASE_URL",
            "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "GLM_API_KEY")


@pytest.fixture
def clean_env(monkeypatch):
    """清掉可能干扰的配置项，让每个用例从干净环境开始。"""
    for key in ALL_KEYS:
        monkeypatch.delenv(key, raising=False)


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


def test_load_settings_requires_a_key_for_active_provider(tmp_path, clean_env):
    with pytest.raises(ValueError, match="GLM_API_KEY"):
        load_settings(tmp_path / "不存在", provider="glm")


def test_collect_api_keys_finds_every_provider(clean_env, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds")
    monkeypatch.setenv("GLM_API_KEY", "glm")
    monkeypatch.setenv("KIMI_API_KEY", "kimi")

    assert collect_api_keys() == {"deepseek": "ds", "glm": "glm", "kimi": "kimi"}


def test_generic_api_key_falls_back_to_default_provider(clean_env, monkeypatch):
    monkeypatch.setenv("API_KEY", "generic")
    assert collect_api_keys() == {"deepseek": "generic"}


def test_specific_key_wins_over_generic(clean_env, monkeypatch):
    monkeypatch.setenv("API_KEY", "generic")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "specific")

    assert collect_api_keys()["deepseek"] == "specific"


def test_load_settings_can_switch_provider(tmp_path, clean_env, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds")
    monkeypatch.setenv("GLM_API_KEY", "glm")

    settings = load_settings(tmp_path / "不存在", provider="glm")

    assert settings.provider == "glm"
    assert settings.api_key == "glm"
    assert settings.api_key_for("deepseek") == "ds"     # 多厂商时能各取各的


def test_per_provider_overrides_beat_global(tmp_path, clean_env, monkeypatch):
    monkeypatch.setenv("GLM_API_KEY", "glm")
    monkeypatch.setenv("MODEL", "全局模型")
    monkeypatch.setenv("GLM_MODEL", "厂商模型")

    assert load_settings(tmp_path / "不存在", provider="glm").model == "厂商模型"

    monkeypatch.delenv("GLM_MODEL")
    assert load_settings(tmp_path / "不存在", provider="glm").model == "全局模型"


def test_settings_repr_never_leaks_keys():
    settings = Settings(provider="deepseek", api_keys={"deepseek": "sk-secret"})
    text = repr(settings)
    assert "sk-secret" not in text
    assert "deepseek" in text          # 厂商名可以出现，凭证不行


def test_describe_config_lists_names_only(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("GLM_API_KEY=glm-secret\n无关键=1\n", encoding="utf8")

    info = describe_config(env_file)

    assert "GLM_API_KEY" in info["from_file"]
    assert all("glm-secret" not in item for item in info["from_file"])
