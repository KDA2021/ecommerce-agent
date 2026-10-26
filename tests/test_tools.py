# -*- coding: utf-8 -*-
from app.application.tools.order_tools import build_order_tools
from app.application.tools.product_search import build_product_search_tool
from app.application.usecases.order_usecases import OrderItemInput
from app.domain.order.address import Address
from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot


async def test_product_search_tool_returns_hits():
    from app.domain.catalog.product_search_spec import ProductSearchSpec

    class _Usecase:
        async def execute(self, spec):
            assert isinstance(spec, ProductSearchSpec)
            return {
                "hits": [
                    {
                        "product_id": "P1001",
                        "reasons": [{"code": "budget_match", "label": "预算命中"}],
                    },
                ],
                "recall_strategy": "keyword_2gram",
                "top_picks": [{"product_id": "P1001", "reason_labels": ["预算命中"]}],
            }

    tool = build_product_search_tool(_Usecase())
    result = await tool.ainvoke({"normalized_query": "旅行三件套", "top_k": 1})
    assert result["hits"][0]["product_id"] == "P1001"
    # P10-3：商品卡 reasons 与前 3 对比数据必须原样透传（前端渲染依赖）。
    assert result["hits"][0]["reasons"] == [{"code": "budget_match", "label": "预算命中"}]
    assert result["top_picks"] == [{"product_id": "P1001", "reason_labels": ["预算命中"]}]
    # 工具描述也提示模型"只可如实转述理由"。
    assert "reasons" in tool.description


async def test_create_order_uses_buyer_from_context():
    place = _FakePlace()
    tools = build_order_tools(place, None, None, require_confirmation=False)  # type: ignore[arg-type]
    create = next(t for t in tools if t.name == "create_order")
    token = ShoppingContext.set(
        ShoppingContextSnapshot(shopping_session_id="s1", buyer_id="buyer-9", locale="zh-CN", currency="CNY")
    )
    try:
        await create.ainvoke(
            {
                "items": [{"product_id": "P1001", "sku_id": "P1001-S1", "quantity": 1}],
                "shipping_address": {
                    "recipient_name": "张三", "country": "CN", "city": "杭州",
                    "address_line": "某路 1 号", "state": "浙江", "postal_code": "310000", "phone": "13800000000",
                },
            }
        )
    finally:
        ShoppingContext.reset(token)
    assert place.buyer_id == "buyer-9"


class _FakePlace:
    def __init__(self):
        self.buyer_id = None

    async def execute(self, buyer_id: str, items: list[OrderItemInput], shipping_address: Address) -> dict:
        self.buyer_id = buyer_id
        assert shipping_address.city == "杭州"
        return {"order_id": "GBX-000001", "status": "CONFIRMED"}
