"""Retry queue with exponential backoff."""

import asyncio
import logging
from typing import Dict, Any, Optional, Callable, Awaitable, List
from dataclasses import dataclass, field
from datetime import datetime, UTC, timedelta
from enum import Enum
import heapq
import json

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
    data: Dict[str, Any]
    attempt: int = 0
    max_attempts: int = 3
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    next_retry_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_error: Optional[str] = None
    status: RetryStatus = RetryStatus.PENDING

    def __lt__(self, other):
        return self.next_retry_at < other.next_retry_at

    def calculate_next_retry(self, base_delay: float = 1.0, max_delay: float = 300.0):
        """Calculate next retry time with exponential backoff."""
        delay = min(base_delay * (2**self.attempt), max_delay)
        # Add jitter
        import random

        delay *= 0.5 + random.random()
        self.next_retry_at = datetime.now(UTC) + timedelta(seconds=delay)

    def to_dict(self) -> dict:
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
        config: Optional[RetryConfig] = None,
        handler: Optional[Callable[[Dict[str, Any]], Awaitable[bool]]] = None,
        persist_path: Optional[str] = None,
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

        self._queue: List[RetryItem] = []
        self._items: Dict[str, RetryItem] = {}
        self._lock = asyncio.Lock()
        self._running = False
        self._process_task: Optional[asyncio.Task] = None

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

    async def add(self, id: str, data: Dict[str, Any], error: Optional[str] = None) -> RetryItem:
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
                    # FIX: Do not push to heap again if already in queue.
                    # The item is modified in-place (reference), so when it pops from heap
                    # it will have the new next_retry_at.
                    # However, heapq doesn't re-sort when items change.
                    # We need to re-heapify or accept that the order might be slightly stale
                    # until it pops. Efficient approach: remove and re-add or lazy delete.
                    # Given the constraints, we will re-push but handle duplicates in get_ready.
                    heapq.heappush(self._queue, item)
                    self.stats["total_retried"] += 1
            else:
                item = RetryItem(
                    id=id, data=data, max_attempts=self.config.max_attempts, last_error=error
                )
                item.calculate_next_retry(self.config.base_delay, self.config.max_delay)
                self._items[id] = item
                heapq.heappush(self._queue, item)
                self.stats["total_added"] += 1

            # FIX: Await non-blocking persistence
            await self._persist_state()
            return item

    async def get_ready(self) -> List[RetryItem]:
        """Get items ready for retry."""
        now = datetime.now(UTC)
        ready = []

        async with self._lock:
            # Clean up the queue from stale references and check timing
            while self._queue:
                # Peek first
                if self._queue[0].next_retry_at > now:
                    break

                item = heapq.heappop(self._queue)

                # FIX: Verify item is still valid and active
                if item.id not in self._items:
                    continue

                # Fix: Check if this is the latest reference (optimization) or just use state
                if item.status == RetryStatus.EXHAUSTED:
                    continue

                # Skip items already in-flight. add() pushes a duplicate
                # heap entry when called on an existing id (lazy-delete
                # strategy); without this guard a stale entry can be popped
                # while the same item is still being processed by an
                # earlier handler invocation — handler runs twice, the
                # recipient receives the email twice.
                if item.status == RetryStatus.RETRYING:
                    continue

                # Prepare for retry
                item.status = RetryStatus.RETRYING
                ready.append(item)

        return ready

    async def mark_success(self, id: str):
        """Mark item as successfully processed."""
        async with self._lock:
            if id in self._items:
                self._items[id].status = RetryStatus.SUCCESS
                self.stats["total_success"] += 1
                del self._items[id]
                await self._persist_state()

    async def mark_failed(self, id: str, error: str):
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

    async def start(self):
        """Start processing retry queue."""
        if self._running:
            return

        self._running = True
        self._process_task = asyncio.create_task(self._process_loop())
        logger.info("Retry queue started")

    async def stop(self):
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

    async def _process_loop(self):
        """Main processing loop."""
        semaphore = asyncio.Semaphore(max(1, self.config.concurrency))

        while self._running:
            try:
                ready_items = await self.get_ready()

                if ready_items and self.handler:

                    async def process_item(item: RetryItem):
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

    async def _persist_state(self):
        """Persist queue state to disk asynchronously."""
        if not self.persist_path:
            return

        try:
            state = {"items": {k: v.to_dict() for k, v in self._items.items()}, "stats": self.stats}

            # FIX: Run file I/O in thread pool to avoid blocking event loop
            await asyncio.to_thread(self._write_state_to_disk, state)

        except Exception as e:
            logger.error(f"Failed to persist retry queue: {e}")

    def _write_state_to_disk(self, state: dict):
        """Synchronous write helper for to_thread."""
        try:
            # Atomic write pattern (write temp then rename)
            temp_path = f"{self.persist_path}.tmp"
            with open(temp_path, "w") as f:
                json.dump(state, f)
            import shutil

            shutil.move(temp_path, self.persist_path or "")
        except Exception as e:
            logger.error(f"Disk write error in retry queue: {e}")
            raise

    def _load_state(self):
        """Load persisted state."""
        if not self.persist_path:
            return

        try:
            import os

            if not os.path.exists(self.persist_path):
                return

            with open(self.persist_path, "r") as f:
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
                    heapq.heappush(self._queue, item)

            self.stats = state.get("stats", self.stats)
            logger.info(f"Loaded {len(self._items)} items from retry queue")

        except Exception as e:
            logger.error(f"Failed to load retry queue: {e}")

    def get_stats(self) -> Dict[str, Any]:
        """Get queue statistics."""
        return {**self.stats, "pending_count": len(self._items), "queue_size": len(self._queue)}
