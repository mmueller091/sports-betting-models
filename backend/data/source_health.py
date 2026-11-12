"""Collect safe, public source status without URLs or credentials."""
from contextvars import ContextVar

_warnings = ContextVar("source_warnings", default=None)


def begin_collection():
    return _warnings.set([])


def end_collection(token):
    warnings = list(dict.fromkeys(_warnings.get() or []))
    _warnings.reset(token)
    return warnings


def warn(message):
    warnings = _warnings.get()
    if warnings is not None:
        warnings.append(message)


async def safe_fetch(coro, default):
    name = getattr(getattr(coro, "cr_code", None), "co_name", "data source")
    try:
        return await coro
    except Exception:
        warn(f"{name}: unavailable; fallback data used")
        return default

