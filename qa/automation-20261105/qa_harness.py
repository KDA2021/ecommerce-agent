"""QA-only assembly: real routers/tools/SSE/business; scripted model + memory data.

Never imported by production. No model client/container bootstrap is constructed.
"""

import json
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

ALICE_KEY = "qa-alice-credential-20261105"
BOB_KEY = "qa-bob-credential-20261105"
CONTROL = "qa-local-control-20261105"
ADDRESS = {
    "recipient_name": "QA Buyer",
    "country": "CN",
    "city": "Guangzhou",
    "address_line": "QA Road",
}


def make_container(data_dir, timeout=8):
    from app.application.evidence.task_state import InMemoryTaskStateStore
    from app.application.order_confirmation import OrderConfirmationManager
    from app.application.tools.order_tools import build_order_tools
    from app.application.usecases.order_usecases import (
        CancelOrderUseCase,
        PlaceOrderUseCase,
        QueryOrderUseCase,
    )
    from app.infrastructure.eventbroker import EventBroker
    from app.infrastructure.persistence.in_memory_repositories import (
        InMemoryOrderRepository,
        InMemoryProductRepository,
    )
    from app.infrastructure.persistence.session_ownership import InMemorySessionOwnershipStore

    class FaultOrders(InMemoryOrderRepository):
        fail_next = False

        async def save(self, order):
            await super().save(order)
            if self.fail_next:
                self.fail_next = False
                raise RuntimeError("QA injected write failure")

    class NoCache:
        async def lookup(self, *args, **kwargs):
            return None

        async def remember(self, *args, **kwargs):
            return None

    class NoHistory:
        async def aget_state(self, config):
            return SimpleNamespace(values={"messages": []})

    class Container:
        async def close(self):
            await self.broker.aclose()

    c = Container()
    c.settings = SimpleNamespace(
        api_key="",
        buyer_api_keys_json=json.dumps({"alice": ALICE_KEY, "bob": BOB_KEY}),
        auth_cookie_secure=False,
        data_dir=Path(data_dir),
        order_confirm_timeout_seconds=timeout,
    )
    c.products = InMemoryProductRepository()
    c.order_repo = FaultOrders()
    c.place = PlaceOrderUseCase(c.products, c.order_repo)
    c.query_order = QueryOrderUseCase(c.order_repo)
    c.cancel_order = CancelOrderUseCase(c.products, c.order_repo)
    c.broker = EventBroker()
    c.confirmation_manager = OrderConfirmationManager()
    c.task_store = InMemoryTaskStateStore()
    c.session_ownership = InMemorySessionOwnershipStore()
    c.semantic_cache, c.agent = NoCache(), NoHistory()
    c.tools = {
        t.name: t
        for t in build_order_tools(
            c.place,
            c.query_order,
            c.cancel_order,
            broker=c.broker,
            task_store=c.task_store,
            confirmation_manager=c.confirmation_manager,
            settings=c.settings,
        )
    }
    return c


def create_qa_app(data_dir, timeout=8):
    from fastapi import HTTPException, Request

    from app.presentation.routers import intents
    from app.presentation.server import create_app

    app = create_app(container=make_container(data_dir, timeout), build_on_startup=False)

    async def scripted_invoke(agent, session_id, query, broker, **kwargs):
        # Only model orchestration is replaced; actual tool requires confirmation.
        c = app.state.container
        if query == "qa:create":
            result = await c.tools["create_order"].ainvoke(
                {
                    "items": [{"product_id": "P1001", "sku_id": "P1001-S1", "quantity": 1}],
                    "shipping_address": ADDRESS,
                }
            )
        elif query.startswith("qa:cancel:"):
            result = await c.tools["cancel_order"].ainvoke(
                {"order_id": query.split(":", 2)[2], "reason": "QA cancel"}
            )
        else:
            raise ValueError("QA driver accepts only explicit qa:create / qa:cancel commands")
        return json.dumps(result, ensure_ascii=False)

    # Override only the lookup used by the QA app, restore on lifespan shutdown.
    original = intents.invoke_agent_stream
    lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def qa_lifespan(instance):
        intents.invoke_agent_stream = scripted_invoke
        try:
            async with lifespan(instance):
                yield
        finally:
            intents.invoke_agent_stream = original

    app.router.lifespan_context = qa_lifespan

    def authorize(request):
        if request.headers.get("X-QA-Control") != CONTROL:
            raise HTTPException(403, "QA control credential required")

    @app.get("/__qa/state")
    async def state(request: Request):
        authorize(request)
        c = app.state.container
        p = await c.products.find_by_id("P1001")
        return {
            "orders": [o.snapshot() for o in c.order_repo._orders.values()],
            "stock": p.primary_sku().stock,
            "subscribers": sum(len(q) for q in c.broker._queues.values()),
        }

    @app.post("/__qa/reset")
    async def reset(request: Request):
        authorize(request)
        await app.state.container.close()
        app.state.container = make_container(data_dir, timeout)
        return {"ok": True}

    @app.post("/__qa/fault")
    async def fault(request: Request):
        authorize(request)
        app.state.container.order_repo.fail_next = True
        return {"ok": True}

    return app


if __name__ == "__main__":
    import sys

    import uvicorn

    # Script invocation requires project root on PYTHONPATH; local temporary data.
    with tempfile.TemporaryDirectory(prefix="commerce-qa-") as folder:
        uvicorn.run(
            create_qa_app(folder),
            host="127.0.0.1",
            port=int(sys.argv[1]) if len(sys.argv) > 1 else 8009,
            log_level="warning",
            access_log=False,
            timeout_graceful_shutdown=3,
        )
