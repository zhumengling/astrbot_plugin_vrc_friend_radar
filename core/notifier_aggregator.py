from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Awaitable, Callable

try:
    from astrbot.api import logger
except ModuleNotFoundError:  # test fallback
    import logging
    logger = logging.getLogger(__name__)

from .events import StandardEvent


class NotifierAggregator:
    def __init__(self, max_batch_size: int = 20, send_retry_count: int = 1, send_retry_delay_seconds: float = 0.2):
        self.max_batch_size = max(1, int(max_batch_size or 1))
        self.send_retry_count = max(0, int(send_retry_count or 0))
        self.send_retry_delay_seconds = max(0.0, float(send_retry_delay_seconds or 0.0))
        self._buffer: "OrderedDict[str, StandardEvent]" = OrderedDict()

    def add_event(self, event: StandardEvent) -> None:
        if event.event_id in self._buffer:
            self._buffer.move_to_end(event.event_id)
            logger.info(
                "[vrc_friend_radar] aggregator_enqueue event_type=%s friend_user_id=%s pending_count=%s deduped=true",
                event.event_type,
                event.friend_user_id,
                len(self._buffer),
            )
            return
        self._buffer[event.event_id] = event
        logger.info(
            "[vrc_friend_radar] aggregator_enqueue event_type=%s friend_user_id=%s pending_count=%s deduped=false",
            event.event_type,
            event.friend_user_id,
            len(self._buffer),
        )

    def add_events(self, events: list[StandardEvent]) -> None:
        for event in events:
            self.add_event(event)

    def flush(self) -> list[StandardEvent]:
        if not self._buffer:
            logger.info("[vrc_friend_radar] aggregator_flush batch_size=0 pending_count=0")
            return []

        out: list[StandardEvent] = []
        pending_before = len(self._buffer)
        flush_start = time.monotonic()
        while self._buffer and len(out) < self.max_batch_size:
            _, event = self._buffer.popitem(last=False)
            out.append(event)
        latency_ms = int((time.monotonic() - flush_start) * 1000)
        logger.info(
            "[vrc_friend_radar] aggregator_flush batch_size=%s pending_before=%s pending_after=%s latency_ms=%s",
            len(out),
            pending_before,
            len(self._buffer),
            latency_ms,
        )
        return out

    async def send_flushed(self, sender: Callable[[list[StandardEvent]], Awaitable[None]]) -> int:
        batch = self.flush()
        if not batch:
            return 0

        sample = batch[0]
        send_start = time.monotonic()
        logger.info(
            "[vrc_friend_radar] send_invoked batch_size=%s event_type=%s friend_user_id=%s",
            len(batch),
            sample.event_type,
            sample.friend_user_id,
        )
        attempts = self.send_retry_count + 1
        for attempt in range(1, attempts + 1):
            try:
                await sender(batch)
                total_latency_ms = int((time.monotonic() - send_start) * 1000)
                logger.info(
                    "[vrc_friend_radar] send_succeeded batch_size=%s event_type=%s friend_user_id=%s attempts=%s latency_ms=%s",
                    len(batch),
                    sample.event_type,
                    sample.friend_user_id,
                    attempt,
                    total_latency_ms,
                )
                return len(batch)
            except Exception as exc:
                attempt_latency_ms = int((time.monotonic() - send_start) * 1000)
                logger.error(
                    "[vrc_friend_radar] send_failed batch_size=%s event_type=%s friend_user_id=%s attempt=%s/%s latency_ms=%s err=%s",
                    len(batch),
                    sample.event_type,
                    sample.friend_user_id,
                    attempt,
                    attempts,
                    attempt_latency_ms,
                    exc,
                    exc_info=True,
                )
                if attempt >= attempts:
                    for event in reversed(batch):
                        self._buffer[event.event_id] = event
                        self._buffer.move_to_end(event.event_id, last=False)
                    logger.warning(
                        "[vrc_friend_radar] send_degraded_requeue batch_size=%s pending_count=%s",
                        len(batch),
                        len(self._buffer),
                    )
                    raise
                if self.send_retry_delay_seconds > 0:
                    await asyncio.sleep(self.send_retry_delay_seconds)
        return 0

    def pending_count(self) -> int:
        return len(self._buffer)
