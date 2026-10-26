"""Preference tools persist validated business facts under the current server actor."""
from datetime import datetime, timezone

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, StrictStr

from app.domain.buyer.preference import BuyerPreference, PreferenceStore
from app.domain.buyer.preference_facts import normalize_preference, safe_preference
from app.infrastructure.context import ShoppingContext


class RememberPreferenceArgs(BaseModel):
    """Explicit slots are preferred; recognized historical phrases remain supported."""

    model_config = ConfigDict(extra="forbid")
    kind: str = Field(description="like 或 dislike")
    statement: str = Field(default="", description="支持的偏好短句，如不要塑料材质、喜欢小众设计")
    attribute: str | None = Field(default=None, description="budget_max/material/category/brand/color/style/ship_to")
    value: StrictStr | StrictInt | StrictFloat | None = Field(default=None, description="商品属性值；预算为正数，其他为支持的商品属性")
    currency: str | None = Field(default=None, description="仅 budget_max 必须提供，如 CNY/USD/EUR")


class ForgetPreferenceArgs(BaseModel):
    """Deletion accepts exact old statements, including quarantined records."""

    model_config = ConfigDict(extra="forbid")
    statement: str = Field(max_length=255, description="要删除的偏好原文，必须完全一致")


def _actor():
    snapshot = ShoppingContext.current()
    if not snapshot or not snapshot.buyer_id.strip() or not snapshot.shopping_session_id.strip():
        return None
    if (
        snapshot.buyer_id != snapshot.buyer_id.strip()
        or snapshot.shopping_session_id != snapshot.shopping_session_id.strip()
    ):
        return None
    if snapshot.buyer_id == "anonymous" or snapshot.shopping_session_id == "anonymous":
        return None
    return snapshot


def build_preference_tools(store: PreferenceStore, broker=None):
    """Build remember/forget without exposing identity or provenance as tool args."""
    async def remember_preference_tool(
        kind: str, statement: str = "", attribute: str | None = None,
        value: str | int | float | None = None, currency: str | None = None,
    ) -> dict:
        snapshot = _actor()
        if snapshot is None:
            return {"error": "缺少买家或会话上下文"}
        try:
            attribute, value, currency, canonical = normalize_preference(
                kind, statement, attribute=attribute, value=value, currency=currency,
            )
            now = datetime.now(timezone.utc).isoformat()
            preference = BuyerPreference(
                buyer_id=snapshot.buyer_id, kind=kind, statement=canonical,
                attribute=attribute, value=value, currency=currency,
                source_session_id=snapshot.shopping_session_id, created_at=now, updated_at=now,
            )
            await store.append(preference)
        except ValueError as err:
            return {"error": str(err)}
        if broker:
            broker.publish(snapshot.shopping_session_id, "tool.result", {
                "tool": "remember_preference_tool", "saved": canonical,
            })
        return {"saved": f"[{kind}] {canonical}"}

    async def forget_preference_tool(statement: str) -> dict:
        snapshot = _actor()
        if snapshot is None:
            return {"error": "缺少买家或会话上下文"}
        deleted = await store.delete(snapshot.buyer_id, statement)
        # A recognized historical alias may render as canonical business text.
        # Resolve only exact canonical equality back to its original statement;
        # retain exact deletion for quarantined rows and never use fuzzy matching.
        existing = await store.list_by_buyer(snapshot.buyer_id)
        for original in existing:
            safe = safe_preference(original)
            if (
                original.buyer_id == snapshot.buyer_id and safe is not None
                and safe.statement == statement and original.statement != statement
            ):
                deleted = await store.delete(snapshot.buyer_id, original.statement) or deleted
        remaining = [safe_preference(p) for p in await store.list_by_buyer(snapshot.buyer_id)]
        rendered = [f"[{p.kind}] {p.statement}" for p in remaining if p is not None]
        if broker:
            broker.publish(snapshot.shopping_session_id, "tool.result", {
                "tool": "forget_preference_tool", "deleted": deleted, "remaining": rendered,
            })
        return {"deleted": deleted, "remaining": rendered}

    return [
        StructuredTool.from_function(
            name="remember_preference_tool",
            description="记住稳定的商品偏好，优先提供 attribute/value；budget_max 必须有 currency。支持材质、品类、品牌、颜色、风格和收货国家。",
            args_schema=RememberPreferenceArgs, coroutine=remember_preference_tool,
        ),
        StructuredTool.from_function(
            name="forget_preference_tool",
            description="撤回买家历史偏好；statement 必须与现有偏好原文完全一致。",
            args_schema=ForgetPreferenceArgs, coroutine=forget_preference_tool,
        ),
    ]
