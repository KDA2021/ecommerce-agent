# -*- coding: utf-8 -*-


def test_settings_have_p1_fields(monkeypatch, tmp_path):
    from app.infrastructure.settings import load_settings

    monkeypatch.setenv("CHROMA_DIR", str(tmp_path / "chroma"))
    monkeypatch.setenv("CONTEXT_TRIGGER_TOKENS", "1234")
    settings = load_settings()
    assert settings.chroma_dir == tmp_path / "chroma"
    assert settings.context_trigger_tokens == 1234
    assert settings.context_keep_recent == 8
    assert settings.semantic_cache_threshold == 0.05
    assert settings.semantic_cache_bucket_limit == 30


def test_settings_load_tavily_api_key(monkeypatch):
    from app.infrastructure.settings import load_settings

    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test-key")
    assert load_settings().tavily_api_key == "tvly-test-key"


def test_tool_timeout_defaults_to_30(monkeypatch):
    """默认 30s：容纳冷 CPU 精排，同时保留有意义的熔断。"""
    from app.infrastructure.settings import load_settings

    monkeypatch.delenv("TOOL_TIMEOUT_SECONDS", raising=False)
    assert load_settings().tool_timeout_seconds == 30


def test_tool_timeout_env_override(monkeypatch):
    from app.infrastructure.settings import load_settings

    monkeypatch.setenv("TOOL_TIMEOUT_SECONDS", "45")
    assert load_settings().tool_timeout_seconds == 45


def test_order_confirm_timeout_defaults_to_120(monkeypatch):
    """P10-2 下单确认握手默认 120s；超时按取消处理、不落库。"""
    from app.infrastructure.settings import load_settings

    monkeypatch.delenv("ORDER_CONFIRM_TIMEOUT_SECONDS", raising=False)
    assert load_settings().order_confirm_timeout_seconds == 120.0


def test_order_confirm_timeout_env_override(monkeypatch):
    from app.infrastructure.settings import load_settings

    monkeypatch.setenv("ORDER_CONFIRM_TIMEOUT_SECONDS", "8.5")
    assert load_settings().order_confirm_timeout_seconds == 8.5

