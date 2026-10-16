"""Application-wide UI resources: bundled icons and custom CSS."""

from __future__ import annotations

from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gtk  # noqa: E402

APP_ID = "io.github.wavesync.WaveSync"
ICONS_DIR = Path(__file__).resolve().parent / "icons"
IMAGES_DIR = Path(__file__).resolve().parent / "images"


def image_path(name: str) -> str:
    """Return the bundled image path used by the setup guide."""
    return str(IMAGES_DIR / name)

_CSS = """
.wavesync-splash {
    background-color: var(--window-bg-color);
    color: var(--window-fg-color);
}
.wavesync-splash-title {
    font-size: 28pt;
    font-weight: 800;
    letter-spacing: 0.02em;
}
"""

_installed = False


def install_resources() -> None:
    """Register the bundled hicolor icons and the custom stylesheet (idempotent)."""
    global _installed
    if _installed:
        return
    display = Gdk.Display.get_default()
    if display is None:
        return
    icon_theme = Gtk.IconTheme.get_for_display(display)
    if ICONS_DIR.is_dir():
        icon_theme.add_search_path(str(ICONS_DIR))
    Gtk.Window.set_default_icon_name(APP_ID)

    provider = Gtk.CssProvider()
    provider.load_from_string(_CSS)
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    _installed = True

