"""Persistent session ownership, independent from chat checkpoints and trace files."""
from typing import Protocol


class SessionOwnershipStore(Protocol):
    async def claim(self, session_id: str, buyer_id: str) -> bool: ...

    async def owner(self, session_id: str) -> str | None: ...

    async def list_by_buyer(self, buyer_id: str) -> list[str]: ...
