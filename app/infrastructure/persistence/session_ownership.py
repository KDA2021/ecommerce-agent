"""Atomic, persistent ownership binding; an existing session cannot change owners."""
import asyncio

from sqlalchemy import LargeBinary, cast, select
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.infrastructure.persistence.tables import ShoppingSessionRow


class MySqlSessionOwnershipStore:
    def __init__(self, engine):
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def claim(self, session_id: str, buyer_id: str) -> bool:
        async with self._sessions.begin() as db:
            stmt = insert(ShoppingSessionRow).values(session_id=session_id, buyer_id=buyer_id)
            await db.execute(stmt.on_duplicate_key_update(session_id=session_id))
            owner = await db.scalar(select(ShoppingSessionRow.buyer_id).where(
                ShoppingSessionRow.session_id == session_id,
            ))
            return owner == buyer_id

    async def owner(self, session_id: str) -> str | None:
        async with self._sessions() as db:
            return await db.scalar(select(ShoppingSessionRow.buyer_id).where(
                ShoppingSessionRow.session_id == session_id,
            ))

    async def list_by_buyer(self, buyer_id: str) -> list[str]:
        async with self._sessions() as db:
            return list(await db.scalars(select(ShoppingSessionRow.session_id).where(
                cast(ShoppingSessionRow.buyer_id, LargeBinary) == buyer_id.encode("utf-8"),
            )))


class InMemorySessionOwnershipStore:
    def __init__(self):
        self._owners: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def claim(self, session_id: str, buyer_id: str) -> bool:
        async with self._lock:
            self._owners.setdefault(session_id, buyer_id)
            return self._owners[session_id] == buyer_id

    async def owner(self, session_id: str) -> str | None:
        return self._owners.get(session_id)

    async def list_by_buyer(self, buyer_id: str) -> list[str]:
        return [sid for sid, owner in self._owners.items() if owner == buyer_id]
