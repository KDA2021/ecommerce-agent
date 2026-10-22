# -*- coding: utf-8 -*-
"""LangChain ChatOpenAI 封装：网关重试 + 绑定工具/结构化输出时保留重试。"""
from __future__ import annotations

from typing import Any

from langchain_core.runnables.retry import RunnableRetry
from langchain_openai import ChatOpenAI

from app.infrastructure.settings import Settings


def build_chat_model(settings: Settings) -> Any:
    """构造 ChatOpenAI 并套指数退避重试。

    Args:
        settings: 运行时配置。

    Returns:
        带重试的 Runnable。
    """
    if not settings.llm_api_key:
        raise RuntimeError("未配置 LLM_API_KEY：请复制 .env.example 为 .env 并填入密钥")
    model = ChatOpenAI(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        temperature=0.1,
        timeout=60,
        # deepseek-v4-flash 默认开启 thinking，关闭它以保证 ReAct 工具循环稳定。
        extra_body={"thinking": {"type": "disabled"}},
    )
    return _apply_retry(model, settings)


def _apply_retry(model, settings):
    """给 Runnable 加指数退避重试。

    langchain-core 0.3 用 ``wait_exponential_multiplier`` /
    ``wait_exponential_max``；1.x 改成 ``ExponentialJitterParams``，
    这里优先走旧参数，遇到 TypeError 再用等价的新参数，兼容两个大版本。
    """
    max_attempts = settings.llm_max_retries + 1
    try:
        # 旧版 langchain-core 参数。
        return model.with_retry(
            stop_after_attempt=max_attempts,
            wait_exponential_multiplier=1,
            wait_exponential_max=8,
        )
    except TypeError:
        # 新版参数（旧参数不被接受时走这里），语义等价：指数退避 + 上限 8s。
        return model.with_retry(
            stop_after_attempt=max_attempts,
            wait_exponential_jitter=True,
            exponential_jitter_params={
                "initial": 1,
                "max": 8,
                "exp_base": 2,
                "jitter": 0,
            },
        )


def bind_tools(llm: Any, tools: list) -> Any:
    """绑定工具；若 llm 已套 ``with_retry``，在底层模型上重建并保留重试。

    Args:
        llm: LLM 或 RunnableRetry。
        tools: 工具列表。

    Returns:
        绑定工具后的 Runnable；已保留重试包装。
    """
    return _rewrap_retry(llm, lambda model: model.bind_tools(tools))


def with_structured_output(llm: Any, schema: Any, **kwargs: Any) -> Any:
    """结构化输出；若 llm 已套 ``with_retry``，在底层模型上重建并保留重试。

    Args:
        llm: LLM 或 RunnableRetry。
        schema: Pydantic 输出模型。

    Returns:
        结构化输出 Runnable；已保留重试包装。
    """
    return _rewrap_retry(llm, lambda model: model.with_structured_output(schema, **kwargs))


def _rewrap_retry(llm: Any, build):
    """在 RunnableRetry 的底层模型上重建并保留重试包装。

    Args:
        llm: 可能是 RunnableRetry 的 LLM。
        build: 接收底层模型、返回新 Runnable 的构造函数。

    Returns:
        重建后的 Runnable。
    """
    if not isinstance(llm, RunnableRetry):
        return build(llm)
    # 先解包到底层模型再重建，避免 bind_tools / with_structured_output 丢掉重试包装。
    rebound = build(llm.bound)
    return rebound.with_retry(
        retry_if_exception_type=getattr(llm, "retry_exception_types", (Exception,)),
        wait_exponential_jitter=getattr(llm, "wait_exponential_jitter", True),
        exponential_jitter_params=getattr(llm, "exponential_jitter_params", None) or None,
        stop_after_attempt=getattr(llm, "max_attempt_number", 3),
    )
