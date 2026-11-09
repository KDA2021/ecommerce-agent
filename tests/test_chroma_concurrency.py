"""Bounded Chroma admission, without a model or persistent database."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from app.application.usecases.catalog_search import CatalogSearchUseCase
from app.infrastructure.persistence.in_memory_repositories import InMemoryProductRepository
from app.infrastructure.vector.chroma_product_index import ChromaProductIndex


class BlockingCollection:
    def __init__(self):
        self.started = [threading.Event() for _ in range(6)]
        self.release = [threading.Event() for _ in range(6)]
        self.calls = []

    def query(self, query_embeddings, **kwargs):
        number = int(query_embeddings[0][0])
        self.calls.append(number)
        self.started[number].set()
        if not self.release[number].wait(3):
            raise RuntimeError("test worker was not released")
        return {"ids": [[]], "metadatas": [[]], "distances": [[]]}


def make_index(collection, **overrides):
    values = dict(chroma_timeout_seconds=1, chroma_max_concurrency=2,
                  chroma_max_queue=1, chroma_queue_timeout_seconds=0.5)
    values.update(overrides)
    index = ChromaProductIndex(SimpleNamespace(**values))
    index._collection = collection
    return index


async def started(event):
    assert await asyncio.to_thread(event.wait, 1), "worker did not start"


async def cleanup(collection, tasks):
    for event in collection.release:
        event.set()
    await asyncio.gather(*tasks, return_exceptions=True)


async def test_two_queries_run_while_third_waits_and_fourth_is_rejected():
    collection = BlockingCollection()
    index = make_index(collection)
    tasks = [asyncio.create_task(index.search([n], 1)) for n in (0, 1)]
    try:
        await started(collection.started[0])
        await started(collection.started[1])
        tasks.append(asyncio.create_task(index.search([2], 1)))
        await asyncio.sleep(0)
        assert not collection.started[2].is_set()
        with pytest.raises(RuntimeError, match="queue.*full"):
            await index.search([3], 1)
        collection.release[0].set()
        await started(collection.started[2])
        assert not collection.started[3].is_set()
        assert not collection.release[1].is_set()
    finally:
        await cleanup(collection, tasks)


async def test_queue_timeout_and_cancellation_never_start_abandoned_work():
    collection = BlockingCollection()
    index = make_index(collection, chroma_max_concurrency=1,
                       chroma_queue_timeout_seconds=0.03)
    first = asyncio.create_task(index.search([0], 1))
    tasks = [first]
    try:
        await started(collection.started[0])
        with pytest.raises(TimeoutError):
            await index.search([1], 1)
        waiting = asyncio.create_task(index.search([2], 1))
        tasks.append(waiting)
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        collection.release[0].set()
        await first
        collection.release[3].set()
        await index.search([3], 1)
        assert collection.calls == [0, 3]
    finally:
        await cleanup(collection, tasks)


async def test_execution_timeout_keeps_capacity_occupied_until_native_work_finishes():
    collection = BlockingCollection()
    index = make_index(collection, chroma_timeout_seconds=0.03,
                       chroma_max_queue=0)
    tasks = [asyncio.create_task(index.search([n], 1)) for n in (0, 1)]
    try:
        await started(collection.started[0])
        await started(collection.started[1])
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert all(isinstance(result, TimeoutError) for result in results)
        with pytest.raises(RuntimeError):
            await index.search([2], 1)
        collection.release[0].set()
        collection.release[1].set()
        await asyncio.gather(*tuple(index._pending), return_exceptions=True)
        collection.release[2].set()
        await index.search([2], 1)
    finally:
        # Unblock every native operation before draining the internal tasks.
        await cleanup(collection, tasks)


async def test_concurrent_lazy_initialization_only_creates_one_client(monkeypatch):
    from app.infrastructure.vector import chroma_product_index as module

    collection = BlockingCollection()
    initialized = threading.Event()
    finish_initialization = threading.Event()
    clients = []

    class Client:
        def get_or_create_collection(self, **kwargs):
            return collection

    def factory(**kwargs):
        clients.append(Client())
        initialized.set()
        assert finish_initialization.wait(2)
        return clients[-1]

    monkeypatch.setattr(module.chromadb, "PersistentClient", factory)
    index = make_index(None, chroma_dir="unused")
    tasks = [asyncio.create_task(index.search([n], 1)) for n in (0, 1)]
    try:
        await started(initialized)
        await asyncio.sleep(0.02)
        finish_initialization.set()
        await started(collection.started[0])
        await started(collection.started[1])
        assert len(clients) == 1
    finally:
        finish_initialization.set()
        await cleanup(collection, tasks)
        if isinstance(index._pending, set):
            await asyncio.gather(*tuple(index._pending), return_exceptions=True)


async def test_write_is_exclusive_and_fifo_prevents_readers_bypassing_it():
    collection = BlockingCollection()
    index = make_index(collection, chroma_max_queue=2)

    def upsert(**kwargs):
        collection.started[1].set()
        assert collection.release[1].wait(3)

    collection.upsert = upsert
    product = SimpleNamespace(product_id="TEST")
    tasks = [asyncio.create_task(index.search([0], 1))]
    try:
        await started(collection.started[0])
        tasks.append(asyncio.create_task(index.upsert_products([product], [[1.0]])))
        await asyncio.sleep(0)
        tasks.append(asyncio.create_task(index.search([2], 1)))
        await asyncio.sleep(0)
        assert not collection.started[1].is_set()
        assert not collection.started[2].is_set()
        collection.release[0].set()
        await started(collection.started[1])
        assert not collection.started[2].is_set()
        collection.release[1].set()
        await started(collection.started[2])
    finally:
        await cleanup(collection, tasks)


async def test_cancellation_during_capacity_handoff_returns_the_reserved_slot():
    collection = BlockingCollection()
    index = make_index(collection, chroma_max_concurrency=1)
    first = asyncio.create_task(index.search([0], 1))
    tasks = [first]
    try:
        await started(collection.started[0])
        waiting = asyncio.create_task(index.search([1], 1))
        tasks.append(waiting)
        await asyncio.sleep(0)
        # _finished grants the slot first. This next callback cancels the caller
        # before it can resume from admission and submit a native query.
        next(iter(index._pending)).add_done_callback(lambda _: waiting.cancel())
        collection.release[0].set()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        await first
        collection.release[2].set()
        await index.search([2], 1)
        assert collection.calls == [0, 2]
    finally:
        await cleanup(collection, tasks)


async def test_cancelling_a_queued_writer_unblocks_readers_behind_it():
    collection = BlockingCollection()
    index = make_index(collection, chroma_max_queue=2)
    first = asyncio.create_task(index.search([0], 1))
    tasks = [first]
    try:
        await started(collection.started[0])
        writer = asyncio.create_task(index.ensure_ready(1))
        tasks.append(writer)
        await asyncio.sleep(0)
        tasks.append(asyncio.create_task(index.search([1], 1)))
        await asyncio.sleep(0)
        assert not collection.started[1].is_set()
        writer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await writer
        await started(collection.started[1])
        assert not collection.release[0].is_set()
    finally:
        await cleanup(collection, tasks)


@pytest.mark.parametrize("queue_size", [0, 1])
async def test_queue_full_or_deadline_uses_existing_keyword_fallback(queue_size):
    collection = BlockingCollection()
    index = make_index(collection, chroma_max_concurrency=1,
                       chroma_max_queue=queue_size, chroma_queue_timeout_seconds=0.02)

    class Embedder:
        async def embed(self, text):
            return [1.0]

    search = CatalogSearchUseCase(InMemoryProductRepository([]), Embedder(), index)
    first = asyncio.create_task(index.search([0], 1))
    try:
        await started(collection.started[0])
        from app.domain.catalog.product_search_spec import ProductSearchSpec

        result = await search.execute(ProductSearchSpec(normalized_query="headphones"))
        assert result["recall_strategy"] == "keyword_2gram"
        assert result["hits"] == []
        assert collection.calls == [0]
    finally:
        await cleanup(collection, [first])


async def test_native_failure_frees_slot_for_the_next_request():
    collection = BlockingCollection()
    index = make_index(collection, chroma_max_concurrency=1)
    original_query = collection.query

    def query(**kwargs):
        result = original_query(**kwargs)
        if kwargs["query_embeddings"][0][0] == 0:
            raise RuntimeError("injected failure")
        return result

    collection.query = query
    first = asyncio.create_task(index.search([0], 1))
    tasks = [first]
    try:
        await started(collection.started[0])
        tasks.append(asyncio.create_task(index.search([1], 1)))
        await asyncio.sleep(0)
        collection.release[0].set()
        with pytest.raises(RuntimeError, match="injected failure"):
            await first
        await started(collection.started[1])
    finally:
        await cleanup(collection, tasks)


def test_queue_configuration_environment(monkeypatch):
    from app.infrastructure.settings import load_settings

    monkeypatch.setenv("CHROMA_MAX_CONCURRENCY", "3")
    monkeypatch.setenv("CHROMA_MAX_QUEUE", "7")
    monkeypatch.setenv("CHROMA_QUEUE_TIMEOUT_SECONDS", "0.4")
    settings = load_settings()
    assert settings.chroma_max_concurrency == 3
    assert settings.chroma_max_queue == 7
    assert settings.chroma_queue_timeout_seconds == 0.4


@pytest.mark.parametrize("field,value", [
    ("chroma_max_concurrency", 0), ("chroma_max_concurrency", -1),
    ("chroma_max_concurrency", 1.5), ("chroma_max_concurrency", True),
    ("chroma_max_queue", -1), ("chroma_max_queue", 1.5),
    ("chroma_max_queue", True), ("chroma_queue_timeout_seconds", 0),
    ("chroma_queue_timeout_seconds", float("nan")),
    ("chroma_queue_timeout_seconds", float("inf")),
])
def test_invalid_admission_configuration(field, value):
    with pytest.raises(ValueError):
        make_index(BlockingCollection(), **{field: value})
