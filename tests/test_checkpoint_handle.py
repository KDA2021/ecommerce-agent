# -*- coding: utf-8 -*-
import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_MYSQL_URL"),
    reason="TEST_MYSQL_URL 未设置，跳过 MySQL checkpointer 测试",
)


async def test_checkpointer_setup_and_close():
    from app.infrastructure.checkpoint import build_checkpointer

    url = os.environ["TEST_MYSQL_URL"]
    handle = await build_checkpointer(url)
    assert handle.saver is not None
    await handle.close()
