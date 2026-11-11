"""Physical treat button adapter (PRD 3.2): press -> Feeder.dispense(Source.BUTTON).

Has no network dependency; works fully offline. The hardware callback may fire from
another thread, so work is handed to the event loop thread-safely.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any

from feeder.core.feeder import Feeder
from feeder.hardware.base import Buttons
from feeder.models import Source

log = logging.getLogger(__name__)

MIN_GAP_S = 0.5


class ButtonAdapter:
    def __init__(
        self,
        buttons: Buttons,
        feeder: Feeder,
        loop: asyncio.AbstractEventLoop | None = None,
        *,
        now: Callable[[], float] = time.monotonic,
        min_gap_s: float = MIN_GAP_S,
    ) -> None:
        self._feeder = feeder
        self._loop = loop
        self._now = now
        self._min_gap_s = min_gap_s
        self._lock = threading.Lock()
        self._inflight = False
        self._last_press: float | None = None
        buttons.on_treat_press(self._on_press)

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def _on_press(self) -> None:
        """Hardware callback (any thread). Never raises."""
        try:
            loop = self._loop
            if loop is None:
                log.warning("treat button pressed but no event loop is bound; ignoring")
                return
            with self._lock:
                t = self._now()
                if self._inflight:
                    log.debug("treat button press ignored: dispense in flight")
                    return
                if self._last_press is not None and 0 <= t - self._last_press < self._min_gap_s:
                    log.debug("treat button press ignored: debounce")
                    return
                self._last_press = t
                self._inflight = True
            try:
                fut = asyncio.run_coroutine_threadsafe(self._dispense(), loop)
            except Exception:
                with self._lock:
                    self._inflight = False
                raise
            fut.add_done_callback(self._done)
        except Exception:
            log.exception("treat button handler failed")

    async def _dispense(self) -> None:
        try:
            await self._feeder.dispense(Source.BUTTON)
        finally:
            with self._lock:
                self._inflight = False

    @staticmethod
    def _done(fut: Future[Any]) -> None:
        try:
            exc = fut.exception()
        except BaseException:  # cancelled
            return
        if exc is not None:
            log.error("button dispense failed", exc_info=exc)
