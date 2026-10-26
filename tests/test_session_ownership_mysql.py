"""Ownership persists across processes and cannot be hijacked by concurrent claims."""
import asyncio
import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.infrastructure.persistence.session_ownership import MySqlSessionOwnershipStore
from app.infrastructure.persistence.tables import Base


@pytest.fixture(name="engine")
async def ownership_engine():
    if not os.getenv("TEST_MYSQL_URL"):
        pytest.skip("TEST_MYSQL_URL not configured")
    url = make_url(os.environ["TEST_MYSQL_URL"])
    database = "commerce_owner_test_" + uuid.uuid4().hex
    admin = create_async_engine(url.set(database=None), isolation_level="AUTOCOMMIT")
    db_engine = None
    try:
        async with admin.connect() as connection:
            await connection.execute(text(f"CREATE DATABASE `{database}`"))
        db_engine = create_async_engine(url.set(database=database))
        async with db_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield db_engine
    finally:
        if db_engine is not None:
            await db_engine.dispose()
        async with admin.connect() as connection:
            await connection.execute(text(f"DROP DATABASE IF EXISTS `{database}`"))
        await admin.dispose()


async def test_concurrent_claim_has_exactly_one_owner(engine):
    first, second = MySqlSessionOwnershipStore(engine), MySqlSessionOwnershipStore(engine)
    results = await asyncio.gather(first.claim("shared", "alice"), second.claim("shared", "bob"))
    assert sorted(results) == [False, True]
    owner = await first.owner("shared")
    assert owner in {"alice", "bob"}
    assert await second.owner("shared") == owner
    assert await first.list_by_buyer(owner) == ["shared"]
    foreign = "bob" if owner == "alice" else "alice"
    assert await second.list_by_buyer(foreign) == []
    assert not await second.claim("shared", foreign)


async def test_ownership_survives_recreating_store(engine):
    await MySqlSessionOwnershipStore(engine).claim("durable", "alice")
    assert await MySqlSessionOwnershipStore(engine).owner("durable") == "alice"


async def test_buyer_identity_comparison_is_case_and_accent_sensitive(engine):
    from app.domain.buyer.preference import BuyerPreference
    from app.infrastructure.persistence.mysql_preference_store import MySqlPreferenceStore

    ownership = MySqlSessionOwnershipStore(engine)
    preferences = MySqlPreferenceStore(engine)
    buyers = ["alice", "ALICE", "álîce"]
    for index, buyer in enumerate(buyers):
        await ownership.claim(f"session-{index}", buyer)
        await preferences.append(BuyerPreference(buyer, "like", ["喜欢黑色", "喜欢白色", "喜欢蓝色"][index], attribute="color", value=["黑色", "白色", "蓝色"][index], source_session_id=f"session-{index}"))
    for index, buyer in enumerate(buyers):
        assert await ownership.list_by_buyer(buyer) == [f"session-{index}"]
        assert [p.statement for p in await preferences.list_by_buyer(buyer)] == [["喜欢黑色", "喜欢白色", "喜欢蓝色"][index]]
    assert not await preferences.delete("alice", "喜欢白色")


async def test_feedback_and_metrics_queries_use_exact_session_ids(engine):
    from app.application.metrics.session_metrics import SessionMetrics
    from app.domain.feedback.feedback import UserFeedback
    from app.infrastructure.persistence.mysql_feedback_store import MySqlFeedbackStore
    from app.infrastructure.persistence.mysql_metrics_store import MySqlMetricsStore

    feedback = MySqlFeedbackStore(engine)
    metrics = MySqlMetricsStore(engine)
    for sid in ("case-session", "CASE-SESSION"):
        await feedback.add(UserFeedback(session_id=sid, buyer_id=sid, score=1, query=sid))
        await metrics.add(SessionMetrics(session_id=sid))
    assert [item.query for item in await feedback.list_by_session("case-session")] == ["case-session"]
    assert [item.session_id for item in await metrics.list_by_session("case-session")] == ["case-session"]
