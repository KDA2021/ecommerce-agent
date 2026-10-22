# -*- coding: utf-8 -*-
"""P15-3 配置分组测试：``settings.llm.model`` 等新路径可用，旧扁平字段不回归。

验收口径：
1. 分组字段与旧扁平字段**逐字段同值**（同一份配置不会出现两套说法）；
2. 分组值跟随环境变量覆盖（不是写死的默认值）；
3. 旧途径全部保持：``load_settings()``、``Settings(...)`` 直接构造、
   ``dataclasses.replace``、``==`` / ``hash``；
4. 分组是只读快照，且不进构造签名 / 不参与 ``==``（避免出现两套比较来源）。
"""

from __future__ import annotations

import dataclasses
import inspect

import pytest

from app.infrastructure.settings import (
    LLMConfig,
    RedisConfig,
    ResilienceConfig,
    Settings,
    SubAgentConfig,
    load_settings,
)

#: 分组名 -> 该组下字段名，覆盖 brief 点名要求的全部访问路径。
GROUP_FIELDS = {
    "llm": ("model", "base_url", "api_key"),
    "resilience": (
        "tool_timeout_seconds",
        "failure_threshold",
        "circuit_reset_seconds",
        "llm_max_retries",
    ),
    "subagents": ("timeout", "max_retries", "lease"),
    "redis": ("url",),
}

#: 分组字段 -> 旧扁平字段（兼容映射的单一事实来源）。
FLAT_ALIASES = {
    "llm.model": "llm_model",
    "llm.base_url": "llm_base_url",
    "llm.api_key": "llm_api_key",
    "resilience.tool_timeout_seconds": "tool_timeout_seconds",
    "resilience.failure_threshold": "tool_failure_threshold",
    "resilience.circuit_reset_seconds": "tool_circuit_reset_seconds",
    "resilience.llm_max_retries": "llm_max_retries",
    "subagents.timeout": "subagent_timeout_seconds",
    "subagents.max_retries": "subagent_max_retries",
    "subagents.lease": "subagent_lease_seconds",
    "redis.url": "redis_url",
}


def _group_value(settings: Settings, dotted: str):
    """按 ``"llm.model"`` 这样的点号路径读取分组值。"""
    group, _, field = dotted.partition(".")
    return getattr(getattr(settings, group), field)


def _clear_group_env(monkeypatch) -> None:
    """清掉会影响分组取值的环境变量，保证断言只看测试显式设置的值。"""
    for name in (
        "LLM_BASE_URL",
        "LLM_API_KEY",
        "LLM_MODEL",
        "TOOL_TIMEOUT_SECONDS",
        "TOOL_FAILURE_THRESHOLD",
        "TOOL_CIRCUIT_RESET_SECONDS",
        "LLM_MAX_RETRIES",
        "SUBAGENT_TIMEOUT_SECONDS",
        "SUBAGENT_MAX_RETRIES",
        "SUBAGENT_LEASE_SECONDS",
        "REDIS_URL",
    ):
        monkeypatch.delenv(name, raising=False)


class TestGroupAccessPaths:
    """分组访问路径：新路径能读、值与旧路径一致、类型正确。"""

    def test_default_settings_expose_all_groups(self, monkeypatch):
        """不设任何相关环境变量时，四个分组都存在且给出默认值。"""
        _clear_group_env(monkeypatch)
        settings = load_settings()

        assert settings.llm == LLMConfig(
            model="deepseek-v4-flash",
            base_url="https://api.deepseek.com/v1",
            api_key="",
        )
        assert settings.resilience == ResilienceConfig(
            tool_timeout_seconds=30,
            failure_threshold=3,
            circuit_reset_seconds=60.0,
            llm_max_retries=2,
        )
        assert settings.subagents == SubAgentConfig(timeout=120.0, max_retries=2, lease=180.0)
        assert settings.redis == RedisConfig(url="")

    def test_group_values_match_legacy_flat_fields(self, monkeypatch):
        """核心契约：每个分组字段与对应旧扁平字段同值（两类路径永不分叉）。"""
        _clear_group_env(monkeypatch)
        settings = load_settings()

        for dotted, flat_name in FLAT_ALIASES.items():
            assert _group_value(settings, dotted) == getattr(settings, flat_name), dotted

    def test_required_nested_paths_are_readable(self, monkeypatch):
        """brief 点名的路径逐个可读，且不是 None。"""
        _clear_group_env(monkeypatch)
        settings = load_settings()

        for dotted in FLAT_ALIASES:
            assert _group_value(settings, dotted) is not None, dotted

    def test_groups_follow_env_overrides(self, monkeypatch):
        """分组读的是环境变量，而不是把默认值复制一份。"""
        _clear_group_env(monkeypatch)
        monkeypatch.setenv("LLM_MODEL", "deepseek-test")
        monkeypatch.setenv("LLM_BASE_URL", "https://example.invalid/v1")
        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        monkeypatch.setenv("TOOL_TIMEOUT_SECONDS", "45")
        monkeypatch.setenv("TOOL_FAILURE_THRESHOLD", "7")
        monkeypatch.setenv("TOOL_CIRCUIT_RESET_SECONDS", "12.5")
        monkeypatch.setenv("LLM_MAX_RETRIES", "5")
        monkeypatch.setenv("SUBAGENT_TIMEOUT_SECONDS", "9.5")
        monkeypatch.setenv("SUBAGENT_MAX_RETRIES", "1")
        monkeypatch.setenv("SUBAGENT_LEASE_SECONDS", "11.5")
        monkeypatch.setenv("REDIS_URL", "redis://cache:6379/3")

        settings = load_settings()

        assert settings.llm == LLMConfig(
            model="deepseek-test",
            base_url="https://example.invalid/v1",
            api_key="sk-test",
        )
        assert settings.resilience == ResilienceConfig(
            tool_timeout_seconds=45,
            failure_threshold=7,
            circuit_reset_seconds=12.5,
            llm_max_retries=5,
        )
        assert settings.subagents == SubAgentConfig(timeout=9.5, max_retries=1, lease=11.5)
        assert settings.redis.url == "redis://cache:6379/3"

    def test_group_types_are_preserved(self, monkeypatch):
        """类型转换仍在 load_settings 里完成，分组不做二次包装。"""
        _clear_group_env(monkeypatch)
        settings = load_settings()

        assert isinstance(settings.llm.model, str)
        assert isinstance(settings.resilience.tool_timeout_seconds, int)
        assert isinstance(settings.resilience.circuit_reset_seconds, float)
        assert isinstance(settings.subagents.timeout, float)
        assert isinstance(settings.subagents.max_retries, int)
        assert isinstance(settings.redis.url, str)


class TestLegacyFlatCompatibility:
    """旧字段 / 旧构造途径全部可用（P15-3 的向后兼容要求）。"""

    def test_legacy_flat_attrs_still_exist(self, monkeypatch):
        """旧 ``settings.llm_model`` 等一个不删。"""
        _clear_group_env(monkeypatch)
        settings = load_settings()

        for flat_name in FLAT_ALIASES.values():
            assert hasattr(settings, flat_name), flat_name

    def test_direct_construction_keeps_working(self, tmp_path):
        """既有测试的 ``Settings(...)`` 关键字构造方式不回归。"""
        settings = Settings(
            llm_base_url="https://example.invalid/v1",
            llm_api_key="k",
            llm_model="m",
            database_url="mysql+asyncmy://u:p@localhost/db",
            data_dir=tmp_path,
            chroma_dir=tmp_path / "chroma",
            context_trigger_tokens=1,
            context_keep_recent=2,
            semantic_cache_threshold=0.5,
            semantic_cache_bucket_limit=3,
            preference_top_k=4,
            reranker_enabled=False,
            reranker_model="rr",
        )

        # 旧路径可用
        assert settings.llm_model == "m"
        assert settings.chroma_dir == tmp_path / "chroma"
        # 新路径与旧路径同值
        assert settings.llm.model == settings.llm_model == "m"
        assert settings.llm.base_url == settings.llm_base_url
        assert settings.redis.url == settings.redis_url == ""
        assert settings.subagents.lease == settings.subagent_lease_seconds == 180.0

    def test_dataclasses_replace_propagates_to_groups(self, monkeypatch):
        """``replace()`` 改旧字段后分组同步更新（派生快照不是陈旧的缓存）。"""
        _clear_group_env(monkeypatch)
        settings = load_settings()

        replaced = dataclasses.replace(settings, llm_model="replaced", redis_url="redis://x/1")

        assert replaced.llm.model == "replaced"
        assert replaced.redis.url == "redis://x/1"
        # 原对象不受影响（frozen + 派生新快照）
        assert settings.llm.model == "deepseek-v4-flash"
        assert settings.redis.url == ""

    def test_settings_stay_hashable_and_frozen(self, monkeypatch):
        """frozen dataclass 契约：可哈希、不可赋值。"""
        _clear_group_env(monkeypatch)
        settings = load_settings()

        assert isinstance(hash(settings), int)
        with pytest.raises(dataclasses.FrozenInstanceError):
            settings.llm_model = "nope"  # type: ignore[misc]

    def test_groups_are_read_only_views(self, monkeypatch):
        """分组里的值也改不了，避免运行期被悄悄改写。"""
        _clear_group_env(monkeypatch)
        settings = load_settings()

        with pytest.raises(dataclasses.FrozenInstanceError):
            settings.llm.model = "nope"  # type: ignore[misc]
        with pytest.raises(AttributeError):
            settings.resilience = None  # type: ignore[misc]

    def test_derived_groups_do_not_change_equality(self, monkeypatch):
        """分组 compare=False：== 只看扁平字段，同一配置必相等。"""
        _clear_group_env(monkeypatch)
        first = load_settings()
        second = load_settings()

        assert first == second
        assert first.llm is not second.llm  # 是各自独立的派生快照
        assert first.llm == second.llm  # 但值相等

    def test_group_fields_not_in_constructor_signature(self):
        """派生字段 init=False：不污染构造签名（旧调用方不受影响）。"""
        parameters = inspect.signature(Settings).parameters

        for group in GROUP_FIELDS:
            assert group not in parameters
        assert "llm_model" in parameters
        assert "redis_url" in parameters
        assert "subagent_timeout_seconds" in parameters

    def test_dataclass_fields_keep_expected_names(self):
        """dataclass 字段表里仍是旧扁平字段；派生字段只在末尾追加。"""
        names = [item.name for item in dataclasses.fields(Settings)]

        for flat_name in FLAT_ALIASES.values():
            assert flat_name in names
        # 派生字段名带下划线前缀，避免与 ``settings.redis`` 这类属性重名冲突。
        assert names[-4:] == ["_llm", "_resilience", "_subagents", "_redis"]
