"""Entry point for ``python -m wavesync``.

When invoked with CLI subcommands (``probe``, ``analyze``, ``sync`` or
``--cli``), delegates to :mod:`wavesync.core.cli` without importing GTK.
Otherwise launches the GTK4/Libadwaita desktop application (implemented in
Phase 2).
"""

from __future__ import annotations

import sys

_CLI_COMMANDS = {"probe", "analyze", "sync", "batch", "-h", "--help"}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and (args[0] in _CLI_COMMANDS or args[0] == "--cli"):
        if args[0] == "--cli":
            args = args[1:]
        from wavesync.core.cli import main as cli_main

        return cli_main(args)
    try:
        from wavesync.ui.app import run_app
    except ImportError as exc:
        # Only fall back to the CLI when the GUI stack (PyGObject/GTK) is not
        # available. Any other import error is a real bug and must be shown.
        if not _is_gui_import_error(exc):
            raise
        sys.stderr.write(
            "WaveSync: no se pudo cargar la interfaz gráfica (PyGObject/GTK 4).\n"
            f"  Detalle: {exc}\n"
            "  Instala PyGObject y GTK 4, o usa la interfaz de línea de órdenes:\n"
            "    wavesync --cli --help\n"
        )
        return 1
    return run_app(args)


def _is_gui_import_error(exc: BaseException) -> bool:
    """Return True when *exc* comes from a missing PyGObject/GTK module."""
    if isinstance(exc, ModuleNotFoundError):
        return exc.name in {"gi", "gi.repository"} or (exc.name or "").startswith("gi.")
    message = str(exc)
    return "No module named 'gi'" in message or "PyGObject" in message


if __name__ == "__main__":
    raise SystemExit(main())

