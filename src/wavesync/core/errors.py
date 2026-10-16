"""Exceptions and cancellation primitives shared by the whole core.

All exceptions raised on purpose by :mod:`wavesync.core` derive from
:class:`WaveSyncError` and carry a human readable (Spanish, gettext-ready)
message in ``user_message`` so the UI can show it verbatim.
"""

from __future__ import annotations

import threading

from wavesync.core.i18n import _


class WaveSyncError(Exception):
    """Base error. ``user_message`` is safe to display to end users."""

    def __init__(self, user_message: str, detail: str = "") -> None:
        super().__init__(user_message if not detail else f"{user_message}\n{detail}")
        self.user_message = user_message
        self.detail = detail


class FFmpegNotFoundError(WaveSyncError):
    """ffmpeg or ffprobe could not be located."""


class MediaError(WaveSyncError):
    """The file is missing, corrupt or lacks the required streams."""


class FFmpegError(WaveSyncError):
    """An ffmpeg/ffprobe process failed."""


class AnalysisError(WaveSyncError):
    """Synchronisation could not be computed."""


class ExportError(WaveSyncError):
    """Export failed or the output did not verify."""


class Cancelled(WaveSyncError):
    """The operation was cancelled by the user."""

    def __init__(self) -> None:
        super().__init__(_("Operación cancelada."))


class CancelToken:
    """Thin wrapper around :class:`threading.Event` used for cooperative cancellation."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        """Request cancellation."""
        self._event.set()

    @property
    def cancelled(self) -> bool:
        """``True`` once :meth:`cancel` was called."""
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        """Raise :class:`Cancelled` if cancellation was requested."""
        if self._event.is_set():
            raise Cancelled()

    def check(self) -> None:
        """Raise :class:`Cancelled` if cancellation was requested (alias for :meth:`raise_if_cancelled`)."""
        self.raise_if_cancelled()

    def wait(self, timeout: float) -> bool:
        """Sleep up to ``timeout`` seconds; return ``True`` if cancelled meanwhile."""
        return self._event.wait(timeout)

