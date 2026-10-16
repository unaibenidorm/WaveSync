"""Background thread runner dispatching callbacks to the GTK main loop via ``GLib.idle_add``."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import TypeVar

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402

from wavesync.core.errors import Cancelled, CancelToken, WaveSyncError  # noqa: E402

T = TypeVar("T")


class BackgroundTask:
    """Handle for a running background worker thread."""

    def __init__(self, cancel_token: CancelToken, thread: threading.Thread) -> None:
        self.cancel_token = cancel_token
        self._thread = thread

    def cancel(self) -> None:
        """Request cooperative cancellation."""
        self.cancel_token.cancel()

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()


def run_in_background(
    work: Callable[[CancelToken, Callable[[float, str], None]], T],
    *,
    on_progress: Callable[[float, str], None] | None = None,
    on_done: Callable[[T], None] | None = None,
    on_error: Callable[[WaveSyncError], None] | None = None,
    on_cancelled: Callable[[], None] | None = None,
) -> BackgroundTask:
    """Run ``work(cancel_token, progress_cb)`` on a daemon thread.

    All UI callbacks are marshalled onto the main GTK thread with
    :func:`GLib.idle_add`.
    """
    token = CancelToken()

    def report_progress(fraction: float, message: str) -> None:
        if on_progress is not None:
            GLib.idle_add(on_progress, float(fraction), str(message))

    def target() -> None:
        try:
            res = work(token, report_progress)
        except Cancelled:
            if on_cancelled is not None:
                GLib.idle_add(on_cancelled)
        except WaveSyncError as exc:
            if on_error is not None:
                GLib.idle_add(on_error, exc)
        except Exception as exc:  # unexpected error → wrap for UI
            err = WaveSyncError(str(exc), repr(exc))
            if on_error is not None:
                GLib.idle_add(on_error, err)
        else:
            if on_done is not None:
                GLib.idle_add(on_done, res)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return BackgroundTask(token, thread)

