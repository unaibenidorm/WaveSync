"""Entry point for ``python -m wavesync``.

When invoked with CLI subcommands (``probe``, ``analyze``, ``sync`` or
``--cli``), delegates to :mod:`wavesync.core.cli` without importing GTK.
Otherwise launches the GTK4/Libadwaita desktop application (implemented in
Phase 2).
"""

from __future__ import annotations

import sys

_CLI_COMMANDS = {"probe", "analyze", "sync", "batch"}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and (args[0] in _CLI_COMMANDS or args[0] == "--cli"):
        if args[0] == "--cli":
            args = args[1:]
        from wavesync.core.cli import main as cli_main

        return cli_main(args)
    try:
        from wavesync.ui.app import run_app
    except ImportError:
        from wavesync.core.cli import main as cli_main

        return cli_main(args or ["--help"])
    return run_app(args)


if __name__ == "__main__":
    raise SystemExit(main())

