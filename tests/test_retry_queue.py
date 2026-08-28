"""Tests for RetryQueue, RetryItem, and exponential backoff behavior."""

import asyncio
import heapq
from datetime import UTC, datetime, timedelta

import pytest

from msmtp.retry_queue import RetryConfig, RetryItem, RetryQueue, RetryStatus


def _past(seconds: float = 1.0) -> datetime:
    return datetime.now(UTC) - timedelta(seconds=seconds)


def _future(seconds: float = 60.0) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


class TestRetryItem:
    def test_ordering_by_next_retry_at(self):
        earlier = RetryItem(id="a", data={}, next_retry_at=_past(5))
        later = RetryItem(id="b", data={}, next_retry_at=_past(1))
        assert earlier < later

    def test_calculate_next_retry_grows_exponentially(self):
        item = RetryItem(id="a", data={})
        item.attempt = 0
        item.calculate_next_retry(base_delay=1.0, max_delay=300.0)
        delay0 = (item.next_retry_at - datetime.now(UTC)).total_seconds()

        item.attempt = 3
        item.calculate_next_retry(base_delay=1.0, max_delay=300.0)
        delay3 = (item.next_retry_at - datetime.now(UTC)).total_seconds()

        # attempt=3 -> base 8s (before jitter) vs attempt=0 -> base 1s (before jitter);
        # even with jitter (0.5x-1.5x) the higher attempt should almost always be larger.
        assert delay3 > delay0

    def test_calculate_next_retry_respects_max_delay(self):
        item = RetryItem(id="a", data={})
        item.attempt = 20  # Would be enormous without capping.
        item.calculate_next_retry(base_delay=1.0, max_delay=10.0)
        delay = (item.next_retry_at - datetime.now(UTC)).total_seconds()
        assert delay <= 15.0  # 10.0 * max jitter (1.5)

    def test_to_dict_roundtrip_fields(self):
        item = RetryItem(id="a", data={"x": 1}, last_error="boom")
        d = item.to_dict()
        assert d["id"] == "a"
        assert d["data"] == {"x": 1}
        assert d["last_error"] == "boom"
        assert d["status"] == RetryStatus.PENDING.value


@pytest.mark.asyncio
class TestRetryQueueAdd:
    async def test_add_new_item(self):
        queue = RetryQueue(config=RetryConfig(max_attempts=3))
        item = await queue.add("id-1", {"to": "a@example.com"})

        assert item.attempt == 0
        assert item.status == RetryStatus.PENDING
        assert queue.stats["total_added"] == 1
        assert "id-1" in queue._items

    async def test_add_existing_item_increments_attempt(self):
        queue = RetryQueue(config=RetryConfig(max_attempts=3))
        await queue.add("id-1", {"to": "a@example.com"})
        item = await queue.add("id-1", {"to": "a@example.com"}, error="timeout")

        assert item.attempt == 1
        assert item.last_error == "timeout"
        assert item.status == RetryStatus.PENDING
        assert queue.stats["total_retried"] == 1

    async def test_add_exhausts_after_max_attempts(self):
        queue = RetryQueue(config=RetryConfig(max_attempts=2))
        await queue.add("id-1", {})
        await queue.add("id-1", {}, error="err-1")
        item = await queue.add("id-1", {}, error="err-2")

        assert item.status == RetryStatus.EXHAUSTED
        assert queue.stats["total_exhausted"] == 1


@pytest.mark.asyncio
class TestGetReady:
    async def test_not_ready_before_next_retry_at(self):
        queue = RetryQueue()
        item = RetryItem(id="a", data={}, next_retry_at=_future())
        queue._items["a"] = item
        heapq.heappush(queue._queue, item)

        assert await queue.get_ready() == []

    async def test_ready_after_next_retry_at_marks_retrying(self):
        queue = RetryQueue()
        item = RetryItem(id="a", data={}, next_retry_at=_past())
        queue._items["a"] = item
        heapq.heappush(queue._queue, item)

        ready = await queue.get_ready()

        assert ready == [item]
        assert item.status == RetryStatus.RETRYING

    async def test_skips_duplicate_heap_entries_already_retrying(self):
        queue = RetryQueue()
        item = RetryItem(id="a", data={}, next_retry_at=_past())
        queue._items["a"] = item
        # Simulate the lazy-delete duplicate: same item pushed twice.
        heapq.heappush(queue._queue, item)
        heapq.heappush(queue._queue, item)

        ready = await queue.get_ready()

        assert len(ready) == 1

    async def test_skips_items_removed_from_items_dict(self):
        queue = RetryQueue()
        item = RetryItem(id="a", data={}, next_retry_at=_past())
        heapq.heappush(queue._queue, item)  # Note: not added to _items.

        assert await queue.get_ready() == []

    async def test_skips_exhausted_items(self):
        queue = RetryQueue()
        item = RetryItem(id="a", data={}, next_retry_at=_past(), status=RetryStatus.EXHAUSTED)
        queue._items["a"] = item
        heapq.heappush(queue._queue, item)

        assert await queue.get_ready() == []


@pytest.mark.asyncio
class TestMarkSuccessAndFailed:
    async def test_mark_success_removes_item(self):
        queue = RetryQueue()
        await queue.add("id-1", {})
        await queue.mark_success("id-1")

        assert "id-1" not in queue._items
        assert queue.stats["total_success"] == 1

    async def test_mark_success_on_unknown_id_is_noop(self):
        queue = RetryQueue()
        await queue.mark_success("does-not-exist")
        assert queue.stats["total_success"] == 0

    async def test_mark_failed_readds_item(self):
        queue = RetryQueue(config=RetryConfig(max_attempts=5))
        await queue.add("id-1", {"to": "a@example.com"})
        await queue.mark_failed("id-1", "smtp timeout")

        assert queue.stats["total_failed"] == 1
        item = queue._items["id-1"]
        assert item.attempt == 1
        assert item.last_error == "smtp timeout"

    async def test_mark_failed_on_unknown_id_is_noop(self):
        queue = RetryQueue()
        await queue.mark_failed("does-not-exist", "err")
        assert queue.stats["total_failed"] == 0


@pytest.mark.asyncio
class TestProcessLoop:
    async def test_successful_handler_marks_item_success(self):
        handler_calls = []

        async def handler(data):
            handler_calls.append(data)
            return True

        queue = RetryQueue(
            config=RetryConfig(base_delay=0.001, max_delay=0.001, process_interval=0.01),
            handler=handler,
        )
        await queue.add("id-1", {"to": "a@example.com"})

        await queue.start()
        try:
            for _ in range(50):
                await asyncio.sleep(0.02)
                if "id-1" not in queue._items:
                    break
        finally:
            await queue.stop()

        assert handler_calls == [{"to": "a@example.com"}]
        assert queue.stats["total_success"] == 1

    async def test_failing_handler_reschedules_item(self):
        async def handler(data):
            return False

        queue = RetryQueue(
            config=RetryConfig(
                base_delay=0.001, max_delay=0.001, process_interval=0.01, max_attempts=10
            ),
            handler=handler,
        )
        await queue.add("id-1", {})

        await queue.start()
        try:
            await asyncio.sleep(0.1)
        finally:
            await queue.stop()

        assert queue.stats["total_failed"] >= 1
        assert queue._items["id-1"].attempt >= 1

    async def test_handler_exception_marks_failed(self):
        async def handler(data):
            raise RuntimeError("boom")

        queue = RetryQueue(
            config=RetryConfig(
                base_delay=0.001, max_delay=0.001, process_interval=0.01, max_attempts=10
            ),
            handler=handler,
        )
        await queue.add("id-1", {})

        await queue.start()
        try:
            await asyncio.sleep(0.1)
        finally:
            await queue.stop()

        assert queue._items["id-1"].last_error == "boom"

    async def test_start_is_idempotent(self):
        queue = RetryQueue(config=RetryConfig(process_interval=1.0))
        await queue.start()
        task = queue._process_task
        await queue.start()  # Should not create a second task.
        assert queue._process_task is task
        await queue.stop()


@pytest.mark.asyncio
class TestPersistence:
    async def test_persist_and_reload_state(self, tmp_path):
        persist_path = str(tmp_path / "queue.json")
        queue = RetryQueue(persist_path=persist_path)
        await queue.add("id-1", {"to": "a@example.com"})

        reloaded = RetryQueue(persist_path=persist_path)

        assert "id-1" in reloaded._items
        assert reloaded._items["id-1"].data == {"to": "a@example.com"}

    async def test_reload_skips_completed_items(self, tmp_path):
        persist_path = str(tmp_path / "queue.json")
        queue = RetryQueue(persist_path=persist_path)
        await queue.add("id-1", {})
        await queue.mark_success("id-1")

        reloaded = RetryQueue(persist_path=persist_path)

        assert "id-1" not in reloaded._items

    async def test_load_missing_file_is_noop(self, tmp_path):
        persist_path = str(tmp_path / "does_not_exist.json")
        queue = RetryQueue(persist_path=persist_path)
        assert queue._items == {}


class TestStats:
    @pytest.mark.asyncio
    async def test_get_stats_reports_pending_and_queue_size(self):
        queue = RetryQueue()
        await queue.add("id-1", {})
        await queue.add("id-2", {})

        stats = queue.get_stats()

        assert stats["pending_count"] == 2
        assert stats["queue_size"] == 2
        assert stats["total_added"] == 2
