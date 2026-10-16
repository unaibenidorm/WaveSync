"""Phase 4 view: Export configuration, multi-track B selection, overwrite confirmation, live progress and post-export verification."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk, Pango  # noqa: E402

from wavesync.core.errors import WaveSyncError  # noqa: E402
from wavesync.core.batch import BatchItem, BatchOptions, process_batch  # noqa: E402
from wavesync.core.export import (  # noqa: E402
    VerificationReport,
    can_direct_copy,
    default_bitrate_kbps,
    export,
)
from wavesync.core.i18n import _  # noqa: E402
from wavesync.core.models import AudioStream, Container, ExportJob, ExportTrack  # noqa: E402
from wavesync.core.probe import probe  # noqa: E402
from wavesync.ui.preferences import UserPreferences  # noqa: E402
from wavesync.ui.state import AppState, TrackSelection  # noqa: E402
from wavesync.ui.workers import BackgroundTask, run_in_background  # noqa: E402

_CODEC_KEYS = ("aac", "ac3", "eac3", "flac", "opus", "copy")
_CODEC_LABELS = (
    "AAC",
    "AC-3 / Dolby Digital",
    "E-AC-3 / Dolby Digital Plus",
    "FLAC",
    "Opus",
    "Copia directa",
)


def _dropdown_with_full_item_tooltips(labels: tuple[str, ...] | list[str]) -> Gtk.DropDown:
    """Create a dropdown whose popup items expose their complete visible label."""
    model = Gtk.StringList.new(list(labels))
    dropdown = Gtk.DropDown(model=model, valign=Gtk.Align.CENTER)
    factory = Gtk.SignalListItemFactory()

    def setup(_factory: Gtk.SignalListItemFactory, list_item: Gtk.ListItem) -> None:
        label = Gtk.Label(xalign=0.0)
        list_item.set_child(label)

    def bind(_factory: Gtk.SignalListItemFactory, list_item: Gtk.ListItem) -> None:
        label = list_item.get_child()
        item = list_item.get_item()
        if isinstance(label, Gtk.Label) and isinstance(item, Gtk.StringObject):
            text = item.get_string()
            label.set_text(text)
            label.set_tooltip_text(text)

    factory.connect("setup", setup)
    factory.connect("bind", bind)
    dropdown.set_factory(factory)
    dropdown.set_list_factory(factory)
    return dropdown


def detect_preferred_codec(stream: AudioStream | None) -> str:
    """Choose the best default export codec matching the source audio stream."""
    if not stream or not stream.codec:
        return "aac"
    codec_name = stream.codec.lower().strip()
    if codec_name in ("ac3", "ac-3", "dolby digital"):
        return "ac3"
    if codec_name in ("eac3", "e-ac3", "ec-3", "dolby digital plus"):
        return "eac3"
    if codec_name in ("flac", "alac", "pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_f32le", "wav", "truehd"):
        return "flac"
    if codec_name in ("opus",):
        return "opus"
    if codec_name in ("aac", "mp4a-latm"):
        return "aac"
    if codec_name in ("dts", "dca", "dtshd"):
        return "ac3"
    if stream.channels > 2:
        return "ac3"
    return "aac"


class ExportView(Gtk.Box):
    """View for configuring and running MKV/MP4 export with post-mux ffprobe verification."""

    def __init__(
        self,
        state: AppState,
        on_error: Callable[[WaveSyncError], None],
        on_toast: Callable[[str], None],
        preferences: UserPreferences | None = None,
        on_export_start: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.state = state
        self._on_error = on_error
        self._on_toast = on_toast
        self._preferences = preferences
        self._on_export_start = on_export_start
        self._task: BackgroundTask | None = None
        self._track_rows: list[Gtk.Widget] = []
        self._batch_rows: list[Gtk.Widget] = []
        self._output_path: Path | None = None
        self._batch_output_dir: Path | None = None
        self._master_selection_master: Path | None = None
        self._master_audio_indices: set[int] = set()
        self._subtitle_paths: list[Path] = []
        self._user_customized_codec = False
        self._selecting_default_codec = False

        # MP4 container warning banner
        self.mp4_banner = Adw.Banner(
            title=_(
                "Aviso: MP4 tiene limitaciones con subtítulos (ASS/SRT/PGS), capítulos y códecs como FLAC. "
                "Se recomienda usar MKV salvo que necesites MP4 explícitamente."
            ),
            revealed=False,
        )
        self.append(self.mp4_banner)

        page = Adw.PreferencesPage(vexpand=True)
        self.append(page)

        # --- 0. Batch Movies Overview Group (visible in batch mode) ---
        self.batch_group = Adw.PreferencesGroup(
            title=_("Películas del lote a exportar"),
            description=_(
                "Cada película se exportará con su propio offset, tempo, tramos y pistas configuradas. "
                "Pulsa «Configurar pistas» en cualquier película para editar sus pistas abajo."
            ),
        )
        self.batch_group.set_visible(False)
        page.add(self.batch_group)

        # --- 1. Container & Codec Group ---
        fmt_group = Adw.PreferencesGroup(
            title=_("Contenedor y codificación de audio"),
            description=_(
                "El vídeo, los subtítulos y los capítulos del Archivo A se copian siempre bit a bit "
                "sin recodificar (-c:v copy -c:s copy)."
            ),
        )
        page.add(fmt_group)

        self.combo_container = _dropdown_with_full_item_tooltips(
            [
                _("Matroska (.mkv)"),
                _("MP4 (.mp4)"),
            ]
        )
        self.combo_container.set_selected(0)
        self.combo_container.set_tooltip_text(
            _("Matroska (.mkv): conserva vídeo, múltiples audios, subtítulos y capítulos.")
        )
        container_row = Adw.ActionRow(
            title=_("Contenedor de salida"),
            subtitle=_("MKV por defecto (conserva todos los formatos de vídeo, subtítulos y metadatos)"),
        )
        container_row.add_suffix(self.combo_container)
        self.combo_container.connect("notify::selected", self._on_container_changed)
        fmt_group.add(container_row)

        self.combo_codec = _dropdown_with_full_item_tooltips(_CODEC_LABELS)
        codec_row = Adw.ActionRow(
            title=_("Códec para las nuevas pistas de audio"),
            subtitle=_(
                "Por defecto usa el códec de la pista B seleccionada cuando es compatible; "
                "conserva el layout de canales (p. ej. 5.1)"
            ),
        )
        self.combo_codec.set_selected(0)
        self.combo_codec.set_tooltip_text(
            _("Códecs disponibles: AAC, AC-3, E-AC-3, FLAC, Opus o copia directa cuando tempo = 1.0.")
        )
        self.combo_codec.connect("notify::selected", self._on_codec_changed)
        codec_row.add_suffix(self.combo_codec)
        fmt_group.add(codec_row)

        self.spin_bitrate = Adw.SpinRow.new_with_range(64.0, 1536.0, 32.0)
        self.spin_bitrate.set_digits(0)
        self.spin_bitrate.set_title(_("Bitrate de audio (kbps)"))
        self.spin_bitrate.set_subtitle(_("Ignorado en FLAC (sin pérdidas) y en copia directa"))
        self.spin_bitrate.set_value(192.0)
        fmt_group.add(self.spin_bitrate)

        self.row_keep_audio = Adw.ActionRow(
            title=_("Conservar pistas de audio originales del máster A"),
            subtitle=_("Se copian sin recodificar (-c:a copy) junto a las nuevas pistas de B"),
        )
        self.switch_keep_audio = Gtk.Switch(active=True, valign=Gtk.Align.CENTER)
        self.switch_keep_audio.connect("notify::active", self._on_keep_audio_switched)
        self.row_keep_audio.add_suffix(self.switch_keep_audio)
        fmt_group.add(self.row_keep_audio)

        self.row_use_segments = Adw.ActionRow(
            title=_("Construir pista por tramos de montaje"),
            subtitle=_("Recorta, concatena y rellena con silencio según los tramos detectados en la Etapa 3"),
        )
        self.switch_use_segments = Gtk.Switch(active=False, valign=Gtk.Align.CENTER)
        self.switch_use_segments.connect("notify::active", self._on_use_segments_switched)
        self.row_use_segments.add_suffix(self.switch_use_segments)
        fmt_group.add(self.row_use_segments)

        self.master_audio_group = Adw.PreferencesGroup(
            title=_("Pistas de audio originales del máster"),
            description=_("Desmarca las pistas que no quieras conservar en el archivo final."),
        )
        page.add(self.master_audio_group)
        self._master_audio_rows: list[Gtk.Widget] = []

        # --- 2. Multi-track B selection group ---
        self.tracks_group = Adw.PreferencesGroup(
            title=_("Pistas a sincronizar e incluir en la exportación"),
            description=_(
                "Puedes añadir varias pistas del mismo archivo o de archivos adicionales con los mismos "
                "parámetros de sincronización."
            ),
        )
        page.add(self.tracks_group)

        self.subtitle_group = Adw.PreferencesGroup(
            title=_("Subtítulos adicionales"),
            description=_("Añade archivos SRT, ASS, WebVTT o SUP al contenedor exportado."),
        )
        page.add(self.subtitle_group)
        self._subtitle_rows: list[Gtk.Widget] = []

        # --- 3. Output destination & Export execution ---
        out_group = Adw.PreferencesGroup(
            title=_("Archivo de destino y exportación"),
            description=_("Se escribe en un archivo temporal oculto y se renombra atómicamente al terminar."),
        )
        page.add(out_group)

        self.row_dest = Adw.ActionRow(
            title=_("Archivo de salida"),
            subtitle=_("Ningún destino seleccionado"),
        )
        btn_choose_dest = Gtk.Button(label=_("Elegir destino…"), valign=Gtk.Align.CENTER)
        btn_choose_dest.connect("clicked", self._on_choose_destination)
        self.row_dest.add_suffix(btn_choose_dest)
        self.row_dest.set_activatable_widget(btn_choose_dest)
        out_group.add(self.row_dest)

        action_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=10, margin_bottom=6)
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, halign=Gtk.Align.CENTER)

        self.btn_export = Gtk.Button(label=_("Exportar y verificar con ffprobe"))
        self.btn_export.add_css_class("pill")
        self.btn_export.add_css_class("suggested-action")
        self.btn_export.set_sensitive(False)
        self.btn_export.connect("clicked", self._on_export_clicked)
        btn_box.append(self.btn_export)

        self.btn_cancel = Gtk.Button(label=_("Cancelar exportación"))
        self.btn_cancel.add_css_class("pill")
        self.btn_cancel.add_css_class("destructive-action")
        self.btn_cancel.set_visible(False)
        self.btn_cancel.connect("clicked", self._on_cancel_clicked)
        btn_box.append(self.btn_cancel)

        action_box.append(btn_box)

        self.progress_bar = Gtk.ProgressBar(show_text=True, visible=False)
        action_box.append(self.progress_bar)
        out_group.add(action_box)

        # --- 4. Post-export verification report ---
        self.verify_group = Adw.PreferencesGroup(
            title=_("Verificación automática post-exportación (ffprobe)"),
        )
        page.add(self.verify_group)
        self.row_verify = Adw.ActionRow(
            title=_("Aún no se ha exportado ningún archivo"),
            subtitle=_("Al terminar la exportación se comprobarán vídeo, pistas, idioma, título y duración."),
        )
        self.verify_group.add(self.row_verify)

        self._expert_widgets = (
            self.spin_bitrate,
            self.row_keep_audio,
            self.row_use_segments,
        )
        self.state.subscribe(self._on_state_changed)
        self._update_expert_visibility()
        self._refresh_from_state()

    def _on_state_changed(self, topic: str) -> None:
        if topic == "inputs":
            self._output_path = None
            if not self.state.batch_enabled:
                self._user_customized_codec = False
                self._batch_output_dir = None
            self._select_default_codec_for_source()
        elif topic in ("inputs_track", "all"):
            if not self._user_customized_codec:
                self._select_default_codec_for_source()
        if topic in (
            "all",
            "inputs",
            "inputs_track",
            "result",
            "calibration",
            "manual_calibration",
            "metadata",
            "batch",
        ):
            self._refresh_from_state()
        if topic in ("all", "preferences"):
            self._update_expert_visibility()

    def _select_default_codec_for_source(self) -> None:
        if not self.state.source_stream:
            return
        target_codec = detect_preferred_codec(self.state.source_stream)
        if target_codec in _CODEC_KEYS:
            idx = _CODEC_KEYS.index(target_codec)
            if int(self.combo_codec.get_selected()) != idx:
                self._selecting_default_codec = True
                try:
                    self.combo_codec.set_selected(idx)
                finally:
                    self._selecting_default_codec = False
            self.state.codec = target_codec

    def _update_expert_visibility(self) -> None:
        for widget in self._expert_widgets:
            widget.set_visible(self.state.expert_mode)

    def _on_use_segments_switched(self, switch: Gtk.Switch, _pspec) -> None:
        self.state.use_segments = bool(switch.get_active())
        if self.state.batch_enabled:
            self.state.save_active_to_batch_entry()

    def _refresh_from_state(self) -> None:
        if not self._user_customized_codec and self.state.source_stream:
            self._select_default_codec_for_source()

        if self.state.batch_enabled:
            self.row_dest.set_title(_("Carpeta de salida del lote"))
            if self._batch_output_dir is None and self.state.batch_masters:
                self._batch_output_dir = self.state.batch_masters[0].parent
            if self._batch_output_dir is not None:
                self.row_dest.set_subtitle(
                    _("Carpeta de salida del lote: {path}").format(path=self._batch_output_dir)
                )
            else:
                self.row_dest.set_subtitle(_("Ninguna carpeta seleccionada"))
            n = len(self.state.batch_entries) or len(self.state.batch_masters)
            self.btn_export.set_label(
                _("Exportar y verificar lote ({n} películas)").format(n=n)
                if n > 0
                else _("Exportar lote")
            )
            if self.state.master:
                self.tracks_group.set_title(
                    _("Pistas a sincronizar — #{i}: {name}").format(
                        i=self.state.active_batch_idx + 1,
                        name=GLib.markup_escape_text(self.state.master.path.name),
                    )
                )
            else:
                self.tracks_group.set_title(_("Pistas a sincronizar e incluir en la exportación"))
        else:
            self.row_dest.set_title(_("Archivo de salida"))
            self.btn_export.set_label(_("Exportar y verificar con ffprobe"))
            self.tracks_group.set_title(_("Pistas a sincronizar e incluir en la exportación"))
            if self.state.master and self._output_path is None:
                ext = ".mp4" if self.state.container == Container.MP4 else ".mkv"
                stem = self.state.master.path.stem
                self._output_path = self.state.master.path.with_name(f"{stem}.wavesync{ext}")
            self._update_dest_label()

        self._rebuild_batch_group()
        self._refresh_master_audio_rows()
        self._refresh_subtitle_rows()

        segs = self.state.segments if self.state.segments else (
            self.state.result.segments if self.state.result else []
        )
        real_segs = [s for s in segs if not s.is_gap]
        has_multi = len(real_segs) > 1
        self.row_use_segments.set_sensitive(has_multi)
        self.switch_use_segments.set_active(self.state.use_segments and has_multi)

        self._rebuild_track_rows()
        self._refresh_export_sensitivity()

    def _rebuild_batch_group(self) -> None:
        for w in self._batch_rows:
            self.batch_group.remove(w)
        self._batch_rows.clear()

        if not self.state.batch_enabled:
            self.batch_group.set_visible(False)
            return

        self.batch_group.set_visible(True)
        entries = self.state.batch_entries
        self.batch_group.set_title(
            _("Películas del lote a exportar ({n})").format(n=len(entries))
        )

        if not entries:
            row = Adw.ActionRow(
                title=_("No hay películas preparadas en el lote"),
                subtitle=_("Vuelve a la pestaña «1. Archivos» y pulsa «Continuar a sincronización por lotes»."),
            )
            self.batch_group.add(row)
            self._batch_rows.append(row)
            return

        row_meta_all = Adw.ActionRow(
            title=_("Unificar metadatos de idioma y título en el lote"),
            subtitle=_(
                "Copia el idioma y título de la película activa actual a todas las demás películas del lote"
            ),
        )
        btn_apply_all = Gtk.Button(label=_("Aplicar a todo el lote"), valign=Gtk.Align.CENTER)
        btn_apply_all.connect("clicked", self._on_apply_metadata_to_all_batch)
        row_meta_all.add_suffix(btn_apply_all)
        self.batch_group.add(row_meta_all)
        self._batch_rows.append(row_meta_all)

        ext = ".mp4" if self.state.container == Container.MP4 else ".mkv"
        for idx, entry in enumerate(entries):
            is_active = idx == self.state.active_batch_idx
            out_name = f"{entry.master_path.stem}.wavesync{ext}"
            real_segs = [s for s in entry.segments if not s.is_gap]
            seg_txt = (
                _(" · {k} tramos").format(k=len(real_segs))
                if (entry.use_segments and len(real_segs) > 1)
                else ""
            )
            enabled_tracks = [tc for tc in entry.track_configs if tc.enabled]
            lang_txt = (
                ", ".join(f"{tc.language}" + (f" ({tc.title})" if tc.title else "") for tc in enabled_tracks)
                if enabled_tracks
                else "spa"
            )
            status = (
                _("Offset: {ms:+.1f} ms · Tempo: {tempo:.6f}{segs} · Idioma: {lang}").format(
                    ms=entry.offset_ms,
                    tempo=entry.tempo,
                    segs=seg_txt,
                    lang=lang_txt,
                )
                if entry.result is not None
                else _("Se analizará automáticamente al exportar · Idioma: {lang}").format(lang=lang_txt)
            )
            row = Adw.ActionRow(
                title=GLib.markup_escape_text(out_name),
                subtitle=GLib.markup_escape_text(
                    f"{entry.master_path.name} ⟷ {entry.source_path.name} — {status}"
                ),
            )
            lbl_num = Gtk.Label(
                label=f"▶ #{idx + 1}" if is_active else f"#{idx + 1}",
                valign=Gtk.Align.CENTER,
            )
            lbl_num.add_css_class("heading")
            if not is_active:
                lbl_num.add_css_class("dim-label")
            row.add_prefix(lbl_num)

            btn_cfg = Gtk.Button(
                label=_("Configurando") if is_active else _("Configurar pistas"),
                valign=Gtk.Align.CENTER,
            )
            btn_cfg.add_css_class("flat")
            btn_cfg.set_sensitive(not is_active)
            btn_cfg.connect("clicked", lambda _b, i=idx: self.state.select_batch_entry(i))
            row.add_suffix(btn_cfg)

            self.batch_group.add(row)
            self._batch_rows.append(row)

    def _on_apply_metadata_to_all_batch(self, _btn: Gtk.Button) -> None:
        self.state.save_active_to_batch_entry()
        active_tcs = [tc for tc in self.state.track_configs if tc.enabled] or self.state.track_configs
        if not active_tcs:
            return
        lang = active_tcs[0].language
        title = active_tcs[0].title
        for entry in self.state.batch_entries:
            for tc in entry.track_configs:
                tc.language = lang
                tc.title = title
        self._rebuild_batch_group()
        self._on_toast(
            _("Metadatos «{lang}» ({title}) aplicados a las {n} películas del lote.").format(
                lang=lang,
                title=title or _("sin título"),
                n=len(self.state.batch_entries),
            )
        )

    def _refresh_master_audio_rows(self) -> None:
        for row in self._master_audio_rows:
            self.master_audio_group.remove(row)
        self._master_audio_rows.clear()
        if not self.state.master or not self.state.master.audio_streams:
            return
        master_path = self.state.master.path.resolve()
        if self._master_selection_master != master_path:
            self._master_selection_master = master_path
            if self.state.master_audio_indices is not None:
                self._master_audio_indices = set(self.state.master_audio_indices)
            else:
                self._master_audio_indices = set(range(len(self.state.master.audio_streams)))
                self.state.master_audio_indices = tuple(sorted(self._master_audio_indices))
        elif self.state.master_audio_indices is not None:
            self._master_audio_indices = set(self.state.master_audio_indices)

        for stream in self.state.master.audio_streams:
            row = Adw.ActionRow(
                title=stream.describe(),
                subtitle=_("{sr} Hz · {lang}").format(
                    sr=stream.sample_rate, lang=stream.language or _("sin idioma")
                ),
            )
            check = Gtk.CheckButton(
                active=stream.position in self._master_audio_indices,
                valign=Gtk.Align.CENTER,
            )
            check.connect("toggled", self._on_master_audio_toggled, stream.position)
            row.add_prefix(check)
            self.master_audio_group.add(row)
            self._master_audio_rows.append(row)

    def _on_master_audio_toggled(self, check: Gtk.CheckButton, position: int) -> None:
        if check.get_active():
            self._master_audio_indices.add(position)
        else:
            self._master_audio_indices.discard(position)
        self.state.master_audio_indices = tuple(sorted(self._master_audio_indices))
        if self.state.batch_enabled:
            self.state.save_active_to_batch_entry()
            self._rebuild_batch_group()
        self._refresh_export_sensitivity()

    def _on_keep_audio_switched(self, switch: Gtk.Switch, _pspec) -> None:
        self.master_audio_group.set_sensitive(switch.get_active())
        if self.state.batch_enabled:
            self.state.save_active_to_batch_entry()
            self._rebuild_batch_group()
        self._refresh_export_sensitivity()

    def _refresh_subtitle_rows(self) -> None:
        for row in self._subtitle_rows:
            self.subtitle_group.remove(row)
        self._subtitle_rows.clear()
        add_row = Adw.ActionRow(
            title=_("Añadir subtítulos"),
            subtitle=_("No se han añadido subtítulos externos") if not self._subtitle_paths
            else _("{n} archivo(s) seleccionado(s)").format(n=len(self._subtitle_paths)),
        )
        button = Gtk.Button(label=_("Añadir archivos…"), valign=Gtk.Align.CENTER)
        button.connect("clicked", self._on_add_subtitles)
        add_row.add_suffix(button)
        self.subtitle_group.add(add_row)
        self._subtitle_rows.append(add_row)
        for path in self._subtitle_paths:
            row = Adw.ActionRow(title=path.name)
            remove = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER)
            remove.add_css_class("flat")
            remove.set_tooltip_text(_("Quitar subtítulo"))
            remove.connect("clicked", lambda _b, item=path: self._remove_subtitle(item))
            row.add_suffix(remove)
            self.subtitle_group.add(row)
            self._subtitle_rows.append(row)

    def _on_add_subtitles(self, _button: Gtk.Button) -> None:
        dialog = Gtk.FileDialog(title=_("Añadir subtítulos"))
        filters = Gio.ListStore.new(Gtk.FileFilter)
        subtitle_filter = Gtk.FileFilter()
        subtitle_filter.set_name(_("Archivos de subtítulos"))
        for pattern in ("*.srt", "*.ass", "*.ssa", "*.vtt", "*.sub", "*.sup"):
            subtitle_filter.add_pattern(pattern)
        filters.append(subtitle_filter)
        dialog.set_filters(filters)
        root = self.get_root()
        dialog.open_multiple(root if isinstance(root, Gtk.Window) else None, None, self._on_subtitles_done)

    def _on_subtitles_done(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        try:
            files = dialog.open_multiple_finish(result)
        except GLib.Error:
            return
        for gfile in files:
            if gfile.get_path():
                path = Path(gfile.get_path())
                if path not in self._subtitle_paths:
                    self._subtitle_paths.append(path)
        self._refresh_subtitle_rows()

    def _remove_subtitle(self, path: Path) -> None:
        if path in self._subtitle_paths:
            self._subtitle_paths.remove(path)
            self._refresh_subtitle_rows()

    def _rebuild_track_rows(self) -> None:
        for w in self._track_rows:
            self.tracks_group.remove(w)
        self._track_rows.clear()

        # Action bar to select all / none / add file
        actions_row = Adw.ActionRow(
            title=_("Pistas a sincronizar"),
            subtitle=_("Selecciona qué pistas de audio incluir en el archivo final exportado"),
        )
        btn_all = Gtk.Button(label=_("Todas"), valign=Gtk.Align.CENTER)
        btn_all.set_tooltip_text(_("Seleccionar todas las pistas"))
        btn_all.connect("clicked", lambda _b: self._on_select_all_tracks(True))
        actions_row.add_suffix(btn_all)

        btn_none = Gtk.Button(label=_("Ninguna"), valign=Gtk.Align.CENTER)
        btn_none.set_tooltip_text(_("Deseleccionar todas las pistas"))
        btn_none.connect("clicked", lambda _b: self._on_select_all_tracks(False))
        actions_row.add_suffix(btn_none)

        btn_add = Gtk.Button(label=_("Añadir otro archivo…"), valign=Gtk.Align.CENTER)
        btn_add.add_css_class("suggested-action")
        btn_add.set_tooltip_text(_("Añadir pistas de audio de otro archivo adicional a sincronizar"))
        btn_add.connect("clicked", self._on_add_additional_audio_file)
        actions_row.add_suffix(btn_add)

        self.tracks_group.add(actions_row)
        self._track_rows.append(actions_row)

        if not self.state.track_configs:
            row = Adw.ActionRow(
                title=_("Sin pistas cargadas"),
                subtitle=_("Selecciona primero el Archivo B en la pestaña Entradas."),
            )
            self.tracks_group.add(row)
            self._track_rows.append(row)
            return

        for tc in self.state.track_configs:
            is_extra_file = bool(
                tc.source and self.state.source and tc.source.path.resolve() != self.state.source.path.resolve()
            )
            file_tag = f"[{tc.source.path.name}] " if (is_extra_file and tc.source) else ""
            desc = tc.stream.describe()
            row = Adw.ActionRow(
                title=GLib.markup_escape_text(f"{file_tag}{desc}"),
                subtitle=_("{sr} Hz · activa la casilla para incluir esta pista").format(
                    sr=tc.stream.sample_rate
                ),
            )
            chk = Gtk.CheckButton(active=tc.enabled, valign=Gtk.Align.CENTER)
            chk.connect("toggled", self._on_track_enabled_toggled, tc)
            row.add_prefix(chk)

            lang_entry = Gtk.Entry(
                text=tc.language,
                placeholder_text="spa",
                width_chars=6,
                valign=Gtk.Align.CENTER,
                tooltip_text=_("Código de idioma ISO 639-2"),
            )
            lang_entry.connect("changed", self._on_track_lang_changed, tc)
            row.add_suffix(lang_entry)

            title_entry = Gtk.Entry(
                text=tc.title,
                placeholder_text=_("Título (p. ej. Castellano)"),
                width_chars=18,
                valign=Gtk.Align.CENTER,
                tooltip_text=_("Título de la pista (Unicode)"),
            )
            title_entry.connect("changed", self._on_track_title_changed, tc)
            row.add_suffix(title_entry)

            if is_extra_file:
                btn_del = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER)
                btn_del.add_css_class("flat")
                btn_del.set_tooltip_text(_("Quitar pista de la lista"))
                btn_del.connect("clicked", lambda _b, t=tc: self._on_remove_track(t))
                row.add_suffix(btn_del)

            self.tracks_group.add(row)
            self._track_rows.append(row)

    def _on_select_all_tracks(self, enabled: bool) -> None:
        self.state.set_all_tracks_enabled(enabled)
        self._rebuild_track_rows()
        self._rebuild_batch_group()
        self._refresh_export_sensitivity()

    def _on_remove_track(self, tc: TrackSelection) -> None:
        self.state.remove_track_config(tc)
        self._rebuild_track_rows()
        self._rebuild_batch_group()
        self._refresh_export_sensitivity()

    def _on_add_additional_audio_file(self, _btn: Gtk.Button) -> None:
        dialog = Gtk.FileDialog(title=_("Añadir archivo con pistas a sincronizar…"))
        filters = Gio.ListStore.new(Gtk.FileFilter)
        f_media = Gtk.FileFilter()
        f_media.set_name(_("Archivos de vídeo y audio"))
        for pat in ("*.mkv", "*.mp4", "*.m4v", "*.avi", "*.mov", "*.webm", "*.ts", "*.m2ts",
                    "*.ac3", "*.eac3", "*.aac", "*.m4a", "*.flac", "*.opus", "*.ogg", "*.wav", "*.mp3"):
            f_media.add_pattern(pat)
        filters.append(f_media)
        f_all = Gtk.FileFilter()
        f_all.set_name(_("Todos los archivos"))
        f_all.add_pattern("*")
        filters.append(f_all)
        dialog.set_filters(filters)

        root = self.get_root()
        parent = root if isinstance(root, Gtk.Window) else None
        dialog.open(parent, None, self._on_add_file_done)

    def _on_add_file_done(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        try:
            gfile = dialog.open_finish(result)
        except GLib.Error:
            return
        if gfile and gfile.get_path():
            path = Path(gfile.get_path())
            try:
                info = probe(path)
                if not info.audio_streams:
                    self._on_toast(_("El archivo no contiene pistas de audio: {name}").format(name=path.name))
                    return
                count = self.state.add_source_tracks_from_media(info)
                self._on_toast(_("Se añadieron {n} pistas de audio de «{name}»").format(n=count, name=path.name))
            except Exception as e:
                self._on_toast(_("Error al leer archivo: {err}").format(err=str(e)))

    def _on_track_enabled_toggled(self, chk: Gtk.CheckButton, tc: TrackSelection) -> None:
        tc.enabled = bool(chk.get_active())
        if self.state.batch_enabled:
            self.state.save_active_to_batch_entry()
            self._rebuild_batch_group()
        self._refresh_export_sensitivity()

    def _on_track_lang_changed(self, entry: Gtk.Entry, tc: TrackSelection) -> None:
        tc.language = entry.get_text().strip() or "und"
        if self.state.batch_enabled:
            self.state.save_active_to_batch_entry()
            self._rebuild_batch_group()

    def _on_track_title_changed(self, entry: Gtk.Entry, tc: TrackSelection) -> None:
        tc.title = entry.get_text()
        if self.state.batch_enabled:
            self.state.save_active_to_batch_entry()
            self._rebuild_batch_group()

    def _on_container_changed(self, combo: Adw.ComboRow, _pspec) -> None:
        is_mp4 = int(combo.get_selected()) == 1
        combo.set_tooltip_text(
            _(
                "MPEG-4 (.mp4) — Opcional: requiere subtítulos mov_text y puede limitar "
                "algunos formatos de audio."
            )
            if is_mp4
            else _("Matroska (.mkv) — Recomendado: conserva vídeo, múltiples audios, subtítulos y capítulos.")
        )
        self.state.container = Container.MP4 if is_mp4 else Container.MKV
        self.mp4_banner.set_revealed(is_mp4)
        if self._output_path is not None:
            new_ext = ".mp4" if is_mp4 else ".mkv"
            self._output_path = self._output_path.with_suffix(new_ext)
            if not self.state.batch_enabled:
                self._update_dest_label()
        if self.state.batch_enabled:
            self._rebuild_batch_group()

    def _on_codec_changed(self, combo: Adw.ComboRow, _pspec) -> None:
        if not self._selecting_default_codec:
            self._user_customized_codec = True
        idx = int(combo.get_selected())
        if 0 <= idx < len(_CODEC_KEYS):
            codec = _CODEC_KEYS[idx]
            combo.set_tooltip_text(_CODEC_LABELS[idx])
            self.state.codec = codec
            ch = self.state.source_stream.channels if self.state.source_stream else 2
            br = default_bitrate_kbps(codec, ch)
            if br is not None:
                self.spin_bitrate.set_value(float(br))
                self.spin_bitrate.set_sensitive(True)
            else:
                self.spin_bitrate.set_sensitive(False)

    def _on_choose_destination(self, _btn: Gtk.Button) -> None:
        if self.state.batch_enabled:
            dialog = Gtk.FileDialog(title=_("Elegir carpeta de salida del lote"))
            if self._batch_output_dir is not None and self._batch_output_dir.is_dir():
                dialog.set_initial_folder(Gio.File.new_for_path(str(self._batch_output_dir)))
            root = self.get_root()
            dialog.select_folder(
                root if isinstance(root, Gtk.Window) else None,
                None,
                self._on_batch_folder_done,
            )
            return
        dialog = Gtk.FileDialog(title=_("Guardar película sincronizada como…"))
        if self._output_path is not None:
            dialog.set_initial_name(self._output_path.name)
            if self._output_path.parent.is_dir():
                dialog.set_initial_folder(Gio.File.new_for_path(str(self._output_path.parent)))
        root = self.get_root()
        parent = root if isinstance(root, Gtk.Window) else None
        dialog.save(parent, None, self._on_save_dialog_done)

    def _on_save_dialog_done(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        try:
            gfile = dialog.save_finish(result)
        except GLib.Error:
            return
        if gfile and gfile.get_path():
            self._output_path = Path(gfile.get_path())
            if self._output_path.suffix.lower() == ".mp4":
                self.combo_container.set_selected(1)
            elif self._output_path.suffix.lower() == ".mkv":
                self.combo_container.set_selected(0)
            self._update_dest_label()
            self._refresh_export_sensitivity()

    def _on_batch_folder_done(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        if folder and folder.get_path():
            self._batch_output_dir = Path(folder.get_path())
            self.row_dest.set_subtitle(
                _("Carpeta de salida del lote: {path}").format(path=self._batch_output_dir)
            )
            self._refresh_export_sensitivity()

    def _update_dest_label(self) -> None:
        if self._output_path is None:
            self.row_dest.set_subtitle(_("Ningún destino seleccionado"))
        else:
            self.row_dest.set_subtitle(GLib.markup_escape_text(str(self._output_path)))

    def _refresh_export_sensitivity(self) -> None:
        running = self._task is not None and self._task.alive
        has_inputs = bool(self.state.master and self.state.source)
        has_tracks = any(tc.enabled for tc in self.state.track_configs)
        has_master_audio = (
            bool(self.switch_keep_audio.get_active())
            and bool(self._master_audio_indices)
        )
        if self.state.batch_enabled:
            ready = (
                (len(self.state.batch_entries) > 0 or (
                    len(self.state.batch_masters) == len(self.state.batch_sources)
                    and bool(self.state.batch_masters)
                ))
                and self._batch_output_dir is not None
            )
        else:
            ready = has_inputs and (has_tracks or has_master_audio) and self._output_path is not None
        self.btn_export.set_sensitive(ready and not running)

    def _build_job(self, *, overwrite: bool) -> ExportJob:
        assert self.state.master is not None
        assert self.state.source is not None
        assert self._output_path is not None

        tracks = [
            ExportTrack(
                stream=tc.stream,
                language=tc.language,
                title=tc.title,
                default=tc.default,
                source=tc.source or self.state.source,
            )
            for tc in self.state.track_configs
            if tc.enabled
        ]
        codec = _CODEC_KEYS[int(self.combo_codec.get_selected())]
        use_segs = bool(self.switch_use_segments.get_active())
        all_segs = self.state.segments if self.state.segments else (
            self.state.result.segments if self.state.result else []
        )
        real_segs = [s for s in all_segs if not s.is_gap]
        segs = all_segs if (use_segs and len(real_segs) > 1) else None
        br = int(round(self.spin_bitrate.get_value())) if self.spin_bitrate.get_sensitive() else None
        job = ExportJob(
            master=self.state.master,
            source=self.state.source,
            output=self._output_path,
            tracks=tracks,
            offset=self.state.offset_seconds,
            tempo=self.state.tempo,
            tempo_mode=self.state.tempo_mode,
            segments=segs,
            codec=codec,
            bitrate_kbps=br,
            container=self.state.container,
            keep_original_audio=bool(self.switch_keep_audio.get_active()),
            master_audio_indices=(
                tuple(sorted(self._master_audio_indices))
                if self.switch_keep_audio.get_active()
                else ()
            ),
            subtitle_paths=tuple(self._subtitle_paths),
            overwrite=overwrite,
        )
        if codec == "copy" and not can_direct_copy(job):
            self._on_toast(
                _("Con tempo ≠ 1.0 o múltiples tramos no es posible la copia directa; se recodificará en AAC.")
            )
        return job

    def _on_export_clicked(self, _btn: Gtk.Button) -> None:
        if self.state.batch_enabled:
            self._start_batch_export()
            return
        if self._output_path is None:
            return
        if self._output_path.exists():
            self._confirm_overwrite(self._output_path)
        else:
            self._start_export(overwrite=False)

    def _start_batch_export(self) -> None:
        from wavesync.core.probe import require_audio
        from wavesync.core.sync import analyze

        if self._batch_output_dir is None:
            return

        self.state.save_active_to_batch_entry()
        entries = list(self.state.batch_entries)
        total = len(entries)
        if total == 0:
            return

        ext = ".mp4" if self.state.container == Container.MP4 else ".mkv"
        selected_codec = _CODEC_KEYS[int(self.combo_codec.get_selected())]
        user_customized_codec = self._user_customized_codec
        custom_br = int(round(self.spin_bitrate.get_value())) if self.spin_bitrate.get_sensitive() else None
        container = self.state.container
        keep_orig = bool(self.switch_keep_audio.get_active())
        sync_params = self.state.params
        out_dir = self._batch_output_dir

        self.btn_export.set_sensitive(False)
        self.btn_cancel.set_visible(True)
        self.progress_bar.set_visible(True)
        self.progress_bar.set_fraction(0.0)
        self.progress_bar.set_text(_("Iniciando exportación del lote…"))
        if self._on_export_start is not None:
            self._on_export_start()

        def work(cancel, progress) -> list[VerificationReport]:
            reports: list[VerificationReport] = []
            for idx, entry in enumerate(entries):
                cancel.raise_if_cancelled()
                m_info = entry.master or probe(entry.master_path, cancel=cancel)
                s_info = entry.source or probe(entry.source_path, cancel=cancel)
                require_audio(s_info)
                entry.master = m_info
                entry.source = s_info

                if entry.result is None:
                    m_stream = (
                        m_info.audio_streams[min(entry.master_audio_idx, len(m_info.audio_streams) - 1)]
                        if m_info.audio_streams
                        else None
                    )
                    s_stream = s_info.audio_streams[
                        min(entry.source_audio_idx, len(s_info.audio_streams) - 1)
                    ]
                    res = analyze(
                        m_info,
                        s_info,
                        m_stream,
                        s_stream,
                        params=sync_params,
                        on_progress=lambda f, msg, i=idx, name=m_info.path.name: progress(
                            (i + 0.5 * f) / total,
                            f"[{i + 1}/{total}] {name} — {msg}",
                        ),
                        cancel=cancel,
                    )
                    entry.result = res
                    entry.offset_ms = round(res.offset_ms, 1)
                    entry.tempo = res.tempo
                    entry.segments = list(res.segments)
                    entry.use_segments = len([s for s in entry.segments if not s.is_gap]) > 1

                if entry.track_configs:
                    enabled_tcs = [tc for tc in entry.track_configs if tc.enabled]
                    tracks = [
                        ExportTrack(
                            stream=tc.stream,
                            language=tc.language,
                            title=tc.title,
                            default=tc.default,
                            source=tc.source or s_info,
                        )
                        for tc in enabled_tcs
                    ]
                    ref_stream = enabled_tcs[0].stream if enabled_tcs else (s_info.audio_streams[0] if s_info.audio_streams else None)
                else:
                    s_stream = (
                        s_info.audio_streams[
                            min(entry.source_audio_idx, len(s_info.audio_streams) - 1)
                        ]
                        if s_info.audio_streams
                        else None
                    )
                    tracks = [
                        ExportTrack(
                            stream=s_stream,
                            language=s_stream.language or "spa",
                            title=s_stream.title or "",
                            default=True,
                            source=s_info,
                        )
                    ] if s_stream else []
                    ref_stream = s_stream

                codec = selected_codec if user_customized_codec else (
                    detect_preferred_codec(ref_stream) if ref_stream else "aac"
                )
                br = custom_br if user_customized_codec else (
                    default_bitrate_kbps(codec, ref_stream.channels) if ref_stream else 192
                )
                out_path = out_dir / f"{m_info.path.stem}.wavesync{ext}"

                master_indices = (
                    entry.master_audio_indices
                    if entry.master_audio_indices is not None
                    else (
                        tuple(range(len(m_info.audio_streams)))
                        if keep_orig
                        else ()
                    )
                ) if keep_orig else ()

                job = ExportJob(
                    master=m_info,
                    source=s_info,
                    output=out_path,
                    tracks=tracks,
                    offset=entry.offset_ms / 1000.0,
                    tempo=entry.tempo,
                    tempo_mode=entry.tempo_mode,
                    segments=entry.active_segments,
                    codec=codec,
                    bitrate_kbps=br,
                    container=container,
                    keep_original_audio=keep_orig,
                    master_audio_indices=master_indices,
                    overwrite=True,
                )

                rep = export(
                    job,
                    on_progress=lambda f, msg, i=idx, name=out_path.name: progress(
                        (i + 0.5 + 0.5 * f) / total,
                        f"[{i + 1}/{total}] {name} — {msg}",
                    ),
                    cancel=cancel,
                )
                reports.append(rep)
            return reports

        def done(reports: list[VerificationReport]) -> None:
            self._finish_task_ui()
            self._rebuild_batch_group()
            ok_count = sum(1 for r in reports if r.ok)
            self.row_verify.set_title(
                _("✅ Lote exportado y verificado: {ok}/{total} películas correctas").format(
                    ok=ok_count, total=len(reports)
                )
            )
            names = ", ".join(r.info.path.name for r in reports)
            self.row_verify.set_subtitle(GLib.markup_escape_text(names))
            if self._preferences:
                for entry, rep in zip(entries, reports):
                    self._preferences.add_sync_history(
                        master_path=entry.master_path,
                        source_path=entry.source_path,
                        offset=entry.offset_ms / 1000.0,
                        tempo=entry.tempo,
                        confidence=entry.result.confidence if entry.result else 1.0,
                        verdict=entry.result.verdict.name if entry.result else "SIMPLE",
                        output_path=rep.info.path,
                    )
                self._preferences.save()
            self._on_toast(
                _("Lote exportado y verificado: {ok}/{total} películas.").format(
                    ok=ok_count, total=len(reports)
                )
            )

        def on_cancel() -> None:
            self._finish_task_ui()
            self._on_toast(_("Exportación del lote cancelada."))

        self._task = run_in_background(
            work,
            on_progress=self._on_export_progress,
            on_done=done,
            on_error=self._on_error,
            on_cancelled=on_cancel,
        )

    def _confirm_overwrite(self, path: Path) -> None:
        dialog = Adw.AlertDialog.new(
            _("¿Sobrescribir archivo existente?"),
            _("El archivo «{name}» ya existe en «{folder}». ¿Deseas reemplazarlo?").format(
                name=path.name, folder=path.parent
            ),
        )
        dialog.add_response("cancel", _("Cancelar"))
        dialog.add_response("overwrite", _("Sobrescribir"))
        dialog.set_response_appearance("overwrite", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_overwrite_response)
        root = self.get_root()
        if isinstance(root, Gtk.Window):
            dialog.present(root)

    def _on_overwrite_response(self, _dialog: Adw.AlertDialog, response: str) -> None:
        if response == "overwrite":
            self._start_export(overwrite=True)

    def _start_export(self, *, overwrite: bool) -> None:
        job = self._build_job(overwrite=overwrite)
        self.btn_export.set_sensitive(False)
        self.btn_cancel.set_visible(True)
        self.progress_bar.set_visible(True)
        self.progress_bar.set_fraction(0.0)
        self.progress_bar.set_text(_("Iniciando exportación…"))
        if self._on_export_start is not None:
            self._on_export_start()

        def work(cancel, progress) -> VerificationReport:
            return export(job, on_progress=progress, cancel=cancel)

        def on_prog(frac: float, msg: str) -> None:
            self.progress_bar.set_fraction(max(0.0, min(1.0, frac)))
            self.progress_bar.set_text(msg)

        def on_done(report: VerificationReport) -> None:
            self._finish_task_ui()
            self._render_verification(report)
            self._on_toast(_("Exportación completada y verificada: {name}").format(name=report.info.path.name))
            if self._preferences and self.state.master and self.state.source and self.state.result:
                self._preferences.add_sync_history(
                    master_path=self.state.master.path,
                    source_path=self.state.source.path,
                    offset=self.state.offset_seconds,
                    tempo=self.state.tempo,
                    confidence=self.state.result.confidence,
                    verdict=self.state.result.verdict.name,
                    output_path=report.info.path,
                )
                self._preferences.save()

        def on_err(err: WaveSyncError) -> None:
            self._finish_task_ui()
            self._on_error(err)

        def on_cancel() -> None:
            self._finish_task_ui()
            self._on_toast(_("Exportación cancelada; archivo temporal eliminado."))

        self._task = run_in_background(
            work,
            on_progress=on_prog,
            on_done=on_done,
            on_error=on_err,
            on_cancelled=on_cancel,
        )

    def _on_export_progress(self, fraction: float, message: str) -> None:
        self.progress_bar.set_fraction(max(0.0, min(1.0, fraction)))
        self.progress_bar.set_text(message)

    def _on_cancel_clicked(self, _btn: Gtk.Button) -> None:
        if self._task is not None:
            self._task.cancel()

    def _finish_task_ui(self) -> None:
        self._task = None
        self.btn_cancel.set_visible(False)
        self.progress_bar.set_visible(False)
        self._refresh_export_sensitivity()

    def _render_verification(self, report: VerificationReport) -> None:
        status_icon = "✅" if report.ok else "⚠️"
        self.row_verify.set_title(
            _("{icon} Verificado: {name} ({n} pistas de audio, {dur:.1f} s)").format(
                icon=status_icon,
                name=report.info.path.name,
                n=len(report.info.audio_streams),
                dur=report.info.duration,
            )
        )
        checks = [
            _("Vídeo copiado sin recodificar: sí") if report.video_copied else _("Vídeo: discrepancia"),
            _("Pistas de audio: OK") if report.audio_count_ok else _("Pistas: error"),
            _("Metadatos (idioma/título): OK") if report.metadata_ok else _("Metadatos: discrepancia"),
            _("Duración: OK") if report.duration_ok else _("Duración: discrepancia"),
        ]
        if report.warnings:
            checks.extend(report.warnings)
        self.row_verify.set_subtitle(GLib.markup_escape_text(" · ".join(checks)))
