# -*- coding: utf-8 -*-
from datetime import datetime, timezone

from app.domain.catalog.money import Money
from app.domain.catalog.product import Product, ProductHighlight
from app.domain.catalog.sku import Sku
from app.infrastructure.persistence.mappers import product_from_dict, product_to_dict
from app.infrastructure.persistence.mysql_repositories import _aware_utc, _naive_utc


def test_product_roundtrip():
    product = Product(
        product_id="P1001",
        title="旅行三件套",
        brand="Nomadica",
        category="旅行装备",
        origin_country="VN",
        description="抗造 轻便 无塑料",
        highlights=[ProductHighlight("材质", "帆布")],
        ships_to=["CN", "US"],
        skus=[Sku(sku_id="P1001-S1", spec="军绿色", price=Money.from_major_units(189.0, "CNY"), stock=50)],
    )
    restored = product_from_dict(product_to_dict(product))
    assert restored.product_id == "P1001"
    assert restored.skus[0].price.amount_in_minor_units == 18900
    assert restored.skus[0].stock == 50
    assert restored.searchable_text() == product.searchable_text()


def test_utc_naive_roundtrip():
    aware = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)
    naive = _naive_utc(aware)
    assert naive.tzinfo is None
    restored = _aware_utc(naive)
    assert restored == aware
