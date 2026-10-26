# -*- coding: utf-8 -*-
"""CatalogSearchUseCase

商品检索核心 UseCase，对齐参考实现五步流程：
    1. EmbeddingClient 把 normalized_query 向量化
    2. ProductVectorIndex.search(top_n) 拿候选 product_id（Chroma，cosine）
    3. ProductRepository.find_by_ids 还原 Product 聚合
    4. Reranker 精排取 top_k；失败/未配置降级按向量分排序（rerank_applied=false）
    5. 组装商品卡 JSON；命中 ship_to 时内联到手价（小计+运费+关税，统一目标币种）

可解释推荐（P10-3）：商品卡带 ``reasons``，只写**真实发生的事**——预算命中 /
可发国家 / 偏好记忆命中，判定逻辑在 ``recommendation_reasons`` 纯函数里。
没有显式预算或收货国就绝不写对应理由；偏好词在商品文本里对不上也不写。
另外把前 3 个商品整理成 ``top_picks``，供前端并排对比。

降级链（recall_strategy 如实标注）：
    embedding_rerank → embedding_only → keyword_2gram（embedding 服务异常时兜底）

计价收敛设计：到手价在检索链路内联计算（TariffSchedule 规则内核），
不给 Agent 单独暴露比价/运费工具，减少不必要的工具调用轮次。

过滤可观测：被 ship_to / price_max_major 硬约束挡掉的候选以 filtered_out 摘要回传，
让模型能区分"库里没有这个商品"与"有但不满足约束"，不致于给出误导性结论。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Optional

from app.application.usecases.recommendation_reasons import (
    build_reasons,
    preference_matches,
)
from app.domain.buyer.preference_facts import safe_preference
from app.domain.catalog.exchange_rate import ExchangeRateTable
from app.domain.catalog.ports.product_repository import ProductRepository
from app.domain.catalog.ports.retrieval_ports import (
    EmbeddingClient,
    ProductVectorIndex,
    Reranker,
)
from app.domain.catalog.product import Product
from app.domain.catalog.product_search_spec import ProductSearchSpec
from app.domain.shipping.tariff_schedule import TariffSchedule
from app.infrastructure.context import ShoppingContext
from app.infrastructure.tracing import traced_span

logger = logging.getLogger(__name__)

# 一阶段召回候选数（> top_k，给精排留空间）
_RECALL_TOP_N = 8

# 被硬约束挡掉的候选回传条数上限（只回摘要，避免上下文膨胀）
_FILTERED_OUT_LIMIT = 3

# 对比视图条数（P10-3：前 3 个商品并排对比）
COMPARISON_TOP_N = 3

# 单卡"偏好记忆命中"理由条数上限（只做上限，不编造：命中几条写几条）
PREFERENCE_REASON_LIMIT = 3


@dataclass(frozen=True)
class ProductCard:
    """检索结果商品卡：商品字段 + 分数 + 可选到手价明细 + 推荐理由。"""

    product_id: str              # SPU 标识
    title: str                   # 商品标题
    brand: str                   # 品牌
    category: str                # 品类
    origin_country: str          # 产地国家二位码
    price_major: float           # 主 SKU 价格（主单位）
    currency: str                # price_major 的币种
    highlights: list[str]        # 已格式化的卖点文案
    skus: list[dict]             # SKU 摘要列表
    score: float                 # 召回 / 精排得分
    landed_price: Optional[dict]  # ship_to 命中时的到手价明细，未命中为 None
    ships_to: Optional[list[str]] = None  # 可发国家二位码列表（对比视图展示用）
    reasons: Optional[list[dict]] = None  # 仅真实命中的推荐理由；无命中为空列表

    def __post_init__(self) -> None:
        """把可选集合字段补齐成空列表，避免下游拿到 None 再判空。"""
        if self.ships_to is None:
            object.__setattr__(self, "ships_to", [])
        if self.reasons is None:
            object.__setattr__(self, "reasons", [])

    def to_dict(self) -> dict:
        """转成对外 JSON 形状；``landed_price`` 非空时才输出该字段。

        Returns:
            商品卡 dict。
        """
        card = {
            "product_id": self.product_id,
            "title": self.title,
            "brand": self.brand,
            "category": self.category,
            "origin_country": self.origin_country,
            "price_major": self.price_major,
            "currency": self.currency,
            "highlights": self.highlights,
            "skus": self.skus,
            "score": round(self.score, 4),
            "reasons": list(self.reasons or []),
        }
        if self.landed_price is not None:
            card["landed_price"] = self.landed_price
        return card

    def to_comparison(self) -> dict:
        """转成对比视图一行：价格 / 重量 / 可发国家 / 亮点 / 推荐理由。

        ``weight`` 从亮点里取"重量"标注（如"全套 420g 轻便"），没有就为 None——
        宁可为空也不猜一个重量数字。

        Returns:
            对比行 dict。
        """
        weight = None
        for highlight in self.highlights:
            if highlight.startswith("重量"):
                weight = highlight.split("：", 1)[-1]
                break
        landed_total = None
        if self.landed_price:
            landed_total = self.landed_price.get("landed_total_major")
        return {
            "product_id": self.product_id,
            "title": self.title,
            "price_major": self.price_major,
            "currency": self.currency,
            "landed_total_major": landed_total,
            "weight": weight,
            "origin_country": self.origin_country,
            "ships_to": list(self.ships_to or []),
            "highlights": list(self.highlights),
            "reason_labels": [reason.get("label") for reason in self.reasons or []],
        }


def tokenize(text: str) -> set[str]:
    """极简分词：空格切词 + 中文连续段落的 2-gram（关键词降级召回用）。

    Args:
        text: 原始文本。

    Returns:
        词项集合。
    """
    terms: set[str] = set()
    for chunk in text.lower().split():
        terms.add(chunk)
        # 对含 CJK 的 chunk 补 2-gram，缓解中文无空格问题
        if any("\u4e00" <= ch <= "\u9fff" for ch in chunk) and len(chunk) >= 2:
            terms.update(chunk[i : i + 2] for i in range(len(chunk) - 1))
    return terms


class CatalogSearchUseCase:
    """商品检索核心用例：向量召回 → rerank 精排 → 硬过滤 → 商品卡。"""

    def __init__(
        self,
        product_repo: ProductRepository,
        embedder: Optional[EmbeddingClient] = None,
        vector_index: Optional[ProductVectorIndex] = None,
        reranker: Optional[Reranker] = None,
        tariff_schedule: Optional[TariffSchedule] = None,
        tracer=None,
        preference_store=None,
    ) -> None:
        """注入仓储与可选的 embedding / 向量索引 / reranker / 关税规则。

        Args:
            product_repo: 商品仓储。
            embedder: 可选 embedding 客户端。
            vector_index: 可选向量索引。
            reranker: 可选精排器。
            tariff_schedule: 可选关税规则，缺省用内置静态汇率表。
            tracer: 可选 LocalTraceLogger / span tracer。
            preference_store: 可选买家偏好存储；给了才能算"偏好记忆命中"理由。
        """
        self._product_repo = product_repo
        self._embedder = embedder
        self._vector_index = vector_index
        self._reranker = reranker
        self._tariff = tariff_schedule or TariffSchedule(rates=ExchangeRateTable())
        self._tracer = tracer
        self._preference_store = preference_store

    async def execute(self, spec: ProductSearchSpec) -> dict:
        """执行检索链路，失败时按 ``embedding_rerank → embedding_only → keyword_2gram`` 降级并如实标注。

        Args:
            spec: 标准化检索规格。

        Returns:
            含 hits / recall_strategy / rerank_applied 的结果 dict。
        """
        spec = await self._with_buyer_preferences(spec)
        scored: list[tuple[float, Product]] = []
        recall_strategy = "keyword_2gram"
        rerank_applied = False

        # 未配置 embedding / 向量索引时跳过向量召回，直接走关键词兜底。
        if self._embedder is not None and self._vector_index is not None:
            try:
                scored = await self._vector_recall(spec)
                recall_strategy = "embedding_only"
            except Exception as err:  # noqa: BLE001 —— 召回基建异常必须降级而非失败
                logger.warning("向量召回不可用，降级关键词召回：%s", err)
                scored = []

        if recall_strategy == "embedding_only" and scored:
            # 二阶段精排；失败降级按向量分排序
            try:
                scored = await self._rerank(spec, scored)
                recall_strategy = "embedding_rerank"
                rerank_applied = True
            except Exception as err:  # noqa: BLE001
                logger.warning("rerank 不可用，按向量分排序：%s", err)
        elif not scored:
            # 向量召回为空：走关键词 2-gram 兜底。
            scored = await self._keyword_recall(spec)
            recall_strategy = "keyword_2gram"

        # ship_to / 价格硬约束过滤 + top_k 截断（硬约束走结构化过滤，不交给模型）
        filtered: list[tuple[float, Product]] = []
        filtered_out: list[dict] = []
        async with traced_span(self._tracer, "filter", {"candidates": len(scored)}) as span:
            for score, product in scored:
                reason = self._reject_reason(product, spec)
                if reason is None:
                    filtered.append((score, product))
                elif len(filtered_out) < _FILTERED_OUT_LIMIT:
                    filtered_out.append(self._to_rejected(product, spec, reason))
            if span is not None and getattr(span, "attrs", None) is not None:
                span.attrs["kept"] = len(filtered)
                span.attrs["rejected"] = len(filtered_out)

        hits = [self._to_card(score, product, spec) for score, product in filtered[: spec.top_k]]
        result = {
            "hits": [card.to_dict() for card in hits],
            "total_candidates": len(filtered),
            "recall_strategy": recall_strategy,
            "rerank_applied": rerank_applied,
        }
        if hits:
            # 前 3 个商品给前端并排对比（价格/重量/可发国家/亮点 + 推荐理由）。
            result["top_picks"] = [card.to_comparison() for card in hits[:COMPARISON_TOP_N]]
        if filtered_out:
            # 如实告知"召回到了但被硬约束挡掉"，否则模型分不清"库里没有"与"被过滤"，
            # 会把超预算商品答成"没有这个商品"
            result["filtered_out"] = filtered_out
        return result

    async def _with_buyer_preferences(self, spec: ProductSearchSpec) -> ProductSearchSpec:
        """把买家长期偏好注入检索规格（供 reasons 计算），失败不阻断检索。

        偏好来源优先级：调用方显式传入的 ``spec.preference_statements`` > 注入的
        ``preference_store`` + 当前 ``ShoppingContext.buyer_id``。取不到就返回空偏好——
        宁可不写"偏好命中"理由，也不猜。

        Args:
            spec: 原始检索规格。

        Returns:
            带偏好原文的检索规格（可能原样返回）。
        """
        if spec.preference_statements or self._preference_store is None:
            return spec
        snapshot = ShoppingContext.current()
        buyer_id = snapshot.buyer_id if snapshot else ""
        if not buyer_id:
            return spec
        try:
            preferences = await self._preference_store.list_by_buyer(buyer_id)
        except Exception as err:  # noqa: BLE001 —— 偏好读取失败只影响理由，不应把检索搞挂
            logger.warning("读取买家偏好失败，跳过偏好理由：%s", err)
            return spec
        # Recommendation reasons are model-visible too; they must use the same
        # canonical business boundary as preference hints and cache rendering.
        safe = (safe_preference(preference) for preference in preferences)
        statements = tuple(p.statement for p in safe if p is not None)
        if not statements:
            return spec
        return replace(spec, preference_statements=statements)

    def _reject_reason(self, product: Product, spec: ProductSearchSpec) -> Optional[str]:
        """返回硬约束拒绝原因，None 表示通过。"""
        if spec.ship_to and spec.ship_to not in product.ships_to:
            return "ship_to_unavailable"
        if not self._within_price_cap(product, spec):
            return "over_price_cap"
        return None

    def _to_rejected(self, product: Product, spec: ProductSearchSpec, reason: str) -> dict:
        """把被硬约束挡掉的候选渲染成 ``filtered_out`` 摘要。

        Args:
            product: 被拒商品。
            spec: 检索规格。
            reason: 拒绝原因码。

        Returns:
            摘要 dict。
        """
        primary_in_target = self._tariff.rates.convert(product.primary_sku().price, spec.target_currency)
        return {
            "product_id": product.product_id,
            "title": product.title,
            "category": product.category,
            "price_major": round(primary_in_target.to_major_units(), 2),
            "currency": spec.target_currency,
            "reason": reason,
        }

    def _within_price_cap(self, product: Product, spec: ProductSearchSpec) -> bool:
        """判断商品主 SKU 到手价是否不超过 ``price_max_major``。

        Args:
            product: 候选商品。
            spec: 检索规格。

        Returns:
            是否通过价格硬约束。
        """
        if spec.price_max_major is None:
            return True
        primary_in_target = self._tariff.rates.convert(product.primary_sku().price, spec.target_currency)
        return primary_in_target.to_major_units() <= spec.price_max_major

    # ---- 一阶段：向量召回 ----

    async def _embed_query(self, spec: ProductSearchSpec) -> list[float]:
        """把 normalized_query 向量化，并记录 embedding span。

        Args:
            spec: 检索规格。

        Returns:
            embedding 向量。
        """
        async with traced_span(
            self._tracer,
            "embedding",
            {"query_length": len(spec.normalized_query)},
        ) as span:
            embedding = await self._embedder.embed(spec.normalized_query)
            if span is not None and getattr(span, "attrs", None) is not None:
                span.attrs["vector_dim"] = len(embedding)
            return embedding

    async def _vector_recall(self, spec: ProductSearchSpec) -> list[tuple[float, Product]]:
        """向量召回 top-N 候选并还原 Product 聚合。

        Args:
            spec: 检索规格。

        Returns:
            ``[(score, Product)]``。
        """
        embedding = await self._embed_query(spec)
        async with traced_span(
            self._tracer,
            "vector_recall",
            {"top_n": _RECALL_TOP_N},
        ) as span:
            vector_hits = await self._vector_index.search(embedding, top_n=_RECALL_TOP_N)
            products = await self._product_repo.find_by_ids([hit.product_id for hit in vector_hits])
            by_id = {product.product_id: product for product in products}
            result = [
                (hit.score, by_id[hit.product_id])
                for hit in vector_hits
                if hit.product_id in by_id
            ]
            if span is not None and getattr(span, "attrs", None) is not None:
                span.attrs["recalled"] = len(result)
            return result

    # ---- 二阶段：精排 ----

    async def _rerank(
        self,
        spec: ProductSearchSpec,
        scored: list[tuple[float, Product]],
    ) -> list[tuple[float, Product]]:
        """用 reranker 对候选精排；未配置或推理失败时抛异常由调用方降级。

        Args:
            spec: 检索规格。
            scored: 第一阶段候选。

        Returns:
            按精排分数降序的 ``[(score, Product)]``。
        """
        async with traced_span(
            self._tracer,
            "rerank",
            {"candidates": len(scored)},
        ) as span:
            if self._reranker is None:
                raise RuntimeError("Reranker 未配置")
            documents = [product.searchable_text() for _, product in scored]
            rerank_scores = await self._reranker.rerank(spec.normalized_query, documents)
            reranked = [
                (rerank_scores[i], product)
                for i, (_, product) in enumerate(scored)
            ]
            reranked.sort(key=lambda pair: pair[0], reverse=True)
            if span is not None and getattr(span, "attrs", None) is not None:
                span.attrs["reranked"] = len(reranked)
            return reranked

    # ---- 兜底：关键词召回 ----

    async def _keyword_recall(self, spec: ProductSearchSpec) -> list[tuple[float, Product]]:
        """关键词 2-gram 兜底召回（embedding 不可用时）。

        Args:
            spec: 检索规格。

        Returns:
            ``[(score, Product)]``。
        """
        query_terms = tokenize(spec.normalized_query)
        candidates: list[tuple[float, Product]] = []
        for product in await self._product_repo.list_all():
            score = self._keyword_score(query_terms, product, spec)
            if score > 0:
                candidates.append((score, product))
        candidates.sort(key=lambda pair: pair[0], reverse=True)
        return candidates

    @staticmethod
    def _keyword_score(query_terms: set[str], product: Product, spec: ProductSearchSpec) -> float:
        """计算 query 与商品检索文本的关键词重合分，品类槽位命中额外加权。

        Args:
            query_terms: query 分词集合。
            product: 候选商品。
            spec: 检索规格。

        Returns:
            关键词得分（无重合为 0.0）。
        """
        doc_terms = tokenize(product.searchable_text())
        matched = query_terms & doc_terms
        if not matched:
            return 0.0
        score = float(len(matched))
        # 品类槽位命中加权，让"槽位过滤"优于全文命中
        if spec.category and spec.category in product.category:
            score += 3.0
        return score

    # ---- 商品卡组装（含到手价内联 + 可解释推荐理由）----

    def _to_reasons(
        self,
        product: Product,
        spec: ProductSearchSpec,
        landed_price: Optional[dict],
    ) -> list[dict]:
        """计算商品卡推荐理由；只输出真实命中的事实，绝不编造。

        三类事实：
            budget_match     有 ``price_max_major`` 且到手价 ≤ 上限；
            ship_to_match    有 ``ship_to`` 且商品可发该国；
            preference_match 买家偏好原句的关键词全部出现在商品文本里。

        Args:
            product: 命中的商品。
            spec: 检索规格。
            landed_price: 已内联的到手价明细（可为 None / 含 unavailable_reason）。

        Returns:
            ``[{'code', 'label', ...}]``；无真实命中时为 ``[]``。
        """
        budget_matched = False
        if spec.price_max_major is not None:
            if landed_price and "landed_total_major" in landed_price:
                # 有到手价就用到手价比：这比裸价更接近用户真实支出。
                budget_matched = landed_price["landed_total_major"] <= spec.price_max_major
            else:
                budget_matched = self._within_price_cap(product, spec)

        ship_to_matched = bool(spec.ship_to) and spec.ship_to in product.ships_to

        product_text = product.searchable_text()
        matched_statements = [
            statement
            for statement in spec.preference_statements
            if preference_matches(statement, product_text)
        ][:PREFERENCE_REASON_LIMIT]

        return build_reasons(
            budget_matched=budget_matched,
            ship_to_matched=ship_to_matched,
            matched_preference_statements=matched_statements,
        )

    def _to_card(self, score: float, product: Product, spec: ProductSearchSpec) -> ProductCard:
        """组装商品卡；``spec.ship_to`` 命中时内联到手价。

        Args:
            score: 该商品的得分。
            product: 命中的商品。
            spec: 检索规格。

        Returns:
            ProductCard。
        """
        primary = product.primary_sku()
        landed_price: Optional[dict] = None
        if spec.ship_to:
            try:
                quote = self._tariff.quote(
                    subtotal=primary.price,
                    category=product.category,
                    ship_to=spec.ship_to,
                    quantity=1,
                    target_currency=spec.target_currency,
                )
                landed_price = quote.to_dict()
            except ValueError as err:
                # 目的国不在规则表内：如实标注，不编造数字
                landed_price = {"unavailable_reason": str(err)}
        return ProductCard(
            product_id=product.product_id,
            title=product.title,
            brand=product.brand,
            category=product.category,
            origin_country=product.origin_country,
            price_major=primary.price.to_major_units(),
            currency=primary.price.currency,
            highlights=[f"{h.label}：{h.detail}" if h.detail else h.label for h in product.highlights],
            skus=[
                {
                    "sku_id": sku.sku_id,
                    "spec": sku.spec,
                    "price_major": sku.price.to_major_units(),
                    "currency": sku.price.currency,
                    "stock": sku.stock,
                }
                for sku in product.skus
            ],
            score=score,
            landed_price=landed_price,
            ships_to=list(product.ships_to),
            reasons=self._to_reasons(product, spec, landed_price),
        )
