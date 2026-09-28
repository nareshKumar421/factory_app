"""Memoising an SAP lookup without memoising an outage.

Some lookups (tax codes, branch states, column metadata) are read once per
worker and kept, and each falls back to an empty answer when HANA does not
answer, so a posting is never blocked by a lookup. Under a plain ``lru_cache``
that fallback is remembered too: one blip and the worker goes on answering "no
tax codes" until gunicorn restarts, long after SAP is back. A lookup under
:func:`cache_answers` raises :class:`NotCached` with its fallback instead of
returning it; the caller still gets the fallback, and the next call asks SAP.
"""

import functools


class NotCached(Exception):
    """A memoised lookup failed: hand back ``value`` and remember nothing."""

    def __init__(self, value):
        super().__init__()
        self.value = value


def cache_answers(maxsize=128):
    """``functools.lru_cache``, for answers only."""

    def decorate(fn):
        cached = functools.lru_cache(maxsize=maxsize)(fn)

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return cached(*args, **kwargs)
            except NotCached as miss:
                return miss.value

        wrapper.cache_clear = cached.cache_clear
        wrapper.cache_info = cached.cache_info
        return wrapper

    return decorate
