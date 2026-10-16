"""Locating and running ffmpeg / ffprobe.

Binaries are searched in this order:

1. ``WAVESYNC_FFMPEG`` environment variable: path to the ``ffmpeg`` binary
   or to a directory containing ``ffmpeg`` and ``ffprobe``.
2. Next to the application: ``$APPDIR/usr/bin`` (AppImage), the directory of
   ``sys.executable``, ``sys._MEIPASS`` (frozen builds) and ``<repo>/bin``.
3. The ``PATH``.

Processes are always started with an argument list (never ``shell=True``)
and can be cancelled through a :class:`~wavesync.core.errors.CancelToken`.
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from wavesync.core.errors import CancelToken, FFmpegError, FFmpegNotFoundError
from wavesync.core.i18n import _

_EXE = ".exe" if os.name == "nt" else ""


@dataclass(frozen=True, slots=True)
class FFmpegBinaries:
    """Resolved paths to ffmpeg and ffprobe."""

    ffmpeg: Path
    ffprobe: Path


def _candidate_dirs() -> list[Path]:
    dirs: list[Path] = []
    appdir = os.environ.get("APPDIR")
    if appdir:
        dirs.append(Path(appdir) / "usr" / "bin")
    dirs.append(Path(sys.executable).resolve().parent)
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        dirs.append(Path(meipass))
    dirs.append(Path(__file__).resolve().parents[3] / "bin")
    return dirs


def _is_exe(p: Path) -> bool:
    return p.is_file() and os.access(p, os.X_OK)


def locate_binaries(env: dict[str, str] | None = None) -> FFmpegBinaries:
    """Find ffmpeg and ffprobe or raise :class:`FFmpegNotFoundError`."""
    env = dict(os.environ) if env is None else env
    override = env.get("WAVESYNC_FFMPEG")
    if override:
        p = Path(override).expanduser()
        ffmpeg = p / f"ffmpeg{_EXE}" if p.is_dir() else p
        ffprobe = ffmpeg.with_name(f"ffprobe{_EXE}")
        if _is_exe(ffmpeg) and _is_exe(ffprobe):
            return FFmpegBinaries(ffmpeg, ffprobe)
        raise FFmpegNotFoundError(
            _("WAVESYNC_FFMPEG apunta a «{path}», pero ahí no hay ffmpeg y ffprobe ejecutables.").format(
                path=override
            )
        )
    for d in _candidate_dirs():
        ffmpeg, ffprobe = d / f"ffmpeg{_EXE}", d / f"ffprobe{_EXE}"
        if _is_exe(ffmpeg) and _is_exe(ffprobe):
            return FFmpegBinaries(ffmpeg, ffprobe)
    path = env.get("PATH")
    f1, f2 = shutil.which("ffmpeg", path=path), shutil.which("ffprobe", path=path)
    if f1 and f2:
        return FFmpegBinaries(Path(f1), Path(f2))
    raise FFmpegNotFoundError(
        _("No se encontró ffmpeg/ffprobe. Instálalo (p. ej. «sudo dnf install ffmpeg» o "
          "«sudo apt install ffmpeg») o define la variable WAVESYNC_FFMPEG.")
    )


@functools.lru_cache(maxsize=1)
def binaries() -> FFmpegBinaries:
    """Cached :func:`locate_binaries` (call ``binaries.cache_clear()`` to re-scan)."""
    return locate_binaries()


@functools.lru_cache(maxsize=1)
def available_hwaccels() -> tuple[str, ...]:
    """Return hardware acceleration methods advertised by the active FFmpeg."""
    try:
        out, err = run([str(binaries().ffmpeg), "-hide_banner", "-hwaccels"], check=False)
    except FFmpegError:
        return ()
    methods: list[str] = []
    text = err
    if out:
        text += "\n" + out.decode("utf-8", errors="replace")
    for line in text.splitlines():
        value = line.strip().lower()
        if value and value != "hardware acceleration methods:":
            methods.append(value)
    return tuple(dict.fromkeys(methods))


def hwaccel_summary() -> str:
    """Human-readable backend status for diagnostics and progress messages."""
    methods = available_hwaccels()
    if not methods:
        return _("CPU (sin aceleración hardware FFmpeg)")
    return _("FFmpeg hardware: {backends}").format(backends=", ".join(methods))


def run(
    args: list[str],
    cancel: CancelToken | None = None,
    *,
    input_bytes: bytes | None = None,
    poll: float = 0.2,
    check: bool = True,
) -> tuple[bytes, str]:
    """Run a process, return ``(stdout, stderr_text)``.

    Polls ``cancel`` while waiting and kills the process if cancelled.
    Raises :class:`FFmpegError` on non-zero exit when ``check`` is true.
    """
    if cancel is not None:
        cancel.raise_if_cancelled()
    try:
        proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:  # binary vanished, permissions...
        raise FFmpegError(_("No se pudo ejecutar {prog}.").format(prog=Path(args[0]).name), str(exc)) from exc
    pending_input = input_bytes
    while True:
        try:
            out, err = proc.communicate(input=pending_input, timeout=poll)
            break
        except subprocess.TimeoutExpired:
            pending_input = None
            if cancel is not None and cancel.cancelled:
                proc.kill()
                proc.communicate()
                cancel.raise_if_cancelled()
    err_text = err.decode("utf-8", errors="replace")
    if check and proc.returncode != 0:
        tail = "\n".join(err_text.strip().splitlines()[-12:])
        raise FFmpegError(
            _("{prog} terminó con error (código {code}).").format(prog=Path(args[0]).name, code=proc.returncode),
            tail,
        )
    return out, err_text


def fmt_time(seconds: float) -> str:
    """Format seconds for ffmpeg arguments (microsecond precision, no exponent)."""
    return f"{seconds:.6f}"
