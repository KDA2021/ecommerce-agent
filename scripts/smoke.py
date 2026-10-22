# -*- coding: utf-8 -*-
"""P1 + P2 + P3 + P8 + P9 端到端冒烟：MySQL + Chroma + 子 Agent + 语义缓存 + checkpointer + 品类 RAG + 偏好记忆。

开始前先报告 Redis 状态（P9，可选）：`REDIS_URL` 为空时验证 disabled 降级语义，
不会因为本机没有 Redis 而失败；Redis 已配置但不可达时也只提示、不阻断。

九步：
1. 商品检索（旅行三件套；P3 断言 embedding_rerank + rerank_applied）；
2. 下单 + 查单（同一 GBX 订单号，CONFIRMED）；
3. 语义缓存回写 + 命中；
4. 真实 Agent 搜索请求的流式事件（agent.dispatch / tool.result / token.delta / trace_id）；
5. 同一 thread 追问，验证 checkpointer 记忆；
6. P2 品类知识库 RAG 检索（旅行装备挑材质）；
7. P2 偏好记忆写入 / 撤回，并验证语义缓存作用域指纹随之变化；
8. P8 幂等下单：同一幂等键重复 create_order 只产生一张订单且标记 replay；
9. P8 延迟报告：真实运行 scripts/latency_report.py 并校验聚合结果。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

# 允许直接以脚本方式运行：把项目根目录加入 import 路径。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
if hasattr(sys.stdout, "reconfigure"):
    # Windows 控制台默认编码可能吞中文，这里强制 UTF-8 输出。
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.application.agents.main_graph import invoke_agent, invoke_agent_stream
from app.application.memory.preference_selector import render_preference_lines
from app.application.tools.order_tools import OrderItemArgs, _idempotency_key
from app.application.usecases.order_usecases import OrderItemInput
from app.bootstrap import build_container
from app.domain.buyer.preference import BuyerPreference
from app.domain.catalog.product_search_spec import ProductSearchSpec
from app.domain.order.address import Address
from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
from app.infrastructure.settings import load_settings


def _preference_scope(preferences) -> str:
    """语义缓存偏好作用域指纹，与 app/presentation/server.py 的 _preference_scope 同算法。"""
    if not preferences:
        return ""
    return hashlib.sha256(render_preference_lines(preferences).encode()).hexdigest()[:16]


async def _wait_for_expected_events(
    frames: list[str],
    expected: tuple[str, ...],
    timeout: float = 8.0,
) -> None:
    """轮询等待事件帧中同时出现全部 ``expected`` 子串。

    超时不抛异常，由调用方随后的断言给出具体缺失项。

    Args:
        frames: 已收集的 SSE 帧列表。
        expected: 需要全部出现的事件关键字（如 agent.dispatch / token.delta / trace_id）。
        timeout: 最长等待秒数。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if all(any(token in frame for frame in frames) for token in expected):
            return
        await asyncio.sleep(0.05)


async def _report_redis_state(container) -> str:
    """打印 P9 Redis 状态；不强制真实 Redis，disabled 时顺带验证降级语义。

    ``REDIS_URL`` 为空（或 redis 包缺失）时 ``RedisCache`` 是 disabled 实例：
    读按未命中（``None``）、写跳过（``False``）、限流计数按 0（fail-open）、
    幂等 SETNX 按 True（视为抢到，回退 MySQL 唯一索引）。这里对这些安全返回值
    做轻量断言，证明“没有 Redis 也能跑”；Redis 已配置时只 ping 一下做提示，
    不可达也不报错。

    Args:
        container: 装配容器，需带 ``redis_cache``。

    Returns:
        一行可直接打印的中文状态字符串。
    """
    redis_cache = container.redis_cache
    if not redis_cache.enabled:
        got = await redis_cache.get_raw("smoke:redis:probe")
        counted = await redis_cache.zadd_and_count(
            "smoke:redis:probe",
            "smoke-member",
            window_seconds=60.0,
        )
        acquired = await redis_cache.set_if_absent("smoke:redis:probe", "1", ttl=5.0)
        assert got is None, f"disabled Redis 读不应命中：{got!r}"
        assert counted == 0, f"disabled Redis 限流计数应为 0（fail-open）：{counted}"
        assert acquired is True, f"disabled Redis 幂等 SETNX 应视为抢到：{acquired}"
        return (
            "disabled（REDIS_URL 为空 → 缓存按未命中、写跳过、"
            "限流 fail-open、幂等回退 MySQL 唯一索引）"
        )

    alive = await redis_cache.ping()
    return (
        f"enabled（ping={alive}；即使不可达也是读取按未命中、"
        "限流 fail-open、幂等回退 MySQL 唯一索引）"
    )


async def main() -> None:
    """执行 9 步端到端冒烟，并断言关键结果。"""
    container = await build_container(load_settings())
    print("0. Redis 状态:", await _report_redis_state(container))
    collector: asyncio.Task | None = None
    context_token = None
    try:
        # 1. 检索
        result = await container.search_usecase.execute(
            ProductSearchSpec(normalized_query="旅行三件套 抗造 轻便", top_k=1),
        )
        assert result["hits"], "商品检索没有命中"
        assert result["recall_strategy"] == "embedding_rerank", result["recall_strategy"]
        assert result["rerank_applied"] is True
        product = result["hits"][0]
        print(
            "1. 检索命中:",
            product["product_id"],
            product["title"],
            "| recall_strategy:",
            result["recall_strategy"],
        )

        # 2. 下单 + 查单
        address = Address(
            recipient_name="张三",
            country="CN",
            state="浙江",
            city="杭州",
            address_line="西湖区某路 1 号",
            postal_code="310000",
            phone="13800000000",
        )
        order = await container.place_order.execute(
            buyer_id="smoke-buyer",
            items=[
                OrderItemInput(
                    product_id=product["product_id"],
                    sku_id=product["skus"][0]["sku_id"],
                    quantity=1,
                )
            ],
            shipping_address=address,
        )
        queried = await container.query_order.execute(order["order_id"])
        assert queried["status"] == "CONFIRMED", f"订单状态不是 CONFIRMED：{queried['status']}"
        assert queried["order_id"] == order["order_id"], "查单订单号与下单不一致"
        print("2. 下单+查单:", queried["order_id"], queried["status"])

        # 3. 语义缓存
        await container.semantic_cache.remember(
            "smoke-buyer",
            "推荐一个旅行三件套",
            "缓存回复",
            has_history=False,
        )
        hit = await container.semantic_cache.lookup(
            "smoke-buyer",
            "推荐一个旅行三件套",
            has_history=False,
        )
        assert hit is not None, "语义缓存未命中"
        print("3. 缓存命中:", hit.matched_query, "->", hit.reply)

        # 4. Agent 流式事件流（含 P6 token.delta 与 trace_id）
        frames: list[str] = []
        trace_id = uuid4().hex

        async def collect_frames() -> None:
            """持续订阅 broker，把 SSE 帧追加到 frames。"""
            async for frame in container.broker.subscribe("smoke-session"):
                frames.append(frame)

        collector = asyncio.create_task(collect_frames())
        await asyncio.sleep(0.05)

        context_token = ShoppingContext.set(
            ShoppingContextSnapshot(
                shopping_session_id="smoke-session",
                buyer_id="smoke-buyer",
                locale="zh-CN",
                currency="CNY",
                trace_id=trace_id,
            )
        )
        try:
            reply = await invoke_agent_stream(
                container.agent,
                "smoke-session",
                "帮我找一个旅行三件套，预算200元",
                container.broker,
            )
            await _wait_for_expected_events(
                frames,
                ("agent.dispatch", "tool.result", "token.delta", trace_id),
            )
        finally:
            collector.cancel()
            with suppress(asyncio.CancelledError):
                await collector
            collector = None
            ShoppingContext.reset(context_token)
            context_token = None

        assert reply, "流式执行没有返回最终文本"
        assert any("agent.dispatch" in frame for frame in frames), "事件流缺少 agent.dispatch"
        assert any("tool.result" in frame for frame in frames), "事件流缺少 tool.result"
        assert any("token.delta" in frame for frame in frames), "事件流缺少 token.delta"
        assert any(trace_id in frame for frame in frames), f"事件流缺少 trace_id：{trace_id}"
        token_frames = sum(1 for frame in frames if "token.delta" in frame)
        print(
            "4. 流式事件验证:",
            f"{len(frames)} frames，token.delta={token_frames}，trace_id={trace_id}",
        )

        # 5. 多轮记忆
        context_token = ShoppingContext.set(
            ShoppingContextSnapshot(
                shopping_session_id="smoke-session",
                buyer_id="smoke-buyer",
                locale="zh-CN",
                currency="CNY",
            )
        )
        try:
            final_reply = await invoke_agent(
                container.agent,
                "smoke-session",
                "我刚才问的是什么？",
            )
        finally:
            ShoppingContext.reset(context_token)
            context_token = None
        assert final_reply, "第二轮追问没有返回文本"
        print("5. 多轮记忆:", final_reply[:120])
        print("最终回复前缀:", final_reply[:80])

        # 6. P2：品类知识库 RAG
        insights = await container.category_kb.search("旅行装备怎么挑材质", top_k=2)
        assert insights, "品类知识库检索没有命中"
        print("6. 品类 RAG:", len(insights), "条命中；首条标题:", insights[0]["heading"])

        # 7. P2：长期偏好记忆写入 / 撤回 + 语义缓存作用域指纹
        await container.preference_store.append(
            BuyerPreference(buyer_id="smoke-buyer", kind="dislike", statement="不要塑料材质"),
        )
        preferences = await container.preference_store.list_by_buyer("smoke-buyer")
        assert any(p.kind == "dislike" and p.statement == "不要塑料材质" for p in preferences), (
            "偏好写入后未出现在列表中"
        )
        scope_before = _preference_scope(preferences)
        deleted = await container.preference_store.delete("smoke-buyer", "不要塑料材质")
        assert deleted, "偏好删除未命中"
        remaining = await container.preference_store.list_by_buyer("smoke-buyer")
        scope_after = _preference_scope(remaining)
        assert scope_before != scope_after, "偏好撤回后作用域指纹没有变化"
        print("7. 偏好记忆:", f"写入后 {len(preferences)} 条，撤回后 {len(remaining)} 条")
        print("   作用域指纹:", scope_before, "->", scope_after)
        print("P2 冒烟通过：品类 RAG 命中 + 偏好写入/撤回 + 指纹变化")

        # 8. P8：幂等下单 —— 同一幂等键重复 create_order 只产生一张订单
        create_order_tool = next(
            (tool for tool in container.tools if getattr(tool, "name", "") == "create_order"),
            None,
        )
        assert create_order_tool is not None, "工具列表里没有 create_order"
        order_args = {
            "items": [
                {
                    "product_id": product["product_id"],
                    "sku_id": product["skus"][0]["sku_id"],
                    "quantity": 1,
                }
            ],
            "shipping_address": {
                "recipient_name": "张三",
                "country": "CN",
                "state": "浙江",
                "city": "杭州",
                "address_line": "西湖区某路 1 号",
                "postal_code": "310000",
                "phone": "13800000000",
            },
        }
        idem_session = f"smoke-idem-{uuid4().hex[:12]}"
        idem_trace = uuid4().hex
        context_token = ShoppingContext.set(
            ShoppingContextSnapshot(
                shopping_session_id=idem_session,
                buyer_id="smoke-buyer",
                locale="zh-CN",
                currency="CNY",
                trace_id=idem_trace,
            )
        )
        try:
            first_order = await create_order_tool.ainvoke(order_args)
            second_order = await create_order_tool.ainvoke(order_args)
        finally:
            ShoppingContext.reset(context_token)
            context_token = None

        assert isinstance(first_order, dict) and first_order.get("order_id"), first_order
        assert isinstance(second_order, dict) and second_order.get("order_id"), second_order
        assert first_order["order_id"] == second_order["order_id"], "幂等重放拿到了不同订单号"
        assert second_order.get("idempotent_replay") is True, "第二次下单没有标记 idempotent_replay"
        assert first_order.get("verification_status") == "VERIFIED", (
            f"下单证据未升级为 VERIFIED：{first_order.get('verification_status')}"
        )
        item = OrderItemArgs(**order_args["items"][0])
        key = _idempotency_key(
            "smoke-buyer",
            [
                OrderItemInput(
                    product_id=item.product_id, sku_id=item.sku_id, quantity=item.quantity
                )
            ],
            idem_session,
        )
        persisted = await container.order_repo.find_by_idempotency_key(key)
        assert persisted is not None, "幂等键没有查到订单"
        assert persisted.order_id == first_order["order_id"], "幂等键对应的订单号不一致"
        verified_ids = await container.task_store.list_verified_order_ids(idem_trace)
        assert first_order["order_id"] in verified_ids, "任务证据里没有 VERIFIED 订单"
        print(
            "8. 幂等下单:",
            f"order_id={first_order['order_id']}",
            f"verification_status={first_order.get('verification_status')}",
            f"replay={second_order.get('idempotent_replay')}",
            f"按幂等键命中 1 张订单（verified_orders={len(verified_ids)}）",
        )

        # 9. P8：latency_report.py 真实运行（生成 data/latency-report.md 并校验聚合）
        project_root = Path(__file__).resolve().parents[1]
        report_path = project_root / "data" / "latency-report.md"
        child_env = dict(os.environ)
        child_env.setdefault("PYTHONIOENCODING", "utf-8")
        proc = subprocess.run(
            [
                sys.executable,
                str(project_root / "scripts" / "latency_report.py"),
                "--json",
                "--out",
                str(report_path),
            ],
            cwd=str(project_root),
            env=child_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        assert proc.returncode == 0, (
            f"latency_report.py 退出码 {proc.returncode}：{proc.stderr[-500:]}"
        )
        summary = json.loads(proc.stdout)
        assert summary["count"] == sum(s["count"] for s in summary["by_name"].values())
        assert report_path.is_file(), "latency-report.md 没有生成"
        assert "Latency Report" in report_path.read_text(encoding="utf-8")
        print(
            "9. 延迟报告:",
            f"{summary['count']} spans / {len(summary['by_name'])} 类；",
            f"request p95={summary['request']['p95']}ms ttft p95={summary['ttft']['p95']}ms；",
            f"report={report_path}",
        )
        print("P9/P8 冒烟通过：Redis 状态可降级 + 幂等下单只产生一张订单 + latency report 可运行")
    finally:
        if collector is not None:
            collector.cancel()
            with suppress(asyncio.CancelledError):
                await collector
        if context_token is not None:
            ShoppingContext.reset(context_token)
        await container.close()


if __name__ == "__main__":
    asyncio.run(main())
