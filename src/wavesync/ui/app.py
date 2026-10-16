"""Adw.Application entry point for WaveSync."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib  # noqa: E402

from wavesync.core.i18n import set_language  # noqa: E402
from wavesync.ui.preferences import UserPreferences  # noqa: E402
from wavesync.ui.resources import APP_ID, install_resources  # noqa: E402
from wavesync.ui.window import WaveSyncWindow  # noqa: E402


class WaveSyncApplication(Adw.Application):
    """GTK4/Libadwaita application."""

    def __init__(self) -> None:
        # Widget labels are translated when constructed, so select the saved
        # language before GTK creates the application window.
        set_language(UserPreferences.load().ui_language)
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        GLib.set_application_name("WaveSync")

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        install_resources()
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_args: self.quit())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<Control>q"])
        self.set_accels_for_action("win.preferences", ["<Control>comma"])
        self.set_accels_for_action("window.close", ["<Control>w"])

    def do_activate(self) -> None:
        win = self.props.active_window
        if not win:
            win = self._create_window()
        win.present()

    def _create_window(self) -> WaveSyncWindow:
        return WaveSyncWindow(application=self)


def run_app(argv: list[str] | None = None) -> int:
    app = WaveSyncApplication()
    return int(app.run(argv))
