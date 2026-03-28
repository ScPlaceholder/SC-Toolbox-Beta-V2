"""Thread-safe background worker utilities."""
import logging
import threading
from typing import Callable, Optional

log = logging.getLogger(__name__)


def run_in_background(fn: Callable, on_done: Optional[Callable] = None,
                      on_error: Optional[Callable] = None) -> threading.Thread:
    """Run fn() in a daemon thread. Calls on_done() or on_error(exc) when finished.

    NOTE: on_done/on_error run in the background thread. Callers must use
    root.after(0, callback) to marshal UI updates to the main thread.
    """
    def _wrapper():
        try:
            fn()
        except Exception as exc:  # broad catch intentional: generic task runner
            log.exception("Background task failed: %s", exc)
            if on_error:
                on_error(exc)
            return
        if on_done:
            on_done()

    t = threading.Thread(target=_wrapper, daemon=True)
    t.start()
    return t
