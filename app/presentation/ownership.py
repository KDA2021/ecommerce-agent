"""Authorization checks used by every route accessing buyer-owned resources."""
import re

from fastapi import HTTPException, Request

from app.presentation.auth import principal
from app.presentation.dependencies import get_container


def _store(request: Request):
    store = getattr(get_container(request), "session_ownership", None)
    if store is None:
        raise HTTPException(status_code=503, detail="会话归属存储未就绪")
    return store


async def owned_sessions(request: Request) -> set[str] | None:
    user = principal(request)
    if user is None:
        return None
    return set(await _store(request).list_by_buyer(user.buyer_id))


async def authorize_session(request: Request, session_id: str, *, claim: bool = False) -> None:
    user = principal(request)
    if user is None:
        return
    # These IDs are also trace filenames: reject aliases (dots, colon, case-folding)
    # rather than authorizing one identifier and reading a different stored file.
    if not re.fullmatch(r"[a-z0-9][a-z0-9_\-]{0,190}", session_id):
        raise HTTPException(status_code=422, detail="会话编号仅支持小写字母、数字、下划线和短横线")
    store = _store(request)
    owner = await store.owner(session_id)
    if owner == user.buyer_id:
        return
    if owner is not None or not claim:
        raise HTTPException(status_code=404, detail="会话不存在")
    container = get_container(request)
    # Legacy chat histories require an explicit owner migration, never first-reader adoption.
    settings = getattr(container, "settings", None)
    data_dir = getattr(settings, "data_dir", None)
    if data_dir is not None and (data_dir / "traces" / f"{session_id}.jsonl").exists():
        raise HTTPException(status_code=409, detail="旧会话尚未迁移用户归属，请创建新会话")
    agent = getattr(container, "agent", None)
    if callable(getattr(agent, "aget_state", None)):
        snapshot = await agent.aget_state({"configurable": {"thread_id": session_id}})
        if getattr(snapshot, "values", {}).get("messages"):
            raise HTTPException(status_code=409, detail="旧会话尚未迁移用户归属，请创建新会话")
    if not await store.claim(session_id, user.buyer_id):
        raise HTTPException(status_code=404, detail="会话不存在")


async def authorize_order(request: Request, order_id: str) -> None:
    user = principal(request)
    if user is None:
        return
    repo = getattr(get_container(request), "order_repo", None)
    if repo is None:
        raise HTTPException(status_code=503, detail="订单存储未就绪")
    order = await repo.find_by_id(order_id)
    if order is None or order.buyer_id != user.buyer_id:
        raise HTTPException(status_code=404, detail="订单不存在")
