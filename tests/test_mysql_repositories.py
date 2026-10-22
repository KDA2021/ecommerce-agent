# tests/test_mysql_repositories.py
import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_MYSQL_URL"),
    reason="TEST_MYSQL_URL 未设置，跳过 MySQL 集成测试",
)


@pytest.fixture()
async def engine():
    from app.infrastructure.persistence.db import bootstrap_schema, create_engine
    engine = create_engine(os.environ["TEST_MYSQL_URL"])
    await bootstrap_schema(engine)
    yield engine
    await engine.dispose()


async def test_product_and_order_roundtrip(engine):
    from app.application.usecases.order_usecases import OrderItemInput, PlaceOrderUseCase
    from app.domain.order.address import Address
    from app.infrastructure.persistence.mysql_repositories import (
        MySqlOrderRepository,
        MySqlProductRepository,
    )
    from app.infrastructure.persistence.seed_products import build_seed_products

    product_repo = MySqlProductRepository(engine)
    if await product_repo.count() == 0:
        await product_repo.insert_many(build_seed_products())
    product = await product_repo.find_by_id("P1001")
    assert product is not None

    order_repo = MySqlOrderRepository(engine)
    place = PlaceOrderUseCase(product_repo, order_repo)
    try:
        snapshot = await place.execute(
            buyer_id="buyer-1",
            items=[OrderItemInput(product_id="P1001", sku_id="P1001-S1", quantity=1)],
            shipping_address=Address(
                recipient_name="张三", country="CN", state="浙江", city="杭州",
                address_line="西湖区某路 1 号", postal_code="310000", phone="13800000000",
            ),
        )
        assert snapshot["status"] == "CONFIRMED"
    finally:
        product = await product_repo.find_by_id("P1001")
        sku = product.find_sku("P1001-S1")
        sku.restore_stock(1)
        await product_repo.save(product)
