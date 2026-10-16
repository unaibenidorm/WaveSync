"""Custom GTK4 DrawingArea visualising segments and per-window offset/confidence."""

from __future__ import annotations

import math

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from wavesync.core.i18n import _  # noqa: E402
from wavesync.core.models import SyncResult, SyncSegment, SyncWindow  # noqa: E402


def _fmt_clock(sec: float) -> str:
    sec = max(0.0, float(sec))
    m, s = divmod(int(round(sec)), 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _conf_color(conf: float) -> tuple[float, float, float]:
    """RGB color from confidence in [0, 1] (red → amber → green)."""
    if conf >= 0.65:
        return (0.18, 0.76, 0.49)  # GNOME green
    if conf >= 0.38:
        return (0.90, 0.65, 0.04)  # GNOME yellow/amber
    return (0.88, 0.24, 0.24)  # GNOME red


class TimelineWidget(Gtk.DrawingArea):
    """Visual timeline of segments and measured windows along master duration."""

    def __init__(self) -> None:
        super().__init__()
        self.set_content_height(150)
        self.set_hexpand(True)
        self._duration: float = 0.0
        self._segments: list[SyncSegment] = []
        self._windows: list[SyncWindow] = []
        self.set_draw_func(self._on_draw)

    def set_data(self, duration: float, result: SyncResult | None) -> None:
        self._duration = max(0.0, float(duration))
        if result is None:
            self._segments = []
            self._windows = []
        else:
            self._segments = list(result.segments)
            self._windows = list(result.windows)
        self.queue_draw()

    def _on_draw(self, _area: Gtk.DrawingArea, cr, width: int, height: int) -> None:
        pad_x = 16.0
        pad_y = 14.0
        w = max(10.0, width - 2 * pad_x)
        h = max(10.0, height - 2 * pad_y)

        # Background card
        cr.set_source_rgba(0.5, 0.5, 0.5, 0.10)
        cr.rectangle(pad_x, pad_y, w, h)
        cr.fill()

        if self._duration <= 0 or (not self._segments and not self._windows):
            cr.set_source_rgba(0.55, 0.55, 0.55, 0.85)
            cr.select_font_face("Sans", 0, 0)
            cr.set_font_size(12.0)
            msg = _("Ejecuta el análisis para visualizar la línea temporal de tramos y ventanas.")
            ext = cr.text_extents(msg)
            cr.move_to(pad_x + (w - ext.width) / 2, pad_y + (h + ext.height) / 2)
            cr.show_text(msg)
            return

        # Top band: segments bar (height 34 px)
        bar_y = pad_y + 8.0
        bar_h = 32.0
        for idx, seg in enumerate(self._segments):
            x0 = pad_x + (seg.start / self._duration) * w
            x1 = pad_x + (seg.end / self._duration) * w
            sw = max(2.0, x1 - x0)
            if seg.is_gap:
                cr.set_source_rgba(0.5, 0.5, 0.5, 0.35)
            else:
                r, g, b = _conf_color(seg.confidence)
                cr.set_source_rgba(r, g, b, 0.32 if idx % 2 == 0 else 0.48)
            cr.rectangle(x0, bar_y, sw, bar_h)
            cr.fill()

            cr.set_source_rgba(0.6, 0.6, 0.6, 0.55)
            cr.set_line_width(1.0)
            cr.rectangle(x0, bar_y, sw, bar_h)
            cr.stroke()

            if sw > 65:
                cr.set_source_rgba(0.85, 0.85, 0.85, 0.95)
                cr.set_font_size(11.0)
                lbl = _("Hueco") if seg.is_gap else f"{seg.offset_ms:+.0f} ms ({seg.confidence:.0%})"
                ext = cr.text_extents(lbl)
                if ext.width + 8 < sw:
                    cr.move_to(x0 + (sw - ext.width) / 2, bar_y + (bar_h + ext.height) / 2 - 1)
                    cr.show_text(lbl)

        # Lower plot: window offsets vs time
        plot_y0 = bar_y + bar_h + 12.0
        plot_h = max(24.0, pad_y + h - plot_y0 - 20.0)
        valid_offs = [win.offset * 1000.0 for win in self._windows if win.offset is not None]
        if valid_offs:
            lo = min(valid_offs)
            hi = max(valid_offs)
            span = max(40.0, hi - lo)
            mid = 0.5 * (lo + hi)
            y_min, y_max = mid - 0.65 * span, mid + 0.65 * span

            # Reference midline
            cr.set_source_rgba(0.5, 0.5, 0.5, 0.25)
            cr.set_line_width(1.0)
            cr.move_to(pad_x, plot_y0 + plot_h / 2)
            cr.line_to(pad_x + w, plot_y0 + plot_h / 2)
            cr.stroke()

            for win in self._windows:
                if win.offset is None:
                    continue
                ms = win.offset * 1000.0
                cx = pad_x + (win.center / self._duration) * w
                norm_y = (ms - y_min) / max(1e-6, y_max - y_min)
                cy = plot_y0 + plot_h * (1.0 - max(0.0, min(1.0, norm_y)))
                r, g, b = _conf_color(win.confidence)
                alpha = 0.95 if win.inlier else 0.40
                cr.set_source_rgba(r, g, b, alpha)
                radius = 4.5 if win.inlier else 3.0
                cr.arc(cx, cy, radius, 0, 2 * math.pi)
                cr.fill()

        # Time axis ticks at bottom
        cr.set_source_rgba(0.65, 0.65, 0.65, 0.85)
        cr.set_font_size(10.0)
        for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
            tx = pad_x + frac * w
            lbl = _fmt_clock(frac * self._duration)
            ext = cr.text_extents(lbl)
            lx = max(pad_x, min(pad_x + w - ext.width, tx - ext.width / 2))
            cr.move_to(lx, pad_y + h - 4.0)
            cr.show_text(lbl)

