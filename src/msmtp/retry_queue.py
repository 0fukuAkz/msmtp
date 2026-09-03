"""Retry queue with exponential backoff."""

import asyncio
import heapq
import json
import logging
import os
import random
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class RetryStatus(Enum):
    PENDING = "pending"
    RETRYING = "retrying"
    SUCCESS = "success"
    FAILED = "failed"
    EXHAUSTED = "exhausted"


@dataclass
class RetryItem:
    """Item in retry queue."""

    id: str
    data: dict[str, Any]
    attempt: int = 0
    max_attempts: int = 3
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    next_retry_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_error: str | None = None
    status: RetryStatus = RetryStatus.PENDING

    def __lt__(self, other: "RetryItem") -> bool:
        return self.next_retry_at < other.next_retry_at

    def calculate_next_retry(self, base_delay: float = 1.0, max_delay: float = 300.0) -> None:
        """Calculate next retry time with exponential backoff."""
        delay = min(base_delay * (2**self.attempt), max_delay)
        delay *= 0.5 + random.random()
        self.next_retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "data": self.data,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "created_at": self.created_at.isoformat(),
            "next_retry_at": self.next_retry_at.isoformat(),
            "last_error": self.last_error,
            "status": self.status.value,
        }


@dataclass
class RetryConfig:
    """Retry queue configuration."""

    max_attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 300.0
    concurrency: int = 10
    process_interval: float = 1.0


class RetryQueue:
    """Async retry queue with exponential backoff."""

    def __init__(
        self,
        config: RetryConfig | None = None,
        handler: Callable[[dict[str, Any]], Awaitable[bool]] | None = None,
        persist_path: str | None = None,
    ):
        """
        Initialize retry queue.

        Args:
            config: Retry configuration
            handler: Async function to process retries
            persist_path: Path to persist queue state
        """
        self.config = config or RetryConfig()
        self.handler = handler
        self.persist_path = persist_path

        self._queue: list[tuple[datetime, str]] = []  # (next_retry_at, item_id)
        self._items: dict[str, RetryItem] = {}
        self._lock = asyncio.Lock()
        self._running = False
        self._process_task: asyncio.Task[None] | None = None

        # Statistics
        self.stats = {
            "total_added": 0,
            "total_retried": 0,
            "total_success": 0,
            "total_failed": 0,
            "total_exhausted": 0,
        }

        # Load persisted state
        if persist_path:
            self._load_state()

    async def add(self, id: str, data: dict[str, Any], error: str | None = None) -> RetryItem:
        """Add item to retry queue."""
        async with self._lock:
            if id in self._items:
                item = self._items[id]
                item.attempt += 1
                item.last_error = error

                if item.attempt >= item.max_attempts:
                    item.status = RetryStatus.EXHAUSTED
                    self.stats["total_exhausted"] += 1
                    logger.warning(f"Retry exhausted for {id} after {item.attempt} attempts")
                else:
                    item.calculate_next_retry(self.config.base_delay, self.config.max_delay)
                    # Push a (scheduled_time, id) tuple. get_ready() skips any entry
                    # whose timestamp no longer matches the item's current next_retry_at,
                    # so each reschedule effectively invalidates the previous heap entry
                    # without an O(n) remove. Heap size is bounded by unique live items.
                    heapq.heappush(self._queue, (item.next_retry_at, item.id))
                    self.stats["total_retried"] += 1
            else:
                item = RetryItem(
                    id=id, data=data, max_attempts=self.config.max_attempts, last_error=error
                )
                item.calculate_next_retry(self.config.base_delay, self.config.max_delay)
                self._items[id] = item
                heapq.heappush(self._queue, (item.next_retry_at, item.id))
                self.stats["total_added"] += 1

        # Persist outside the lock so asyncio.to_thread doesn't block queue access
        await self._persist_state()
        return item

    async def get_ready(self) -> list[RetryItem]:
        """Get items ready for retry."""
        now = datetime.now(timezone.utc)
        ready = []

        async with self._lock:
            while self._queue:
                if self._queue[0][0] > now:
                    break

                scheduled_at, item_id = heapq.heappop(self._queue)

                item = self._items.get(item_id)
                if item is None:
                    continue  # Already succeeded or exhausted

                # Stale heap entry: item was rescheduled after this entry was pushed.
                # The current next_retry_at won't match, so skip and let the newer
                # entry fire when its time comes.
                if item.next_retry_at != scheduled_at:
                    continue

                if item.status in (RetryStatus.EXHAUSTED, RetryStatus.RETRYING):
                    continue

                item.status = RetryStatus.RETRYING
                ready.append(item)

        return ready

    async def mark_success(self, id: str) -> None:
        """Mark item as successfully processed."""
        async with self._lock:
            if id not in self._items:
                return
            self._items[id].status = RetryStatus.SUCCESS
            self.stats["total_success"] += 1
            del self._items[id]
        # Persist outside the lock (asyncio.to_thread must not hold _lock)
        await self._persist_state()

    async def mark_failed(self, id: str, error: str) -> None:
        """Mark item as failed, will be retried."""
        async with self._lock:
            item = self._items.get(id)
            if not item:
                return

            item.last_error = error
            item.status = RetryStatus.FAILED
            self.stats["total_failed"] += 1
            data = item.data

        # Re-add to queue
        await self.add(id, data, error)

    async def start(self) -> None:
        """Start processing retry queue."""
        if self._running:
            return

        self._running = True
        self._process_task = asyncio.create_task(self._process_loop())
        logger.info("Retry queue started")

    async def stop(self) -> None:
        """Stop processing retry queue."""
        self._running = False
        if self._process_task:
            self._process_task.cancel()
            try:
                await self._process_task
            except asyncio.CancelledError:
                pass

        await self._persist_state()
        logger.info("Retry queue stopped")

    async def _process_loop(self) -> None:
        """Main processing loop."""
        semaphore = asyncio.Semaphore(max(1, self.config.concurrency))

        while self._running:
            try:
                ready_items = await self.get_ready()

                if ready_items and self.handler:

                    async def process_item(item: RetryItem) -> None:
                        async with semaphore:
                            try:
                                if self.handler is None:
                                    return
                                success = await self.handler(item.data)
                                if success:
                                    await self.mark_success(item.id)
                                else:
                                    await self.mark_failed(item.id, "Handler returned False")
                            except Exception as e:
                                await self.mark_failed(item.id, str(e))

                    await asyncio.gather(
                        *[process_item(item) for item in ready_items], return_exceptions=True
                    )

                await asyncio.sleep(self.config.process_interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in retry queue: {e}")
                await asyncio.sleep(self.config.process_interval)

    async def _persist_state(self) -> None:
        """Persist queue state to disk asynchronously."""
        if not self.persist_path:
            return

        try:
            state = {"items": {k: v.to_dict() for k, v in self._items.items()}, "stats": self.stats}

            # FIX: Run file I/O in thread pool to avoid blocking event loop
            await asyncio.to_thread(self._write_state_to_disk, state)

        except Exception as e:
            logger.error(f"Failed to persist retry queue: {e}")

    def _write_state_to_disk(self, state: dict[str, Any]) -> None:
        """Synchronous write helper for to_thread."""
        try:
            # Atomic write pattern (write temp then rename)
            temp_path = f"{self.persist_path}.tmp"
            with open(temp_path, "w") as f:
                json.dump(state, f)
            shutil.move(temp_path, self.persist_path or "")
        except Exception as e:
            logger.error(f"Disk write error in retry queue: {e}")
            raise

    def _load_state(self) -> None:
        """Load persisted state."""
        if not self.persist_path:
            return

        try:
            if not os.path.exists(self.persist_path):
                return

            with open(self.persist_path) as f:
                state = json.load(f)

            for item_data in state.get("items", {}).values():
                item = RetryItem(
                    id=item_data["id"],
                    data=item_data["data"],
                    attempt=item_data["attempt"],
                    max_attempts=item_data["max_attempts"],
                    last_error=item_data.get("last_error"),
                    status=RetryStatus(item_data["status"]),
                )
                item.created_at = datetime.fromisoformat(item_data["created_at"])
                item.next_retry_at = datetime.fromisoformat(item_data["next_retry_at"])

                if item.status not in [RetryStatus.SUCCESS, RetryStatus.EXHAUSTED]:
                    self._items[item.id] = item
                    heapq.heappush(self._queue, (item.next_retry_at, item.id))

            self.stats = state.get("stats", self.stats)
            logger.info(f"Loaded {len(self._items)} items from retry queue")

        except Exception as e:
            logger.error(f"Failed to load retry queue: {e}")

    def get_stats(self) -> dict[str, Any]:
        """Get queue statistics."""
        return {**self.stats, "pending_count": len(self._items), "queue_size": len(self._queue)}
