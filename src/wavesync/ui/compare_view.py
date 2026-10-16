"""Phase 3 view: A/B comparator player + manual calibration (offset, tempo presets, tempo mode, ISO 639-2 metadata)."""

from __future__ import annotations

from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from wavesync.core.i18n import _  # noqa: E402
from wavesync.core.languages import ALL_LANGUAGES, Language, find_language, search_languages  # noqa: E402
from wavesync.core.models import TempoMode  # noqa: E402
from wavesync.core.tempo import PRESETS  # noqa: E402
from wavesync.ui.player import ABPlayerController  # noqa: E402
from wavesync.ui.state import AppState  # noqa: E402


def _fmt_pos(sec: float) -> str:
    sec = max(0.0, float(sec))
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class CompareView(Gtk.Box):
    """A/B comparator player and manual calibration controls."""

    def __init__(
        self,
        state: AppState,
        on_toast: Callable[[str], None],
        on_go_export: Callable[[], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.state = state
        self._on_toast = on_toast
        self._on_go_export = on_go_export
        self._updating_controls = False
        self._filtered_languages: list[Language] = list(ALL_LANGUAGES)
        self._player_window: Gtk.Window | None = None

        self.set_focusable(True)
        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.connect("key-pressed", self._on_key_pressed)
        self.add_controller(key_ctrl)

        self.player = ABPlayerController(
            on_state_update=self._on_player_updated,
            on_status_message=self._on_toast,
        )
        self._batch_movie_labels: list[str] | None = None
        self._compare_a_labels: list[str] | None = None
        self._compare_b_labels: list[str] | None = None

        scroller = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        self.append(scroller)

        clamp = Adw.Clamp(maximum_size=980, tightening_threshold=720)
        scroller.set_child(clamp)

        main_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=16,
            margin_top=16,
            margin_bottom=24,
            margin_start=16,
            margin_end=16,
        )
        clamp.set_child(main_box)

        # --- 0. Batch Movie Selector (visible in batch mode) ---
        self.batch_group = Adw.PreferencesGroup(
            title=_("Película activa del lote"),
            description=_(
                "Selecciona qué película del lote quieres previsualizar y ajustar. "
                "Los ajustes de cada película se guardan de forma individual."
            ),
        )
        self.batch_group.set_visible(False)
        main_box.append(self.batch_group)

        self.combo_batch_movie = Adw.ComboRow(
            title=_("Película en el comparador A/B"),
            subtitle=_("Cambia entre las películas del lote"),
        )
        self.combo_batch_movie.connect("notify::selected", self._on_batch_movie_selected)

        nav_batch_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, valign=Gtk.Align.CENTER)
        self.btn_batch_prev = Gtk.Button(label=_("◀ Anterior"))
        self.btn_batch_prev.add_css_class("flat")
        self.btn_batch_prev.connect(
            "clicked",
            lambda _b: GLib.idle_add(self.state.select_batch_entry, self.state.active_batch_idx - 1),
        )
        nav_batch_box.append(self.btn_batch_prev)

        self.btn_batch_next = Gtk.Button(label=_("Siguiente ▶"))
        self.btn_batch_next.add_css_class("flat")
        self.btn_batch_next.connect(
            "clicked",
            lambda _b: GLib.idle_add(self.state.select_batch_entry, self.state.active_batch_idx + 1),
        )
        nav_batch_box.append(self.btn_batch_next)
        self.combo_batch_movie.add_suffix(nav_batch_box)
        self.batch_group.add(self.combo_batch_movie)

        # --- 1. Video Picture & Transport Bar ---
        self.player_card = Gtk.Frame()
        self.player_vbox = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=8,
            hexpand=True,
            vexpand=True,
        )
        self.player_card.set_child(self.player_vbox)
        main_box.append(self.player_card)

        # Placeholder shown inside player_card while player_vbox is popped out
        self._popout_placeholder = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=10,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
            margin_top=28,
            margin_bottom=28,
        )
        lbl_pop = Gtk.Label(label=_("El reproductor y sus controles están abiertos en una ventana ampliada."))
        lbl_pop.add_css_class("dim-label")
        self._popout_placeholder.append(lbl_pop)
        btn_restore = Gtk.Button(label=_("Contraer reproductor aquí"), halign=Gtk.Align.CENTER)
        btn_restore.add_css_class("pill")
        btn_restore.connect("clicked", lambda _b: self._toggle_player_window())
        self._popout_placeholder.append(btn_restore)

        self.picture = Gtk.Picture(hexpand=True, vexpand=True)
        self.picture.set_size_request(-1, 180)
        if self.player.paintable is not None:
            self.picture.set_paintable(self.player.paintable)
        self.source_picture = Gtk.Picture(hexpand=True, vexpand=True)
        self.source_picture.set_size_request(-1, 180)
        if self.player.source_paintable is not None:
            self.source_picture.set_paintable(self.player.source_paintable)
        self.source_picture.set_visible(False)
        self.video_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, vexpand=True, hexpand=True)
        self.video_box.append(self.picture)
        self.video_box.append(self.source_picture)
        self.player_vbox.append(self.video_box)

        # Timeline seek scale + timestamps
        self.seek_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
            margin_start=12,
            margin_end=12,
        )
        self.lbl_pos = Gtk.Label(label="00:00 / 00:00")
        self.lbl_pos.add_css_class("numeric")
        self.seek_row.append(self.lbl_pos)

        self.scale_seek = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.0, 100.0, 1.0)
        self.scale_seek.set_hexpand(True)
        self.scale_seek.set_draw_value(False)
        self.scale_seek.connect("change-value", self._on_seek_slider)
        self.seek_row.append(self.scale_seek)
        self.player_vbox.append(self.seek_row)

        # Transport & A/B buttons bar
        self.transport = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
            margin_bottom=10,
            margin_start=12,
            margin_end=12,
        )
        self.btn_play = Gtk.Button(label=_("Reproducir (Espacio)"))
        self.btn_play.add_css_class("suggested-action")
        self.btn_play.connect("clicked", lambda _b: self.player.toggle_play())
        self.transport.append(self.btn_play)

        spacer = Gtk.Box(hexpand=True)
        self.transport.append(spacer)
        self.btn_ab = Gtk.Button(label=_("Cambiar audio A/B — [Tab / B]"))
        self.btn_ab.connect("clicked", lambda _b: self._toggle_ab())
        self.transport.append(self.btn_ab)
        self.btn_side_by_side = Gtk.ToggleButton(label=_("Vídeo lado a lado"))
        self.btn_side_by_side.connect("toggled", self._on_side_by_side_toggled)
        self.transport.append(self.btn_side_by_side)
        self.btn_popout = Gtk.Button(label=_("Ampliar reproductor"))
        self.btn_popout.connect("clicked", lambda _b: self._toggle_player_window())
        self.transport.append(self.btn_popout)

        self.player_vbox.append(self.transport)

        # Active tracks & Fallback clip group
        fallback_group = Adw.PreferencesGroup(
            title=_("Pistas activas en el comparador A/B"),
            description=_(
                "Puedes cambiar aquí mismo qué pista del Archivo B quieres escuchar y sincronizar."
            ),
        )
        main_box.append(fallback_group)

        self.combo_compare_b = Adw.ComboRow(
            title=_("Pista de Audio B (sincronizado)"),
            subtitle=_("Pista de la fuente B que se reproduce al pulsar Tab / B"),
        )
        self.combo_compare_b.connect("notify::selected", self._on_compare_b_track_selected)
        fallback_group.add(self.combo_compare_b)

        self.combo_compare_a = Adw.ComboRow(
            title=_("Pista de Audio A (máster original)"),
            subtitle=_("Pista del Archivo A que se reproduce como referencia original"),
        )
        self.combo_compare_a.connect("notify::selected", self._on_compare_a_track_selected)
        fallback_group.add(self.combo_compare_a)

        self.row_fallback = Adw.ActionRow(
            title=_("Modo clip pre-renderizado de 20 s (fallback)"),
            subtitle=_(
                "Pre-renderiza con FFmpeg un fragmento de 20 s con ambas pistas si el contenedor da "
                "problemas de seek en vivo."
            ),
        )
        self.switch_fallback = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.switch_fallback.connect("notify::active", self._on_fallback_toggled)
        self.row_fallback.add_suffix(self.switch_fallback)
        btn_ext = Gtk.Button(label=_("Abrir clip en reproductor externo"), valign=Gtk.Align.CENTER)
        btn_ext.connect("clicked", self._on_open_external_clip)
        self.row_fallback.add_suffix(btn_ext)
        fallback_group.add(self.row_fallback)

        # --- 2. Manual Calibration Group (Offset + Tempo + Mode) ---
        cal_group = Adw.PreferencesGroup(
            title=_("Calibración manual de sincronización"),
            description=_(
                "Convención: «tempo» es el multiplicador de velocidad aplicado al audio B "
                "(t_A = t_B / tempo + offset). Offset > 0 retrasa B insertando silencio."
            ),
        )
        main_box.append(cal_group)

        self.spin_offset = Adw.SpinRow.new_with_range(-600000.0, 600000.0, 0.5)
        self.spin_offset.set_digits(1)
        self.spin_offset.set_title(_("Offset de sincronización (ms)"))
        self.spin_offset.set_subtitle(
            _("Rango −600 000 a +600 000 ms (paso 0.5 ms). Positivo = B entra más tarde.")
        )
        self.spin_offset.connect("notify::value", self._on_offset_changed)
        cal_group.add(self.spin_offset)

        self.row_use_segments = Adw.ActionRow(
            title=_("Sincronización multi-tramo por zonas"),
            subtitle=_("Aplica el ajuste fino de cada tramo detectado a lo largo de la película"),
        )
        self.switch_segments = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.switch_segments.connect("notify::active", self._on_use_segments_toggled)
        self.row_use_segments.add_suffix(self.switch_segments)
        self.row_use_segments.set_activatable_widget(self.switch_segments)
        cal_group.add(self.row_use_segments)

        self.spin_tempo = Adw.SpinRow.new_with_range(0.500000, 2.000000, 0.000010)
        self.spin_tempo.set_digits(6)
        self.spin_tempo.set_title(_("Factor de tempo (6 decimales)"))
        self.spin_tempo.set_subtitle(
            _("Multiplicador sobre la velocidad del audio B (1.000000 = sin cambio).")
        )
        self.spin_tempo.connect("notify::value", self._on_tempo_changed)
        cal_group.add(self.spin_tempo)

        preset_labels = [_("Sin conversión (1.000000)")] + [
            f"{p.key} ({p.tempo:.6f})" for p in PRESETS
        ]
        self.combo_preset = Adw.ComboRow(
            title=_("Preajustes de conversión de framerate"),
            subtitle=_("Aplica factores exactos para conversiones NTSC / Cine / PAL"),
            model=Gtk.StringList.new(preset_labels),
        )
        self.combo_preset.connect("notify::selected", self._on_preset_selected)
        cal_group.add(self.combo_preset)

        self.combo_tempo_mode = Adw.ComboRow(
            title=_("Modo de procesamiento del tempo"),
            subtitle=_(
                "«atempo» conserva el tono original; «asetrate+aresample» varía el tono "
                "(ideal para corregir el +4 % de tono de doblajes PAL acelerados)."
            ),
            model=Gtk.StringList.new(
                [
                    _("Conservar tono (FFmpeg atempo)"),
                    _("Corregir tono PAL (FFmpeg asetrate + aresample)"),
                ]
            ),
        )
        self.combo_tempo_mode.connect("notify::selected", self._on_tempo_mode_changed)
        cal_group.add(self.combo_tempo_mode)

        # --- 3. Track Metadata Group (ISO 639-2 searchable + Free-form + Unicode title) ---
        meta_group = Adw.PreferencesGroup(
            title=_("Metadatos de la pista sincronizada"),
            description=_(
                "Lista completa ISO 639-2/B y /T (487 idiomas con nombre en español y endónimo) "
                "más campo libre y título en cualquier alfabeto Unicode."
            ),
        )
        main_box.append(meta_group)

        # Search row
        search_row = Adw.ActionRow(
            title=_("Buscar idioma (nombre en español, idioma nativo o código)"),
        )
        self.entry_lang_search = Gtk.SearchEntry(
            placeholder_text=_("Ej.: castellano, 日本語, العربية, fra, eng…"),
            valign=Gtk.Align.CENTER,
            hexpand=True,
        )
        self.entry_lang_search.connect("search-changed", self._on_lang_search_changed)
        search_row.add_suffix(self.entry_lang_search)
        meta_group.add(search_row)

        self.combo_lang = Adw.ComboRow(
            title=_("Idioma ISO 639-2 (/B y /T)"),
            subtitle=_("Selecciona un idioma de la lista filtrada"),
        )
        self._populate_language_combo("")
        self.combo_lang.connect("notify::selected", self._on_lang_combo_selected)
        meta_group.add(self.combo_lang)

        free_code_row = Adw.ActionRow(
            title=_("Código de idioma (ISO 639-2 o libre)"),
            subtitle=_("Puedes escribir cualquier código de 3 letras o etiqueta personalizada"),
        )
        self.entry_lang_code = Gtk.Entry(
            text="spa",
            valign=Gtk.Align.CENTER,
            width_chars=10,
        )
        self.entry_lang_code.connect("changed", self._on_lang_code_edited)
        free_code_row.add_suffix(self.entry_lang_code)
        meta_group.add(free_code_row)

        title_row = Adw.ActionRow(
            title=_("Título de la pista"),
            subtitle=_("Soporta cualquier script Unicode: «Castellano», «日本語», «Français (VF)», «العربية»…"),
        )
        self.entry_title = Gtk.Entry(
            placeholder_text=_("Castellano"),
            valign=Gtk.Align.CENTER,
            hexpand=True,
        )
        self.entry_title.connect("changed", self._on_title_edited)
        title_row.add_suffix(self.entry_title)
        meta_group.add(title_row)

        # Proceed to Export button
        btn_export = Gtk.Button(
            label=_("Continuar a Exportación →"),
            halign=Gtk.Align.CENTER,
            margin_top=8,
        )
        btn_export.add_css_class("pill")
        btn_export.add_css_class("suggested-action")
        btn_export.connect("clicked", lambda _b: self._on_go_export())
        main_box.append(btn_export)

        self._expert_groups = (fallback_group, cal_group)
        self.state.subscribe(self._on_state_changed)
        self._update_expert_visibility()
        self._refresh_batch_selector()
        self._sync_from_state()

    def _update_expert_visibility(self) -> None:
        for group in self._expert_groups:
            group.set_visible(self.state.expert_mode)

    def _refresh_batch_selector(self) -> None:
        if not self.state.batch_enabled:
            self.batch_group.set_visible(False)
            return
        self.batch_group.set_visible(True)
        entries = self.state.batch_entries
        self._updating_controls = True
        try:
            if not entries:
                if self._batch_movie_labels != []:
                    self._batch_movie_labels = []
                    self.combo_batch_movie.set_model(Gtk.StringList.new([_("Sin películas en el lote")]))
                self.combo_batch_movie.set_sensitive(False)
                self.btn_batch_prev.set_sensitive(False)
                self.btn_batch_next.set_sensitive(False)
                return
            labels = []
            for i, e in enumerate(entries):
                status = f" ({e.offset_ms:+.1f} ms)" if e.result is not None else _(" (sin analizar)")
                labels.append(f"#{i + 1}: {e.master_path.name} ⟷ {e.source_path.name}{status}")
            if self._batch_movie_labels != labels:
                self._batch_movie_labels = labels
                self.combo_batch_movie.set_model(Gtk.StringList.new(labels))
            idx = min(max(0, self.state.active_batch_idx), len(labels) - 1)
            if self.combo_batch_movie.get_selected() != idx:
                self.combo_batch_movie.set_selected(idx)
            self.combo_batch_movie.set_sensitive(len(labels) > 1)
            self.btn_batch_prev.set_sensitive(idx > 0)
            self.btn_batch_next.set_sensitive(idx < len(labels) - 1)
        finally:
            self._updating_controls = False

    def _on_batch_movie_selected(self, combo: Adw.ComboRow, _pspec) -> None:
        if self._updating_controls:
            return
        pos = int(combo.get_selected())
        if pos != Gtk.INVALID_LIST_POSITION and 0 <= pos < len(self.state.batch_entries):
            if pos != self.state.active_batch_idx:
                GLib.idle_add(self.state.select_batch_entry, pos)

    def _populate_language_combo(self, query: str) -> None:
        self._filtered_languages = search_languages(query, limit=150 if query else None)
        if not self._filtered_languages:
            self.combo_lang.set_model(Gtk.StringList.new([_("Sin coincidencias (usa el campo libre)")]))
            self.combo_lang.set_sensitive(False)
            return
        labels = [lang.display_name() for lang in self._filtered_languages]
        self.combo_lang.set_model(Gtk.StringList.new(labels))
        self.combo_lang.set_sensitive(True)

    def _on_lang_search_changed(self, entry: Gtk.SearchEntry) -> None:
        self._updating_controls = True
        try:
            self._populate_language_combo(entry.get_text())
        finally:
            self._updating_controls = False
        if self._filtered_languages:
            self._on_lang_combo_selected(self.combo_lang, None)

    def _on_lang_combo_selected(self, combo: Adw.ComboRow, _pspec) -> None:
        if self._updating_controls or not self._filtered_languages:
            return
        idx = int(combo.get_selected())
        if 0 <= idx < len(self._filtered_languages):
            lang = self._filtered_languages[idx]
            self._updating_controls = True
            try:
                self.entry_lang_code.set_text(lang.t_code)
            finally:
                self._updating_controls = False
            self._update_primary_track_meta()

    def _on_lang_code_edited(self, _entry: Gtk.Entry) -> None:
        if self._updating_controls:
            return
        self._update_primary_track_meta()

    def _on_title_edited(self, _entry: Gtk.Entry) -> None:
        if self._updating_controls:
            return
        self._update_primary_track_meta()

    def _update_primary_track_meta(self) -> None:
        code = self.entry_lang_code.get_text().strip() or "und"
        title = self.entry_title.get_text()
        if self.state.track_configs:
            idx = min(self.state.source_audio_idx, len(self.state.track_configs) - 1)
            self.state.track_configs[idx].language = code
            self.state.track_configs[idx].title = title
            if self.state.batch_enabled:
                self.state.save_active_to_batch_entry()
            self.state.notify("metadata")

    def _on_compare_b_track_selected(self, combo: Adw.ComboRow, _pspec) -> None:
        if self._updating_controls:
            return
        pos = int(combo.get_selected())
        if pos != Gtk.INVALID_LIST_POSITION:
            self.state.set_source_track(pos)
            if self.state.source_stream:
                self._on_toast(
                    _("Pista B activa en comparador: {desc}").format(desc=self.state.source_stream.describe())
                )

    def _on_compare_a_track_selected(self, combo: Adw.ComboRow, _pspec) -> None:
        if self._updating_controls:
            return
        pos = int(combo.get_selected())
        if pos != Gtk.INVALID_LIST_POSITION:
            self.state.set_master_track(pos)
            if self.state.master_stream:
                self._on_toast(
                    _("Pista A activa en comparador: {desc}").format(desc=self.state.master_stream.describe())
                )

    def on_tab_activated(self) -> None:
        """Called when switching into the compare tab to ensure media and params are loaded."""
        if self.state.master is not None and self.state.source is not None:
            if (
                self.player._master != self.state.master
                or self.player._source != self.state.source
                or self.player._master_audio_idx != self.state.master_audio_idx
                or self.player._source_audio_idx != self.state.source_audio_idx
            ):
                self.player.load_media(
                    self.state.master,
                    self.state.source,
                    self.state.master_audio_idx,
                    self.state.source_audio_idx,
                )
                self._sync_from_state()
            else:
                self.player.update_sync_params(
                    self.state.offset_seconds,
                    self.state.tempo,
                    self.state.tempo_mode,
                    segments=self.state.active_segments,
                )
                self._sync_from_state()

    def _on_state_changed(self, topic: str) -> None:
        if topic in ("all", "batch", "inputs"):
            self._refresh_batch_selector()
        if topic in ("all", "inputs", "inputs_track") or (
            topic == "batch"
            and (self.player._master != self.state.master or self.player._source != self.state.source)
        ):
            self.player.load_media(
                self.state.master,
                self.state.source,
                self.state.master_audio_idx,
                self.state.source_audio_idx,
            )
            self._sync_from_state()
        elif topic in ("result", "calibration", "manual_calibration"):
            self._refresh_batch_selector()
            if topic != "manual_calibration":
                self._sync_from_state()
        if topic in ("all", "preferences"):
            self._update_expert_visibility()

    def _sync_from_state(self) -> None:
        self._updating_controls = True
        try:
            if self.state.source and self.state.source.audio_streams:
                b_labels = [s.describe() for s in self.state.source.audio_streams]
                if self._compare_b_labels != b_labels:
                    self._compare_b_labels = b_labels
                    self.combo_compare_b.set_model(Gtk.StringList.new(b_labels))
                sel_b = min(self.state.source_audio_idx, len(b_labels) - 1)
                if self.combo_compare_b.get_selected() != sel_b:
                    self.combo_compare_b.set_selected(sel_b)
                self.combo_compare_b.set_sensitive(True)
            else:
                if self._compare_b_labels != []:
                    self._compare_b_labels = []
                    self.combo_compare_b.set_model(Gtk.StringList.new([_("Sin pistas en B")]))
                self.combo_compare_b.set_sensitive(False)

            if self.state.master and self.state.master.audio_streams:
                a_labels = [s.describe() for s in self.state.master.audio_streams]
                if self._compare_a_labels != a_labels:
                    self._compare_a_labels = a_labels
                    self.combo_compare_a.set_model(Gtk.StringList.new(a_labels))
                sel_a = min(self.state.master_audio_idx, len(a_labels) - 1)
                if self.combo_compare_a.get_selected() != sel_a:
                    self.combo_compare_a.set_selected(sel_a)
                self.combo_compare_a.set_sensitive(True)
            else:
                if self._compare_a_labels != []:
                    self._compare_a_labels = []
                    self.combo_compare_a.set_model(Gtk.StringList.new([_("Sin pistas en A")]))
                self.combo_compare_a.set_sensitive(False)

            clamped_ms = max(-600000.0, min(600000.0, round(self.state.offset_ms, 1)))
            self.spin_offset.set_value(clamped_ms)

            segs = self.state.segments if self.state.segments else (
                self.state.result.segments if self.state.result else []
            )
            real_segs = [s for s in segs if not s.is_gap]
            has_multi = len(real_segs) > 1
            self.row_use_segments.set_visible(has_multi)
            self.switch_segments.set_active(bool(self.state.use_segments and has_multi))
            if has_multi:
                self.row_use_segments.set_subtitle(
                    _("Aplicando {n} tramos con ajuste fino por zona").format(n=len(real_segs))
                )

            self.spin_tempo.set_value(self.state.tempo)
            self.combo_tempo_mode.set_selected(
                0 if self.state.tempo_mode == TempoMode.ATEMPO else 1
            )
            if self.state.track_configs:
                idx = min(self.state.source_audio_idx, len(self.state.track_configs) - 1)
                tc = self.state.track_configs[idx]
                self.entry_lang_code.set_text(tc.language)
                self.entry_title.set_text(tc.title)
                found = find_language(tc.language)
                if found and found in self._filtered_languages:
                    self.combo_lang.set_selected(self._filtered_languages.index(found))
        finally:
            self._updating_controls = False
        self.player.update_sync_params(
            self.state.offset_seconds,
            self.state.tempo,
            self.state.tempo_mode,
            segments=self.state.active_segments,
        )
        self._on_player_updated()

    def _on_offset_changed(self, spin: Adw.SpinRow, _pspec) -> None:
        if self._updating_controls:
            return
        new_ms = round(float(spin.get_value()), 1)
        if abs(new_ms - self.state.offset_ms) < 0.05:
            return
        self.state.set_offset_ms(new_ms, notify_topic="manual_calibration")
        self.player.update_sync_params(
            self.state.offset_seconds,
            self.state.tempo,
            self.state.tempo_mode,
            segments=self.state.active_segments,
        )

    def _on_use_segments_toggled(self, switch: Gtk.Switch, _pspec) -> None:
        if self._updating_controls:
            return
        self.state.use_segments = bool(switch.get_active())
        if self.state.batch_enabled:
            self.state.save_active_to_batch_entry()
        self.player.update_sync_params(
            self.state.offset_seconds,
            self.state.tempo,
            self.state.tempo_mode,
            segments=self.state.active_segments,
        )
        self.state.notify("manual_calibration")

    def _on_tempo_changed(self, spin: Adw.SpinRow, _pspec) -> None:
        if self._updating_controls:
            return
        new_tempo = float(spin.get_value())
        if abs(new_tempo - self.state.tempo) < 1e-7:
            return
        self.state.tempo = new_tempo
        self.state.use_segments = False
        if self.state.batch_enabled:
            self.state.save_active_to_batch_entry()
        self.player.update_sync_params(
            self.state.offset_seconds,
            self.state.tempo,
            self.state.tempo_mode,
            segments=self.state.active_segments,
        )
        self.state.notify("manual_calibration")

    def _on_preset_selected(self, combo: Adw.ComboRow, _pspec) -> None:
        if self._updating_controls:
            return
        sel = int(combo.get_selected())
        if sel == 0:
            val = 1.0
        elif 1 <= sel <= len(PRESETS):
            val = PRESETS[sel - 1].tempo
        else:
            return
        self.spin_tempo.set_value(val)

    def _on_tempo_mode_changed(self, combo: Adw.ComboRow, _pspec) -> None:
        if self._updating_controls:
            return
        self.state.tempo_mode = (
            TempoMode.ATEMPO if int(combo.get_selected()) == 0 else TempoMode.ASETRATE
        )
        if self.state.batch_enabled:
            self.state.save_active_to_batch_entry()
        self.player.update_sync_params(
            self.state.offset_seconds,
            self.state.tempo,
            self.state.tempo_mode,
            segments=self.state.active_segments,
        )
        self.state.notify("manual_calibration")

    def _toggle_ab(self) -> None:
        branch = self.player.toggle_branch()
        self._on_player_updated()
        b_desc = self.state.source_stream.describe() if self.state.source_stream else "B"
        a_desc = self.state.master_stream.describe() if self.state.master_stream else "A"
        self._on_toast(
            _("Reproduciendo Audio B sincronizado ({desc})").format(desc=b_desc)
            if branch == "B"
            else _("Reproduciendo Audio A original ({desc})").format(desc=a_desc)
        )

    def _on_jump_fraction(self, _btn: Gtk.Button, frac: float) -> None:
        dur = self.player.duration()
        if dur > 0:
            self.player.seek(dur * frac)

    def _on_seek_slider(self, _scale: Gtk.Scale, _scroll: Gtk.ScrollType, value: float) -> bool:
        dur = self.player.duration()
        if dur > 0:
            self.player.seek((max(0.0, min(100.0, value)) / 100.0) * dur)
        return False

    def _on_fallback_toggled(self, switch: Gtk.Switch, _pspec) -> None:
        self.player.use_clip_fallback = bool(switch.get_active())
        if self.player.use_clip_fallback:
            self.player.clip_start_a = self.player.position()
            self.player.regenerate_preview_clip()
        else:
            self.player.load_media(
                self.state.master,
                self.state.source,
                self.state.master_audio_idx,
                self.state.source_audio_idx,
            )

    def _on_open_external_clip(self, _btn: Gtk.Button) -> None:
        self.player.clip_start_a = self.player.position()

        def launch(path) -> None:
            Gio.AppInfo.launch_default_for_uri(path.resolve().as_uri(), None)

        self.player.regenerate_preview_clip(on_ready=launch)

    def _on_side_by_side_toggled(self, button: Gtk.ToggleButton) -> None:
        enabled = button.get_active()
        self.player.set_side_by_side(enabled)
        self.source_picture.set_visible(enabled)
        self.picture.set_hexpand(not enabled)
        self.source_picture.set_hexpand(enabled)

    def _toggle_player_window(self) -> None:
        GLib.idle_add(self._toggle_player_window_idle)

    def _toggle_player_window_idle(self) -> bool:
        if self._player_window is not None:
            self._close_player_window()
        else:
            self._open_player_window()
        return GLib.SOURCE_REMOVE

    def _open_player_window(self) -> None:
        if self._player_window is not None:
            self._player_window.present()
            return

        root = self.get_root()
        window = Gtk.Window(
            title=_("Reproductor A/B — WaveSync"),
            transient_for=root if isinstance(root, Gtk.Window) else None,
        )
        window.set_default_size(1280, 720)

        win_key_ctrl = Gtk.EventControllerKey()
        win_key_ctrl.connect("key-pressed", self._on_key_pressed)
        window.add_controller(win_key_ctrl)

        # Move player_vbox (video + seek bar + transport controls) from player_card into the window
        self.player_card.set_child(self._popout_placeholder)
        self.btn_popout.set_label(_("Contraer reproductor"))
        window.set_child(self.player_vbox)
        window.connect("close-request", self._on_player_window_close_request)
        self._player_window = window
        window.present()

    def _close_player_window(self) -> None:
        if self._player_window is not None:
            self._player_window.close()

    def _on_player_window_close_request(self, window: Gtk.Window) -> bool:
        if window.get_child() is self.player_vbox:
            window.set_child(None)
        if self.player_card.get_child() is not self.player_vbox:
            self.player_card.set_child(self.player_vbox)
        self.btn_popout.set_label(_("Ampliar reproductor"))
        self._player_window = None
        return False

    def _on_player_updated(self) -> None:
        dur = self.player.duration()
        pos = self.player.position()
        self.lbl_pos.set_text(f"{_fmt_pos(pos)} / {_fmt_pos(dur)}")
        if dur > 0:
            self.scale_seek.set_value((pos / dur) * 100.0)
        self.btn_play.set_label(
            _("Pausar (Espacio)") if self.player.playing else _("Reproducir (Espacio)")
        )
        if self.player.active_branch == "B":
            self.btn_ab.set_label(_("Cambiar audio A/B · B activo"))
            self.btn_ab.add_css_class("accent")
        else:
            self.btn_ab.set_label(_("Cambiar audio A/B · A activo"))
            self.btn_ab.remove_css_class("accent")

    def _on_key_pressed(
        self, _ctrl: Gtk.EventControllerKey, keyval: int, _keycode: int, _state: Gdk.ModifierType
    ) -> bool:
        root = self.get_root()
        focus = root.get_focus() if root else None
        if isinstance(focus, (Gtk.Editable, Gtk.Entry, Gtk.SearchEntry)):
            return False
        if keyval == Gdk.KEY_space:
            self.player.toggle_play()
            return True
        if keyval in (Gdk.KEY_Tab, Gdk.KEY_ISO_Left_Tab, Gdk.KEY_b, Gdk.KEY_B):
            self._toggle_ab()
            return True
        if keyval == Gdk.KEY_Left:
            self.player.seek(self.player.position() - 5.0)
            return True
        if keyval == Gdk.KEY_Right:
            self.player.seek(self.player.position() + 5.0)
            return True
        return False
