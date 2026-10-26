"""Exchange a personal access credential for an HttpOnly browser session."""
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.presentation.auth import (
    SESSION_COOKIE,
    SESSION_TTL_SECONDS,
    authenticated_buyer,
    buyer_for_key,
    issue_session,
    resolve_api_key,
    resolve_buyer_keys,
)
from app.presentation.dependencies import get_container


class SessionLogin(BaseModel):
    api_key: str = Field(min_length=1, max_length=4096)


def _state(request: Request, buyer: str | None = None) -> dict:
    container = get_container(request)
    keys = resolve_buyer_keys(container)
    return {
        "auth_required": bool(keys or resolve_api_key(container)),
        "identity_mode": bool(keys),
        "buyer_id": buyer,
    }


def build_auth_session_router() -> APIRouter:
    router = APIRouter()

    @router.get("/auth/session")
    async def session(request: Request, response: Response) -> dict:
        response.headers["Cache-Control"] = "no-store"
        return _state(request, authenticated_buyer(request, get_container(request)))

    @router.post("/auth/session")
    async def login(body: SessionLogin, request: Request, response: Response) -> dict:
        container = get_container(request)
        buyer = buyer_for_key(container, body.api_key)
        if buyer is None:
            raise HTTPException(status_code=401, detail="访问凭证无效")
        secure = bool(getattr(getattr(container, "settings", None), "auth_cookie_secure", False))
        response.set_cookie(
            SESSION_COOKIE, issue_session(container, buyer), max_age=SESSION_TTL_SECONDS,
            httponly=True, secure=secure, samesite="strict", path="/",
        )
        response.headers["Cache-Control"] = "no-store"
        return _state(request, buyer)

    @router.delete("/auth/session")
    async def logout(request: Request, response: Response) -> dict:
        response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="strict")
        response.headers["Cache-Control"] = "no-store"
        return _state(request)

    return router
