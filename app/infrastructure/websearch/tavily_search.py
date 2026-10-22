# -*- coding: utf-8 -*-
"""Tavily WebSearch 适配器：用异步 httpx 调用 Tavily Search API。

设计约束：
- 只做 HTTP 调用与结果字段映射，不在这里做重试/熔断（由上层 tool 包装）；
- 非 2xx 抛 :class:`TavilySearchError`，异常消息保留状态码，便于启动日志/工具错误排查；
- 返回统一为 ``[{title, url, snippet}]``，调用方不再关心 Tavily 原始 ``content`` 字段。
"""
from __future__ import annotations

import httpx

TAVILY_SEARCH_URL = "https://api.tavily.com/search"


class TavilySearchError(RuntimeError):
    """Tavily 返回非 2xx 时抛出，消息中带 HTTP 状态码。"""

    def __init__(self, status_code: int, detail: str) -> None:
        """构造带 HTTP 状态码的错误。

        Args:
            status_code: HTTP 状态码。
            detail: 响应正文。
        """
        super().__init__(f"Tavily search failed with status {status_code}: {detail}")
        self.status_code = status_code


class TavilySearchClient:
    """Tavily Search API 的最小异步客户端。

    Args:
        api_key: Tavily API key；空字符串表示未配置，由 bootstrap 决定不注册工具。
        client: 可选的 ``httpx.AsyncClient``，用于测试注入；为空时按 timeout 自建并关闭。
        timeout: 自建 HTTP 客户端的超时秒数。
    """

    def __init__(
        self,
        api_key: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 15.0,
    ) -> None:
        """记录 api_key 与可注入的 httpx 客户端。

        Args:
            api_key: Tavily API key。
            client: 可选 httpx.AsyncClient（测试注入）。
            timeout: 自建 HTTP 客户端的超时秒数。
        """
        self._api_key = api_key
        self._client = client
        self._timeout = timeout

    async def search(self, query: str, max_results: int = 3) -> list[dict]:
        """检索公开网页并返回 ``[{title, url, snippet}]``。

        Args:
            query: 搜索词。
            max_results: 返回结果条数，默认 3。

        Raises:
            TavilySearchError: Tavily 返回非 2xx 时，消息中包含 HTTP 状态码。
        """
        payload = {
            "api_key": self._api_key,
            "query": query,
            "max_results": max_results,
        }
        if self._client is not None:
            response = await self._client.post(TAVILY_SEARCH_URL, json=payload)
        else:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(TAVILY_SEARCH_URL, json=payload)

        if not 200 <= response.status_code < 300:
            raise TavilySearchError(
                response.status_code,
                getattr(response, "text", ""),
            )

        data = response.json()
        results = data.get("results") if isinstance(data, dict) else None
        return [
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("snippet") or item.get("content", ""),
            }
            for item in (results or [])
            if isinstance(item, dict)
        ]
