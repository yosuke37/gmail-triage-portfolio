"""Optional in-process usage reporting, without storing prompt content."""
from contextvars import ContextVar
from contextlib import contextmanager

_listener = ContextVar("usage_listener", default=None)


@contextmanager
def capture_usage(listener):
    parent = _listener.get()
    def emit(event):
        try:
            listener(event)
        finally:
            if parent:
                parent(event)
    token = _listener.set(emit)
    try:
        yield
    finally:
        _listener.reset(token)


def report_usage(result):
    listener = _listener.get()
    usage = getattr(getattr(result, "context_wrapper", None), "usage", None)
    if listener and usage:
        listener({name: int(getattr(usage, name, 0)) for name in ("requests", "input_tokens", "output_tokens", "total_tokens")})
