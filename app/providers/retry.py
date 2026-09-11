from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable, Iterable
from typing import TypeVar

T = TypeVar("T")

log = logging.getLogger("retry")


async def with_retries(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 10.0,
    exceptions: Iterable[type[BaseException]] = (Exception,),
    what: str = "operation",
) -> T:
    retry_on = tuple(exceptions)
    last_exc: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await fn()
        except retry_on as exc:
            last_exc = exc
            if attempt >= attempts:
                break
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            delay *= random.uniform(0.8, 1.3)
            log.warning(
                "retry %s: attempt %d/%d failed (%s), retrying in %.1fs",
                what,
                attempt,
                attempts,
                type(exc).__name__,
                delay,
            )
            await asyncio.sleep(delay)
    raise last_exc if last_exc else RuntimeError(f"{what}: retries exhausted")
