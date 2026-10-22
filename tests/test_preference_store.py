# -*- coding: utf-8 -*-
import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_MYSQL_URL"), reason="TEST_MYSQL_URL 未设置，跳过 MySQL 偏好测试"
)


@pytest.fixture()
async def store():
    from app.infrastructure.persistence.db import bootstrap_schema, create_engine
    from app.infrastructure.persistence.mysql_preference_store import MySqlPreferenceStore

    engine = create_engine(os.environ["TEST_MYSQL_URL"])
    await bootstrap_schema(engine)
    s = MySqlPreferenceStore(engine)
    yield s
    await engine.dispose()


async def test_preference_idempotent_and_delete(store):
    from app.domain.buyer.preference import BuyerPreference

    p = BuyerPreference(buyer_id="buyer-p", kind="dislike", statement="不要塑料材质")
    await store.append(p)
    await store.append(p)
    rows = await store.list_by_buyer("buyer-p")
    assert [r.statement for r in rows].count("不要塑料材质") == 1

    assert await store.delete("buyer-p", "不要塑料材质") is True
    assert await store.list_by_buyer("buyer-p") == []
