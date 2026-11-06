"""Background loops: the escalation ticker and the (optional) UAC poller."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from app.engine.escalation import EscalationEngine
from app.integrations.base import IntegrationError
from app.integrations.uac import UacClient

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, engine: EscalationEngine, uac: UacClient | None = None):
        self.engine = engine
        self.uac = uac
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()
        self.last_tick_ok = None  # datetime of the last successful tick, for /api/ready

    def start(self) -> None:
        self._tasks.append(asyncio.create_task(self._tick_loop(), name="escalation-ticker"))
        if self.uac and self.engine.s.uac_poll_enabled:
            self._tasks.append(asyncio.create_task(self._poll_loop(), name="uac-poller"))

    async def stop(self) -> None:
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)

    async def _tick_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.engine.tick()
                self.last_tick_ok = self.engine.clock.now()
            except Exception:
                log.exception("worker tick failed")
            await self._sleep(self.engine.s.worker_tick_seconds)

    async def _poll_loop(self) -> None:
        assert self.uac is not None
        window = f"-{max(2, self.engine.s.uac_poll_seconds * 3 // 60)}mn"  # overlap; duplicates are ignored
        while not self._stop.is_set():
            try:
                for event in await self.uac.list_failed(since=window):
                    await self.engine.open(event)
            except IntegrationError as exc:
                log.warning("UAC poll failed", extra={"error": str(exc)})
            except Exception:
                log.exception("UAC poll crashed")
            await self._sleep(self.engine.s.uac_poll_seconds)
