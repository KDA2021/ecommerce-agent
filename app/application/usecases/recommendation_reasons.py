# -*- coding: utf-8 -*-
"""推荐理由（可解释性）纯函数

商品卡的 ``reasons`` 只允许来自**已经发生的事实**：

    budget_match     预算命中：商品到手价 ≤ 用户显式给定的价格上限
    ship_to_match    国家可发：用户显式给定收货国，且商品确实发往该国
    preference_match 偏好记忆命中：买家长期偏好语句里的关键词**全部**出现在商品文本中

设计红线（与 P10-3 需求对齐）：**不允许编造理由**。

    没有 ``price_max_major`` 就没有预算理由；没有 ``ship_to`` 就没有可发理由；
    没有偏好记忆（或偏好词在商品文本里对不上）就没有偏好理由。

偏好命中的判定刻意选"全词覆盖"而不是"沾边即中"：只有偏好里的每个关键词都能在
商品的可检索文本里找到，才写"命中偏好"。宁可少说一条理由，也不给用户一个
看起来像事实、实际上只是字面重叠的结论。

本模块不依赖任何基础设施，便于单测与复用（usecase / 前端契约共用同一套 code）。
"""
from __future__ import annotations

import re
from typing import Iterable, Sequence

# ---------------------------------------------------------------- reason codes

REASON_BUDGET_MATCH = "budget_match"
REASON_SHIP_TO_MATCH = "ship_to_match"
REASON_PREFERENCE_MATCH = "preference_match"

REASON_LABELS: dict[str, str] = {
    REASON_BUDGET_MATCH: "预算命中",
    REASON_SHIP_TO_MATCH: "可发往收货国",
    REASON_PREFERENCE_MATCH: "命中你的偏好",
}

# 关键词形态：CJK 连续段 / 拉丁字母数字串（如 USB、100W）
_KEYWORD_RE = re.compile(r"[\u4e00-\u9fff]+|[0-9A-Za-z]+")

# 前缀语气词：整块以它们开头时先剥掉再抽关键词。
# "喜欢小众设计" 的核心是 "小众设计"，"不要塑料材质" 的核心是 "塑料材质"——
# 语气词留在关键词里只会制造 "要塑 / 欢小" 这类无意义词对。
_SENTIMENT_TOKENS = (
    "不想要", "不喜欢", "喜欢", "想要", "不要", "需要", "偏爱", "偏好", "讨厌", "介意",
)

# 单字停用词：孤立的这些字没有商品描述力，命中也不构成"偏好命中"。
_STOPWORDS = frozenset("我你他她它的了别和与或很都就也还这那好我会个件些点先再更吧呢啊")


def normalize_text(text: str) -> str:
    """归一化文本：小写 + 去掉空白与常见分隔符，便于跨字段子串比较。

    例：``"PC 聚碳酸酯硬壳"`` → ``"pc聚碳酸酯硬壳"``。

    Args:
        text: 原始文本。

    Returns:
        归一化后的文本。
    """
    return re.sub(r"[\s\-_/·、,，。.（）()]+", "", (text or "").lower())


def _chunk_candidates(chunk: str) -> list[str]:
    """把一个词块展开成候选关键词。

    - CJK / 拉丁块都先剥掉前导语气词（"喜欢/不要/想要"），只留描述性部分；
    - 剩余 CJK 段长度 1 取该字，长度 ≥2 取 2-gram；
    - 剩余拉丁块整体保留（如 ``usb`` / ``100w``）。

    Args:
        chunk: 连续的中文段或拉丁字母数字段。

    Returns:
        候选关键词列表（保持顺序，可能重复）。
    """
    if chunk.isascii():
        if chunk and chunk not in _SENTIMENT_TOKENS:
            return [chunk]
        return []
    stripped = chunk
    for token in _SENTIMENT_TOKENS:
        if stripped.startswith(token) and len(stripped) > len(token):
            stripped = stripped[len(token):]
            break
    if not stripped:
        return []
    if len(stripped) == 1:
        return [stripped]
    return [stripped[index : index + 2] for index in range(len(stripped) - 1)]


def _extract_keywords(statement: str) -> tuple[list[str], list[str]]:
    """抽取偏好关键词，返回 ``(全部关键词, 核心关键词)``。

    核心关键词 = 全部关键词里的非停用词，且不能整词都由停用字组成。核心关键词为空
    （整句都是"喜欢 / 想要"这类语气词）时命中没有判别力，调用方应放弃判定。

    Args:
        statement: 偏好原文。

    Returns:
        ``(keywords, core_keywords)``，两者都保持出现顺序且去重。
    """
    keywords: list[str] = []
    for chunk in _KEYWORD_RE.findall(statement or ""):
        for candidate in _chunk_candidates(chunk):
            if candidate not in keywords:
                keywords.append(candidate)

    core = [
        keyword
        for keyword in keywords
        if keyword not in _STOPWORDS and any(ch not in _STOPWORDS for ch in keyword)
    ]
    return keywords, core


def preference_keywords(statement: str) -> list[str]:
    """返回一句偏好里参与商品文本比对的**核心**关键词。

    Args:
        statement: 偏好原文，如 ``"喜欢小众设计"`` → ``["小众", "众设", "设计"]``。

    Returns:
        核心关键词列表；整句都是功能字时返回 ``[]``。
    """
    return _extract_keywords(statement)[1]


def preference_match_terms(statement: str) -> list[str]:
    """返回比对用的完整关键词（核心词为空时退回全部词，保证判定可解释）。

    Args:
        statement: 偏好原文。

    Returns:
        参与"全词覆盖 / 词对重合"判断的关键词列表。
    """
    keywords, core = _extract_keywords(statement)
    return core or keywords


def has_word_pair(statement: str, product_text: str) -> bool:
    """判断偏好与商品文本的词对重合是否足以支撑"命中"结论。

    "全词覆盖"只保证每个词都出现，形如偏好"要雨伞"在"雨伞"标题里的字面重叠并不
    代表口味命中。因此要求**至少一半关键词**以 2-gram（或拉丁词）出现在商品文本里。
    只沾一半的偏好（"喜欢轻量收纳" 命中 "轻量 …"）会在"全词覆盖"那步就被挡掉。

    Args:
        statement: 偏好原文。
        product_text: 商品可检索文本。

    Returns:
        词对重合达标返回 True。
    """
    keywords = preference_match_terms(statement)
    haystack = normalize_text(product_text)
    matched = [keyword for keyword in keywords if len(keyword) >= 2 and keyword in haystack]
    return len(matched) * 2 >= len(keywords)


def preference_matches(statement: str, product_text: str) -> bool:
    """判断一句偏好是否**完整**命中商品文本。

    命中需要同时满足：
        1. 抽得出关键词（整句都是"喜欢/不要"这类功能字时直接放弃）；
        2. 每个关键词都出现在商品文本里（不放过"只沾一半"的偏好）；
        3. 词对重合达标（见 :func:`has_word_pair`），避免纯字面巧合。

    Args:
        statement: 偏好原文。
        product_text: 商品可检索文本（标题 + 品牌 + 品类 + 描述 + 亮点）。

    Returns:
        命中返回 True。
    """
    keywords = preference_match_terms(statement)
    if not keywords:
        return False
    haystack = normalize_text(product_text)
    if not all(keyword in haystack for keyword in keywords):
        return False
    return has_word_pair(statement, product_text)


# ------------------------------------------------------------- reason builder

def _reason(code: str) -> dict:
    """把 reason code 渲染成对外契约 ``{'code', 'label'}``。"""
    return {"code": code, "label": REASON_LABELS[code]}


def build_reasons(
    *,
    budget_matched: bool = False,
    ship_to_matched: bool = False,
    matched_preference_statements: Sequence[str] = (),
) -> list[dict]:
    """按固定顺序组装理由列表；只输出真实命中的项。

    顺序固定为 预算 → 可发 → 偏好，保证同一商品多次检索理由顺序稳定（前端可缓存）。

    Args:
        budget_matched: 是否命中预算上限。
        ship_to_matched: 是否命中可发国家。
        matched_preference_statements: 真实命中商品文本的偏好原文（去重后的列表）。

    Returns:
        ``[{'code': ..., 'label': ...}]``；无命中时为 ``[]``。
    """
    reasons: list[dict] = []
    if budget_matched:
        reasons.append(_reason(REASON_BUDGET_MATCH))
    if ship_to_matched:
        reasons.append(_reason(REASON_SHIP_TO_MATCH))
    for statement in _dedupe(matched_preference_statements):
        reason = _reason(REASON_PREFERENCE_MATCH)
        reason["matched_preference"] = statement
        reasons.append(reason)
    return reasons


def _dedupe(statements: Iterable[str]) -> list[str]:
    """保持顺序去重（同一句偏好命中两次也只写一条理由）。"""
    seen: list[str] = []
    for statement in statements or ():
        if statement and statement not in seen:
            seen.append(statement)
    return seen


