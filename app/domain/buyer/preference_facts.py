"""Closed business vocabulary for persistent buyer preferences (framework independent).

Legacy text is recognized only by whole-statement matching. Unknown legacy rows are
quarantined at use time and retained for inspection/exact deletion.
"""
from __future__ import annotations

import re
from dataclasses import replace
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.domain.buyer.preference import BuyerPreference

CURRENCIES = frozenset({"CNY", "USD", "EUR", "GBP", "JPY", "HKD", "CAD", "AUD", "SGD"})
COUNTRIES = frozenset({"CN", "US", "GB", "JP", "DE", "FR", "CA", "AU", "SG", "HK", "TW", "KR", "NZ", "IT", "ES", "NL"})

# Add vocabulary through reviewed business changes; never accept arbitrary model text.
_VALUES = {
    "material": ("塑料", "皮革", "帆布", "棉", "不锈钢", "金属", "玻璃", "陶瓷", "竹", "尼龙", "羊毛", "硅胶", "木", "亚麻"),
    "category": ("旅行装备", "家居生活", "户外运动", "数码配件", "服饰配件", "美妆个护", "食品饮料", "母婴用品", "水杯", "背包", "行李箱", "收纳", "鞋服"),
    "brand": ("Nomadica", "TrailOx", "GlideCase", "PackMate", "TravelScale", "BudgetPack", "UniversalStrap", "BambooLine", "SilkRoute", "Nike", "Adidas", "Apple", "Samsung", "Uniqlo", "Muji"),
    "color": ("黑色", "白色", "红色", "蓝色", "绿色", "黄色", "灰色", "粉色", "紫色", "棕色", "米色", "橙色", "银色", "金色", "军绿色", "沙漠黄"),
    "style": ("小众设计", "经典款", "简约", "复古", "运动", "商务", "休闲", "轻量收纳", "轻量", "现代", "日式", "北欧"),
    "ship_to": tuple(sorted(COUNTRIES)),
}
ALIASES = {name: {v.casefold(): v for v in values} for name, values in _VALUES.items()}
ALIASES["material"].update({"plastic": "塑料", "leather": "皮革", "canvas": "帆布", "cotton": "棉", "stainless steel": "不锈钢", "metal": "金属", "glass": "玻璃", "ceramic": "陶瓷", "bamboo": "竹", "nylon": "尼龙", "wool": "羊毛", "silicone": "硅胶", "wood": "木", "linen": "亚麻"})
ALIASES["color"].update({"black": "黑色", "white": "白色", "red": "红色", "blue": "蓝色", "green": "绿色", "gray": "灰色", "grey": "灰色"})
ALIASES["style"].update({"minimalist": "简约", "classic": "经典款", "vintage": "复古", "niche": "小众设计"})


def _statement(kind: str, attribute: str, value: str, currency: str | None) -> str:
    if attribute == "budget_max":
        return f"预算上限 {value} {currency}"
    if attribute == "ship_to":
        return f"收货国家 {value}"
    prefix = "喜欢" if kind == "like" else "不要"
    if attribute == "material" and value == "塑料":
        return prefix + "塑料材质"
    return prefix + value


# Exact known phrases; suffixes/newlines/embedded role delimiters never match.
_LEGACY = {}
for _attribute, _values in _VALUES.items():
    if _attribute == "ship_to":
        continue
    for _value in _values:
        for _kind in ("like", "dislike"):
            _LEGACY[(_kind, _statement(_kind, _attribute, _value, None))] = (_attribute, _value)
_LEGACY[("like", "喜欢旧款帆布")] = ("material", "帆布")


def normalize_preference(
    kind: str, statement: str = "", *, attribute: str | None = None,
    value: str | int | float | None = None, currency: str | None = None,
) -> tuple[str, str, str | None, str]:
    """Validate a closed fact and generate display text; never persist raw input."""
    if kind not in ("like", "dislike"):
        raise ValueError("kind 必须为 like 或 dislike")
    if attribute is None:
        if value is not None or currency is not None:
            raise ValueError("请同时提供 attribute 和 value")
        fact = _LEGACY.get((kind, statement))
        if fact is None:
            raise ValueError("请提供支持的商品偏好属性和有效值")
        attribute, value = fact
    elif not isinstance(attribute, str):
        raise ValueError("不支持的偏好属性")
    if attribute == "budget_max":
        if kind != "like" or currency not in CURRENCIES:
            raise ValueError("预算上限需要有效币种，且 kind 为 like")
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ValueError("预算必须是正数")
        raw = str(value)
        if not re.fullmatch(r"[0-9]{1,7}(?:\.[0-9]{1,2})?", raw):
            raise ValueError("预算仅支持最多两位小数")
        amount = Decimal(raw)
        if not Decimal("0") < amount <= Decimal("1000000"):
            raise ValueError("预算应在 0 到 1000000 之间")
        canonical = f"{amount:.2f}"
    else:
        if currency is not None or attribute not in ALIASES or not isinstance(value, str):
            raise ValueError("不支持的偏好属性或值")
        canonical = ALIASES[attribute].get(value.casefold())
        if canonical is None or (attribute == "ship_to" and kind != "like"):
            raise ValueError("不支持的偏好属性值")
    generated = _statement(kind, attribute, canonical, currency)
    # Explicit slots + raw text must agree; arbitrary text is never silently accepted.
    if statement and statement != generated and _LEGACY.get((kind, statement)) != (attribute, canonical):
        raise ValueError("偏好陈述与结构化属性不一致")
    return attribute, canonical, currency, generated


def safe_preference(preference: BuyerPreference) -> BuyerPreference | None:
    """Return a canonical safe copy, or quarantine an unrecognized legacy fact."""
    try:
        attribute, value, currency, statement = normalize_preference(
            preference.kind,
            preference.statement if preference.attribute is None else "",
            attribute=preference.attribute, value=preference.value, currency=preference.currency,
        )
    except (ValueError, TypeError, AttributeError):
        return None
    return replace(preference, attribute=attribute, value=value, currency=currency, statement=statement)
