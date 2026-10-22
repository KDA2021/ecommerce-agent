# -*- coding: utf-8 -*-
"""领域对象 <-> 数据库 JSON 载荷转换。"""
from __future__ import annotations

from typing import Any

from app.domain.catalog.money import Money
from app.domain.catalog.product import Product, ProductHighlight
from app.domain.catalog.sku import Sku


def product_to_dict(product: Product) -> dict:
    """Product → 可入库 JSON 载荷（价格存最小单位）。

    Args:
        product: 商品聚合。

    Returns:
        JSON 载荷 dict。
    """
    return {
        "product_id": product.product_id,
        "title": product.title,
        "brand": product.brand,
        "category": product.category,
        "origin_country": product.origin_country,
        "description": product.description,
        "highlights": [{"label": h.label, "detail": h.detail} for h in product.highlights],
        "ships_to": list(product.ships_to),
        "skus": [
            {
                "sku_id": sku.sku_id,
                "spec": sku.spec,
                "price_minor": sku.price.amount_in_minor_units,
                "currency": sku.price.currency,
                "stock": sku.stock,
            }
            for sku in product.skus
        ],
    }


def product_from_dict(data: dict[str, Any]) -> Product:
    """JSON 载荷 → Product 聚合。

    Args:
        data: 商品 JSON 载荷。

    Returns:
        Product。
    """
    return Product(
        product_id=data["product_id"],
        title=data["title"],
        brand=data["brand"],
        category=data["category"],
        origin_country=data["origin_country"],
        description=data["description"],
        highlights=[ProductHighlight(h["label"], h.get("detail", "")) for h in data.get("highlights", [])],
        ships_to=list(data.get("ships_to", [])),
        skus=[
            Sku(
                sku_id=s["sku_id"],
                spec=s["spec"],
                price=Money.of(s["price_minor"], s["currency"]),
                stock=s["stock"],
            )
            for s in data.get("skus", [])
        ],
    )
