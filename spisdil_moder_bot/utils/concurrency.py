from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable, Iterable, TypeVar

T = TypeVar("T")


async def run_blocking(func: Callable[..., T], *args: Any, loop: asyncio.AbstractEventLoop | None = None) -> T:
    """Run a blocking callable in the default executor."""
    event_loop = loop or asyncio.get_running_loop()
    return await event_loop.run_in_executor(None, lambda: func(*args))


async def bounded_gather(
    coroutines: Iterable[Callable[[], Awaitable[T]]],
    limit: int,
) -> list[T]:
    """Run callables returning coroutines with concurrency limit."""
    semaphore = asyncio.Semaphore(limit)
    results: list[T] = []

    async def run(coro_factory: Callable[[], Awaitable[T]]) -> None:
        async with semaphore:
            result = await coro_factory()
            results.append(result)

    await asyncio.gather(*(run(factory) for factory in coroutines))
    return results


@asynccontextmanager
async def staggered_timer(delay: float) -> asyncio.TaskGroup:
    """
    Context manager returning TaskGroup and sleeping `delay` between
    scheduled tasks to avoid thundering herd.
    """
    async with asyncio.TaskGroup() as group:
        yield group
        await asyncio.sleep(delay)
