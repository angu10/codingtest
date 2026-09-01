"""The one place replay is allowed to wait.

There is no fixed sleep in `replay/` that stands in for a check. `POLL_INTERVAL_SECONDS` is the
interval between *evaluations* of a condition: every wait built on `poll_until` ends because the
condition became true, or because a declared deadline expired and the step failed — never because a
duration elapsed and execution simply carried on. That distinction is invariant 3.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

POLL_INTERVAL_SECONDS = 0.1


async def poll_until[T](probe: Callable[[], Awaitable[T | None]], timeout_ms: int) -> T | None:
    """Re-evaluate ``probe`` until it yields a value or the deadline passes."""

    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        result = await probe()
        if result is not None:
            return result
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
