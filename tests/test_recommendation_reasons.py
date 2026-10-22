# -*- coding: utf-8 -*-
"""P10-3 可解释推荐回归：reasons 只在**真实命中**时出现。

覆盖四层：
1. **预算命中**：只有显式给了 ``price_max_major`` 且到手价 ≤ 上限才写理由；
2. **可发国家**：只有显式给了 ``ship_to`` 且商品确实发往该国才写理由；
3. **偏好记忆命中**：买家偏好原句的关键词必须全部出现在商品文本里；
4. **无命中即无理由**：没给预算/收货国/偏好时，商品卡 reasons 是空列表。

另外锁死两件事：
- 理由的 code 只允许出现在 ``REASON_LABELS`` 白名单里（防"编造理由"回归）；
- ``product_search`` 工具把 reasons / top_picks 原样透传给调用方（前端契约）。
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.application.tools.product_search import build_product_search_tool
from app.application.usecases.catalog_search import CatalogSearchUseCase
from app.application.usecases.recommendation_reasons import (
    REASON_BUDGET_MATCH,
    REASON_LABELS,
    REASON_PREFERENCE_MATCH,
    REASON_SHIP_TO_MATCH,
)
from app.domain.buyer.preference import BuyerPreference
from app.domain.catalog.money import Money
from app.domain.catalog.product import Product, ProductHighlight
from app.domain.catalog.product_search_spec import ProductSearchSpec
from app.domain.catalog.sku import Sku
from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
from app.infrastructure.persistence.in_memory_repositories import InMemoryProductRepository

BUYER = "buyer-reco"

#: 理由 code 白名单：任何不在其中的 code 都算"编造理由"。
ALLOWED_CODES = {REASON_BUDGET_MATCH, REASON_SHIP_TO_MATCH, REASON_PREFERENCE_MATCH}


# --------------------------------------------------------------------------- #
# 测试夹具
# --------------------------------------------------------------------------- #
def _product(
    product_id: str,
    title: str,
    *,
    description: str,
    highlights: list[tuple[str, str]],
    ships_to: list[str],
    major: float = 100.0,
    currency: str = "CNY",
    category: str = "旅行装备",
) -> Product:
    """构造一个最小可用商品（主 SKU + 亮点 + 可发国家）。"""
    return Product(
        product_id=product_id,
        title=title,
        brand="TestBrand",
        category=category,
        origin_country="CN",
        description=description,
        highlights=[ProductHighlight(label, detail) for label, detail in highlights],
        ships_to=list(ships_to),
        skus=[Sku(sku_id=f"{product_id}-S1", spec="标准", price=Money.from_major_units(major, currency), stock=10)],
    )


def _three_products() -> list[Product]:
    """三个商品，分别覆盖"预算内可发 + 偏好命中 / 超预算 / 便宜但不可发"。"""
    return [
        # P-RECO-1：129 CNY，可发 CN/US，命中"喜欢轻量收纳"偏好。
        _product(
            "P-RECO-1",
            "Nomadica 轻量收纳旅行三件套",
            description="旅行 收纳 旅行套装 轻量 套装 帆布材质 适合长途飞行",
            highlights=[("材质", "帆布，非塑料"), ("重量", "全套 420g 轻便")],
            ships_to=["CN", "US"],
            major=129.0,
        ),
        # P-RECO-2：899 CNY，超预算，可发 CN/US，且不含"轻量/收纳"。
        _product(
            "P-RECO-2",
            "TrailOx 铝框登机行李箱",
            description="旅行 出差 铝镁合金框架 结实抗摔 万向静音轮 商务",
            highlights=[("箱体", "德国工艺铝框")],
            ships_to=["CN", "US"],
            major=899.0,
        ),
        # P-RECO-3：99 CNY，只发 CN（ship_to=US 时应被硬过滤掉）。
        _product(
            "P-RECO-3",
            "GlideCase 塑料硬壳收纳箱",
            description="收纳 箱体 塑料 聚碳酸酯硬壳 平价",
            highlights=[("箱体", "PC 塑料硬壳")],
            ships_to=["CN"],
            major=99.0,
        ),
    ]


#: 三个商品都能命中的检索词：P-RECO-1 重合最多（"旅行/收纳/套装"），排第一。
QUERY = "旅行 收纳 套装"


class _FakePreferenceStore:
    """最小偏好存储替身：只实现检索需要读取的 list_by_buyer。"""

    def __init__(self, statements: list[str] | None = None, *, kind: str = "like") -> None:
        self._preferences = [
            BuyerPreference(buyer_id=BUYER, kind=kind, statement=statement)
            for statement in (statements or [])
        ]

    def set(self, statements: list[str], *, kind: str = "like") -> None:
        self._preferences = [
            BuyerPreference(buyer_id=BUYER, kind=kind, statement=statement)
            for statement in statements
        ]

    async def list_by_buyer(self, buyer_id: str) -> list[BuyerPreference]:
        return [p for p in self._preferences if p.buyer_id == buyer_id]


def _usecase(store: _FakePreferenceStore | None = None) -> CatalogSearchUseCase:
    return CatalogSearchUseCase(
        product_repo=InMemoryProductRepository(_three_products()),
        preference_store=store,
    )


def _codes(hit: dict) -> list[str]:
    return [reason["code"] for reason in hit.get("reasons", [])]


def _hit(result: dict, product_id: str) -> dict:
    for hit in result["hits"]:
        if hit["product_id"] == product_id:
            return hit
    raise AssertionError(f"结果里没有 {product_id}：{[h['product_id'] for h in result['hits']]}")


def _context(buyer_id: str = BUYER):
    return ShoppingContext.set(
        ShoppingContextSnapshot(
            shopping_session_id="reco-thread",
            buyer_id=buyer_id,
            locale="zh-CN",
            currency="CNY",
        )
    )


# --------------------------------------------------------------------------- #
# 1. 预算命中
# --------------------------------------------------------------------------- #
async def test_budget_reason_only_when_within_cap():
    """预算理由：上限内命中、超预算不命中（且被 filtered_out 如实标注）；不限预算则谁都不写。"""
    usecase = _usecase()

    capped = await usecase.execute(
        ProductSearchSpec(normalized_query=QUERY, price_max_major=200.0, top_k=5),
    )
    assert REASON_BUDGET_MATCH in _codes(_hit(capped, "P-RECO-1"))
    # 899 CNY 的 P-RECO-2 被价格硬约束挡在结果之外，不允许出现预算命中理由。
    assert all(hit["product_id"] != "P-RECO-2" for hit in capped["hits"])
    assert any(item["product_id"] == "P-RECO-2" for item in capped.get("filtered_out", []))

    uncapped = await usecase.execute(ProductSearchSpec(normalized_query=QUERY, top_k=5))
    assert all(REASON_BUDGET_MATCH not in _codes(hit) for hit in uncapped["hits"]), uncapped["hits"]
    # 不限预算时超预算商品可以正常返回，但同样不许写预算命中。
    assert REASON_BUDGET_MATCH not in _codes(_hit(uncapped, "P-RECO-2"))


async def test_budget_reason_uses_landed_price_when_shipping_known():
    """命中 ship_to 时预算判定用到手价，而不是裸价。

    P-RECO-1 裸价 129：给上限 150 时裸价算命中；但发 CN 的到手价 154 > 150，
    所以真实结论必须是"不命中"——用到手价才不会把含运税的超支说成预算命中。
    """
    usecase = _usecase()

    base_only = await usecase.execute(
        ProductSearchSpec(normalized_query=QUERY, price_max_major=150.0, top_k=5),
    )
    assert REASON_BUDGET_MATCH in _codes(_hit(base_only, "P-RECO-1"))

    with_shipping = await usecase.execute(
        ProductSearchSpec(
            normalized_query=QUERY, price_max_major=150.0, ship_to="CN", top_k=5,
        ),
    )
    hit = _hit(with_shipping, "P-RECO-1")
    assert hit["landed_price"]["landed_total_major"] == 154.0
    assert REASON_BUDGET_MATCH not in _codes(hit)


# --------------------------------------------------------------------------- #
# 2. 国家可发
# --------------------------------------------------------------------------- #
async def test_ship_to_reason_only_when_product_ships_there():
    """可发理由：发 US 的写、只发 CN 的不写；没给 ship_to 时谁都不写。"""
    usecase = _usecase()

    to_us = await usecase.execute(ProductSearchSpec(normalized_query=QUERY, ship_to="US", top_k=5))
    assert REASON_SHIP_TO_MATCH in _codes(_hit(to_us, "P-RECO-1"))
    # P-RECO-3 只发 CN：硬过滤掉，不该出现在结果里（更不能有可发 US 的理由）。
    assert all(hit["product_id"] != "P-RECO-3" for hit in to_us["hits"])

    no_ship_to = await usecase.execute(ProductSearchSpec(normalized_query=QUERY, top_k=5))
    assert all(REASON_SHIP_TO_MATCH not in _codes(hit) for hit in no_ship_to["hits"])


# --------------------------------------------------------------------------- #
# 3. 偏好记忆命中
# --------------------------------------------------------------------------- #
async def test_preference_reason_only_when_keywords_all_present():
    """偏好理由：全词命中才算；只沾一半的词不算，偏好为空时谁都不算。"""
    store = _FakePreferenceStore(["喜欢轻量收纳", "不要塑料材质"])
    usecase = _usecase(store)

    token = _context()
    try:
        result = await usecase.execute(ProductSearchSpec(normalized_query=QUERY, top_k=5))
    finally:
        ShoppingContext.reset(token)

    matched = _hit(result, "P-RECO-1")
    assert REASON_PREFERENCE_MATCH in _codes(matched)
    # 命中的偏好原句如实回传，前端可显示"命中你的偏好：喜欢轻量收纳"。
    statements = [
        reason.get("matched_preference")
        for reason in matched["reasons"]
        if reason["code"] == REASON_PREFERENCE_MATCH
    ]
    assert statements == ["喜欢轻量收纳"]

    # P-RECO-2（铝框行李箱）不含"轻量/收纳"，两条偏好都不该命中。
    assert REASON_PREFERENCE_MATCH not in _codes(_hit(result, "P-RECO-2"))
    # P-RECO-3 含"收纳/塑料"，但"喜欢轻量收纳"缺"轻量"、"不要塑料材质"缺"料材/材质"：
    # 都不该命中——偏好判定不允许"沾一半就算"。
    assert REASON_PREFERENCE_MATCH not in _codes(_hit(result, "P-RECO-3"))


async def test_preference_reason_absent_without_store_or_preferences():
    """没有偏好存储 / 买家没有偏好时不写偏好理由（不猜）。"""
    no_store = await _usecase().execute(ProductSearchSpec(normalized_query=QUERY))
    assert all(REASON_PREFERENCE_MATCH not in _codes(hit) for hit in no_store["hits"])

    store = _FakePreferenceStore([])
    token = _context()
    try:
        empty = await _usecase(store).execute(ProductSearchSpec(normalized_query=QUERY))
    finally:
        ShoppingContext.reset(token)
    assert all(REASON_PREFERENCE_MATCH not in _codes(hit) for hit in empty["hits"])


async def test_preference_store_failure_does_not_break_search():
    """偏好存储读取异常只影响理由，检索本身必须照常返回。"""

    class _BrokenStore:
        async def list_by_buyer(self, buyer_id: str):
            raise RuntimeError("preference store down")

    token = _context()
    try:
        result = await _usecase(_BrokenStore()).execute(
            ProductSearchSpec(normalized_query=QUERY),
        )
    finally:
        ShoppingContext.reset(token)

    assert result["hits"], "偏好读取失败不应把检索搞挂"
    assert all(_codes(hit) == [] for hit in result["hits"])


# --------------------------------------------------------------------------- #
# 4. 无命中即无理由 + 理由 code 白名单
# --------------------------------------------------------------------------- #
async def test_no_reasons_when_nothing_matches():
    """商品确实命中召回，但预算/国家/偏好三条都没命中：reasons 必须是空列表。"""
    result = await _usecase().execute(ProductSearchSpec(normalized_query=QUERY, top_k=5))

    matched = _hit(result, "P-RECO-1")
    assert matched["reasons"] == []


async def test_all_reasons_come_from_factual_whitelist():
    """所有理由的 code 必须在白名单里，label 与 code 对应，绝不出现自造标签。"""
    store = _FakePreferenceStore(["喜欢轻量收纳"])
    token = _context()
    try:
        result = await _usecase(store).execute(
            ProductSearchSpec(
                normalized_query=QUERY,
                price_max_major=200.0,
                ship_to="US",
                top_k=5,
            ),
        )
    finally:
        ShoppingContext.reset(token)

    seen: set[str] = set()
    for hit in result["hits"]:
        for reason in hit["reasons"]:
            assert reason["code"] in ALLOWED_CODES, reason
            assert reason["label"] == REASON_LABELS[reason["code"]]
            seen.add(reason["code"])
    # 三个槽位都给全时，三条事实理由都应该真实出现（而不是被吞掉）。
    assert seen == ALLOWED_CODES, seen


async def test_reasons_are_json_serializable_and_ordered():
    """理由顺序固定（预算 → 可发 → 偏好），且是 JSON 安全的简单结构。"""
    import json

    store = _FakePreferenceStore(["喜欢轻量收纳"])
    token = _context()
    try:
        result = await _usecase(store).execute(
            ProductSearchSpec(
                normalized_query=QUERY,
                price_max_major=200.0,
                ship_to="US",
                top_k=5,
            ),
        )
    finally:
        ShoppingContext.reset(token)

    codes = _codes(_hit(result, "P-RECO-1"))
    assert codes == [REASON_BUDGET_MATCH, REASON_SHIP_TO_MATCH, REASON_PREFERENCE_MATCH]
    assert json.loads(json.dumps(result)) == result


# --------------------------------------------------------------------------- #
# 5. 对比视图：前 3 个商品
# --------------------------------------------------------------------------- #
async def test_top_picks_cover_first_three_hits():
    """top_picks 供前端对比表：最多 3 条，字段覆盖价格/重量/可发国家/亮点/理由。"""
    result = await _usecase().execute(ProductSearchSpec(normalized_query=QUERY, top_k=5))

    picks = result["top_picks"]
    assert len(picks) == min(3, len(result["hits"]))
    assert [pick["product_id"] for pick in picks] == [hit["product_id"] for hit in result["hits"][:3]]

    first = picks[0]
    assert {"product_id", "title", "price_major", "currency", "weight", "ships_to", "highlights", "reason_labels"} <= set(first)
    assert first["weight"] == "全套 420g 轻便"  # 从亮点"重量："里取，不猜
    assert first["ships_to"] == ["CN", "US"]
    assert first["reason_labels"] == []


# --------------------------------------------------------------------------- #
# 6. product_search 工具透传（前端契约）
# --------------------------------------------------------------------------- #
async def test_product_search_tool_forwards_reasons_and_top_picks():
    """工具结果必须把 reasons / top_picks 原样透传，前端才能渲染理由与对比表。"""
    tool = build_product_search_tool(_usecase())

    result = await tool.ainvoke(
        {"normalized_query": QUERY, "price_max_major": 200.0, "ship_to": "US", "top_k": 3},
    )

    assert REASON_BUDGET_MATCH in _codes(_hit(result, "P-RECO-1"))
    assert REASON_SHIP_TO_MATCH in _codes(_hit(result, "P-RECO-1"))
    assert result["top_picks"], result
    assert all("reason_labels" in pick for pick in result["top_picks"])


async def test_product_search_tool_reads_buyer_preferences_from_context():
    """工具不接收偏好参数：跨会话偏好由 usecase 按当前 buyer 自取，模型无法编造。"""
    store = _FakePreferenceStore(["喜欢轻量收纳"])
    tool = build_product_search_tool(_usecase(store))

    token = _context()
    try:
        result = await tool.ainvoke({"normalized_query": QUERY, "top_k": 3})
    finally:
        ShoppingContext.reset(token)

    assert REASON_PREFERENCE_MATCH in _codes(_hit(result, "P-RECO-1"))


def test_product_search_args_schema_has_no_preference_field():
    """入参 schema 不含偏好字段：LLM 不能把"记忆"当参数塞进来。"""
    from app.application.tools.product_search import ProductSearchArgs

    assert "preference_statements" not in ProductSearchArgs.model_fields
    assert set(ProductSearchArgs.model_fields) == {
        "normalized_query",
        "category",
        "ship_to",
        "top_k",
        "price_max_major",
        "target_currency",
    }


@pytest.mark.parametrize(
    ("statement", "product_text", "expected"),
    [
        ("喜欢轻量收纳", "轻量 收纳 旅行三件套", True),
        ("喜欢轻量收纳", "轻量 旅行三件套", False),  # 只沾一半
        ("喜欢轻量收纳", "结实抗摔 行李箱", False),
        ("喜欢塑料材质", "PC 塑料材质箱体", True),   # 全词与词对都命中
        ("不要塑料材质", "SilkRoute 天然材质 无塑料", False),  # "料材"对不上，不硬凑
        ("喜欢", "轻量 收纳", False),               # 整句都是语气词，无判别力
    ],
)
def test_preference_match_rules(statement: str, product_text: str, expected: bool):
    """偏好命中判定是纯函数；"喜欢"这类无实质内容的偏好直接放弃。"""
    from app.application.usecases.recommendation_reasons import preference_matches

    assert preference_matches(statement, product_text) is expected


# --------------------------------------------------------------------------- #
# 7. 前端契约：偏好 chips → preference_hint；理由/对比表在前端纯函数里可用
# --------------------------------------------------------------------------- #
ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
NODE_TEST_FILE = "tests/recommendation.test.mts"


def test_frontend_pure_functions_pass_in_node_test_runner():
    """跑 frontend/tests/recommendation.test.mts：理由渲染 / 对比表 / 偏好 hint 组装。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("未安装 Node，跳过前端纯函数单测")
    if not (FRONTEND / NODE_TEST_FILE).exists():
        pytest.fail(f"前端测试文件缺失：{NODE_TEST_FILE}")

    proc = subprocess.run(
        [node, "--test", NODE_TEST_FILE],
        cwd=str(FRONTEND),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    output = f"{proc.stdout}\n{proc.stderr}"
    assert proc.returncode == 0, f"node --test 失败（exit={proc.returncode}）：\n{output}"
    # 防止测试文件被清空后"0 test passed"也返回 0。
    assert "pass " in output, output
    assert "fail 0" in output, output


def test_frontend_helper_keeps_contract_fields():
    """前端 helper 源码必须仍引用后端契约字段（字段漂移回归）。"""
    source = (FRONTEND / "src" / "products.ts").read_text(encoding="utf-8")
    for key in ("reason_labels", "matched_preference", "top_picks", "ships_to", "landed_total_major"):
        assert key in source, f"products.ts 缺少契约字段：{key}"
    for code in (REASON_BUDGET_MATCH, REASON_SHIP_TO_MATCH, REASON_PREFERENCE_MATCH):
        assert code in source, f"products.ts 缺少理由 code：{code}"

    app_source = (FRONTEND / "src" / "App.tsx").read_text(encoding="utf-8")
    assert "preference_hint" in app_source
    assert "PreferenceChips" in app_source
    assert "ComparisonTable" in app_source
    assert "buildComparisonRows" in app_source


def test_render_preference_directive_only_includes_selected_slots():
    """偏好 chips 渲染成注入文本；没选的槽位不出现（不替用户臆测）。"""
    from app.presentation.server import PreferenceHint, render_preference_directive

    assert render_preference_directive(None) == ""
    assert render_preference_directive(PreferenceHint()) == ""

    text = render_preference_directive(
        PreferenceHint(budget_max_major=200, ship_to="US", category="旅行装备"),
    )
    assert "预算上限 200 CNY" in text
    assert "收货国家 US" in text
    assert "品类 旅行装备" in text
    assert "ship_to=" not in text  # 不是写给机器看的参数名

    only_country = render_preference_directive(PreferenceHint(ship_to="JP"))
    assert "JP" in only_country
    assert "预算" not in only_country


def test_intent_request_accepts_structured_preference_hint():
    """REST 请求体接受结构化 preference_hint；不传时不影响旧调用方。"""
    from app.presentation.server import IntentRequest

    legacy = IntentRequest(shopping_session_id="s", buyer_id="b", query="q")
    assert legacy.preference_hint is None

    with_hint = IntentRequest(
        shopping_session_id="s",
        buyer_id="b",
        query="q",
        preference_hint={"budget_max_major": 200, "ship_to": "US", "category": "旅行装备"},
    )
    assert with_hint.preference_hint.budget_max_major == 200
    assert with_hint.preference_hint.ship_to == "US"


def test_preference_hint_is_injected_into_agent_query_not_cache_key():
    """偏好 hint 进入 Agent 看到的 query，但不改变语义缓存 key / 回显 query。"""
    from fastapi.testclient import TestClient

    from app.presentation.server import create_app

    class _Agent:
        def __init__(self) -> None:
            self.queries: list[str] = []

        async def ainvoke(self, payload, config):
            from langchain_core.messages import AIMessage

            self.queries.append(str(payload["messages"][-1].content))
            return {"messages": [AIMessage(content="好的")]}

        async def aget_state(self, config):
            from types import SimpleNamespace

            return SimpleNamespace(values={"messages": []})

    class _Cache:
        def __init__(self) -> None:
            self.lookups: list[tuple] = []

        async def lookup(self, buyer_id, query, has_history, scope=""):
            self.lookups.append((buyer_id, query, has_history, scope))
            return None

        async def remember(self, buyer_id, query, reply, has_history, scope=""):
            self.lookups.append(("remember", query))

    class _Broker:
        def publish(self, session_id, event_type, payload):
            return None

    class _Container:
        def __init__(self) -> None:
            self.agent = _Agent()
            self.semantic_cache = _Cache()
            self.broker = _Broker()
            self.preference_store = _FakePreferenceStore([])

    container = _Container()
    client = TestClient(create_app(container=container, build_on_startup=False))
    response = client.post(
        "/commerce/intents",
        json={
            "shopping_session_id": "s-hint",
            "buyer_id": BUYER,
            "query": "推荐旅行三件套",
            "preference_hint": {"budget_max_major": 200, "ship_to": "US"},
        },
    )

    assert response.status_code == 200, response.text
    assert len(container.agent.queries) == 1
    agent_query = container.agent.queries[0]
    assert agent_query.startswith("推荐旅行三件套")
    assert "预算上限 200 CNY" in agent_query
    assert "收货国家 US" in agent_query
    # 缓存 key 仍是用户原句：chips 不该把同一句话拆成多个缓存条目。
    assert container.semantic_cache.lookups[0][1] == "推荐旅行三件套"
