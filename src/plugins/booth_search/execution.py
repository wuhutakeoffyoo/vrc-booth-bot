"""Bounded query context and cancellation-safe shared computations."""
import asyncio
import contextlib
import contextvars
import copy
import threading
import time
import uuid

_QUERY = contextvars.ContextVar("booth_query", default=None)
_META_LOCK = threading.Lock()


@contextlib.contextmanager
def query_scope(max_requests=12, timeout=180):
    current = _QUERY.get()
    if current is not None:
        yield current
        return
    value = {"request_id": uuid.uuid4().hex, "max_requests": max_requests,
             "deadline": time.time()+timeout, "wire_count": 0, "stages": {}}
    token = _QUERY.set(value)
    try:
        yield value
    finally:
        _QUERY.reset(token)


def request_context():
    value = _QUERY.get()
    return {name: value[name] for name in ("request_id", "max_requests", "deadline")} if value else None


def observe_wire(stats):
    value = _QUERY.get()
    if value and isinstance(stats, dict):
        with _META_LOCK:
            value["wire_count"] = max(value["wire_count"], int(stats.get("used") or 0))


def extend_budget(maximum=18):
    value = _QUERY.get()
    if value:
        value["max_requests"] = max(value["max_requests"], maximum)


async def stage(name, operation):
    started = time.monotonic()
    try:
        return await operation()
    finally:
        value = _QUERY.get()
        if value:
            value["stages"][name] = value["stages"].get(name, 0) + time.monotonic()-started


class SingleFlight:
    def __init__(self):
        self.running = {}

    async def run(self, key, factory, *, listener=None, timeout=180):
        record = self.running.get(key)
        if record is None:
            record = {"listeners": {}}
            self.running[key] = record

            async def notify(message):
                callbacks = list(record["listeners"].values())
                if callbacks:
                    await asyncio.gather(*(callback(message) for callback in callbacks),
                                         return_exceptions=True)

            async def produce():
                return await asyncio.wait_for(factory(notify), timeout=timeout)

            task = asyncio.create_task(produce())
            record["task"] = task

            def done(completed):
                if self.running.get(key) is record:
                    del self.running[key]
                if not completed.cancelled():
                    completed.exception()

            task.add_done_callback(done)
        ticket = object()
        if listener:
            record["listeners"][ticket] = listener
        try:
            return copy.deepcopy(await asyncio.shield(record["task"]))
        finally:
            record["listeners"].pop(ticket, None)
