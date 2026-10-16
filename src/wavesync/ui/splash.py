"""Opaque startup splash that lifts upwards (like a curtain) to reveal the app."""

from __future__ import annotations

from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, GLib, Graphene, Gtk  # noqa: E402

from wavesync.core.i18n import _  # noqa: E402
from wavesync.ui.resources import APP_ID  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Callable

INTRO_MS = 450
HOLD_MS = 700
LIFT_MS = 650


class StartupSplash(Gtk.Widget):
    """Full-window, fully opaque splash.

    Sequence: the logo fades and drops into place, stays briefly, and then the
    whole (opaque) panel slides upwards out of the window, uncovering the UI.
    The upward motion is done by translating the snapshot, so the panel always
    keeps the full window size and never becomes see-through.
    """

    def __init__(self) -> None:
        super().__init__(hexpand=True, vexpand=True, can_target=True)
        self.set_layout_manager(Gtk.BinLayout())
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self._lift = 0.0  # 0 = covering the window, 1 = fully lifted out
        self._on_finished: Callable[[], None] | None = None

        self._panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._panel.add_css_class("wavesync-splash")
        self._panel.set_parent(self)

        self._content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
            vexpand=True,
            opacity=0.0,
        )
        self._panel.append(self._content)

        logo = Gtk.Image.new_from_icon_name(APP_ID)
        logo.set_pixel_size(160)
        self._content.append(logo)

        title = Gtk.Label(label="WaveSync")
        title.add_css_class("wavesync-splash-title")
        self._content.append(title)

        subtitle = Gtk.Label(label=_("Sincronizador universal de pistas de audio"))
        subtitle.add_css_class("dim-label")
        self._content.append(subtitle)

        spinner = Adw.Spinner(margin_top=12, width_request=24, height_request=24)
        self._content.append(spinner)

        target = Adw.CallbackAnimationTarget.new(self._set_intro)
        self._intro = Adw.TimedAnimation.new(self, 0.0, 1.0, INTRO_MS, target)
        self._intro.set_easing(Adw.Easing.EASE_OUT_CUBIC)

        target = Adw.CallbackAnimationTarget.new(self._set_lift)
        self._lift_anim = Adw.TimedAnimation.new(self, 0.0, 1.0, LIFT_MS, target)
        self._lift_anim.set_easing(Adw.Easing.EASE_IN_OUT_CUBIC)
        self._lift_anim.connect("done", self._on_lift_done)

    # -- public API -----------------------------------------------------
    def play(self, on_finished: Callable[[], None] | None = None) -> None:
        """Run the intro → hold → lift sequence, then call ``on_finished``."""
        self._on_finished = on_finished
        self._intro.play()
        GLib.timeout_add(INTRO_MS + HOLD_MS, self._start_lift)

    @property
    def lift(self) -> float:
        return self._lift

    # -- animation callbacks -------------------------------------------
    def _set_intro(self, value: float) -> None:
        self._content.set_opacity(value)
        # Drop in from 32 px above its resting place.
        self._content.set_margin_bottom(int(round(64 * (1.0 - value))))

    def _start_lift(self) -> bool:
        self.set_can_target(False)  # let clicks through while lifting
        self._lift_anim.play()
        return GLib.SOURCE_REMOVE

    def _set_lift(self, value: float) -> None:
        self._lift = value
        self.queue_draw()

    def _on_lift_done(self, _anim: Adw.TimedAnimation) -> None:
        self.set_visible(False)
        callback, self._on_finished = self._on_finished, None
        if callback is not None:
            callback()

    # -- GtkWidget vfuncs ----------------------------------------------
    def do_snapshot(self, snapshot) -> None:  # noqa: ANN001 - Gtk.Snapshot
        offset = -self._lift * self.get_height()
        if offset:
            point = Graphene.Point()
            point.init(0.0, offset)
            snapshot.translate(point)
        self.snapshot_child(self._panel, snapshot)

    def do_dispose(self) -> None:
        if self._panel is not None:
            self._panel.unparent()
            self._panel = None
        Gtk.Widget.do_dispose(self)

