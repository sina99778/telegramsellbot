"""Shared clients stay open until their last borrower releases them."""
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

@dataclass
class PoolEntry:
    config: Any
    client: Any
    borrowers: int = 0
    retired: bool = False

async def evict(pool, key):
    entry = pool.pop(key, None)
    if entry is not None:
        entry.retired = True
        if entry.borrowers == 0:
            await entry.client.aclose()

@asynccontextmanager
async def borrow(pool, key, config, factory):
    # No await between lookup, insertion and increment: atomic on the event loop.
    entry = pool.get(key)
    old = None
    if entry is None or entry.config != config:
        old = entry
        if old is not None:
            old.retired = True
        entry = PoolEntry(config, factory(config))
        pool[key] = entry
    entry.borrowers += 1
    try:
        if old is not None and old.borrowers == 0:
            await old.client.aclose()
        yield entry.client
    except Exception:
        # Next operation gets a fresh transport/session without editing the URL.
        # Other borrowers finish safely on this retired entry.
        if pool.get(key) is entry:
            pool.pop(key)
        entry.retired = True
        raise
    finally:
        entry.borrowers -= 1
        if entry.retired and entry.borrowers == 0:
            await entry.client.aclose()
