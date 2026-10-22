# -*- coding: utf-8 -*-
from app.application.tools.preference_tools import build_preference_tools
from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot


class _FakeStore:
    def __init__(self) -> None:
        self.items = []

    async def append(self, preference) -> None:
        self.items.append(preference)

    async def list_by_buyer(self, buyer_id: str) -> list:
        return [p for p in self.items if p.buyer_id == buyer_id]

    async def delete(self, buyer_id: str, statement: str) -> bool:
        before = len(self.items)
        self.items = [
            p for p in self.items
            if not (p.buyer_id == buyer_id and p.statement == statement)
        ]
        return len(self.items) != before


class _Broker:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    def publish(self, session_id: str, event_type: str, payload: dict) -> None:
        self.events.append((session_id, event_type, payload))


def _tool(tools, name):
    return next(tool for tool in tools if tool.name == name)


async def test_remember_preference_uses_buyer_from_shopping_context():
    store = _FakeStore()
    remember = _tool(build_preference_tools(store), "remember_preference_tool")
    token = ShoppingContext.set(
        ShoppingContextSnapshot(
            shopping_session_id="s-pref",
            buyer_id="buyer-9",
            locale="zh-CN",
            currency="CNY",
        )
    )
    try:
        result = await remember.ainvoke(
            {"kind": "dislike", "statement": "不要塑料材质"}
        )
    finally:
        ShoppingContext.reset(token)

    assert result == {"saved": "[dislike] 不要塑料材质"}
    assert len(store.items) == 1
    assert store.items[0].buyer_id == "buyer-9"
    assert store.items[0].kind == "dislike"
    assert store.items[0].statement == "不要塑料材质"


async def test_forget_preference_deletes_and_returns_remaining():
    store = _FakeStore()
    broker = _Broker()
    tools = build_preference_tools(store, broker=broker)
    remember = _tool(tools, "remember_preference_tool")
    forget = _tool(tools, "forget_preference_tool")
    token = ShoppingContext.set(
        ShoppingContextSnapshot(
            shopping_session_id="s-pref",
            buyer_id="buyer-9",
            locale="zh-CN",
            currency="CNY",
        )
    )
    try:
        await remember.ainvoke({"kind": "dislike", "statement": "不要塑料材质"})
        await remember.ainvoke({"kind": "like", "statement": "喜欢小众设计"})
        result = await forget.ainvoke({"statement": "不要塑料材质"})
    finally:
        ShoppingContext.reset(token)

    assert result == {"deleted": True, "remaining": ["[like] 喜欢小众设计"]}
    assert [p.statement for p in store.items] == ["喜欢小众设计"]
    assert broker.events[-1] == (
        "s-pref",
        "tool.result",
        {
            "tool": "forget_preference_tool",
            "deleted": True,
            "remaining": ["[like] 喜欢小众设计"],
        },
    )
