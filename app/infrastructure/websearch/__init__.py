# -*- coding: utf-8 -*-
"""WebSearch 基础设施包：外部搜索服务的适配器。"""
from app.infrastructure.websearch.tavily_search import TavilySearchClient, TavilySearchError

__all__ = ["TavilySearchClient", "TavilySearchError"]

