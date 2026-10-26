"""MySQL transactions use one shared session and InnoDB row locks."""

from __future__ import annotations

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.domain.order.ports.order_transaction import RetryableOrderTransactionError
from app.infrastructure.persistence.mysql_repositories import (
    MySqlOrderRepository,
    MySqlProductRepository,
)


class MySqlOrderTransactionFactory:
    def __init__(self, engine):
        self.engine = engine

    def __call__(self):
        return MySqlOrderTransaction(self.engine)


class MySqlOrderTransaction:
    def __init__(self, engine):
        # Fresh reads after waiting on stock locks must see newly committed replays.
        self._sessions = async_sessionmaker(
            engine.execution_options(isolation_level="READ COMMITTED"),
            expire_on_commit=False,
        )
        self._engine = engine

    async def __aenter__(self):
        self._session = self._sessions()
        try:
            await self._session.begin()
            self.products = MySqlProductRepository(self._engine, session=self._session)
            self.orders = MySqlOrderRepository(self._engine, session=self._session)
        except BaseException:
            await self._session.close()
            raise
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        try:
            if exc_type is None:
                try:
                    await self._session.commit()
                except BaseException as commit_error:
                    await self._session.rollback()
                    self._raise_retryable(commit_error)
                    raise
            else:
                await self._session.rollback()
                self._raise_retryable(exc)
        finally:
            await self._session.close()
        return False

    @staticmethod
    def _raise_retryable(error):
        if not isinstance(error, DBAPIError):
            return
        args = getattr(error.orig, "args", ())
        code = args[0] if args else None
        # Both failures guarantee rollback before the usecase retries. Other
        # integrity errors are real defects and must propagate without replay.
        if code in (1205, 1213) or (
            code == 1062 and "uq_orders_idempotency_key" in str(error.orig)
        ):
            raise RetryableOrderTransactionError(
                "Order transaction conflicted; retry with fresh state"
            ) from error
