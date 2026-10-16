"""Inputs tab (Archivo A máster + Archivo B fuente) with file picker, drag & drop and stream selectors."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from wavesync.core.errors import MediaError, WaveSyncError  # noqa: E402
from wavesync.core.i18n import _  # noqa: E402
from wavesync.core.models import MediaInfo  # noqa: E402
from wavesync.core.probe import probe, require_audio  # noqa: E402
from wavesync.ui.state import AppState  # noqa: E402
from wavesync.ui.workers import run_in_background  # noqa: E402


def _fmt_duration(sec: float) -> str:
    m, s = divmod(int(round(sec)), 60)
    h, m = divmod(m, 60)
    return f"{h:d}h {m:02d}m {s:02d}s" if h else f"{m:d}m {s:02d}s"


class InputsView(Gtk.Box):
    """View for selecting and inspecting Master (A) and Source (B) files."""

    def __init__(
        self,
        state: AppState,
        on_error: Callable[[WaveSyncError], None],
        on_toast: Callable[[str], None],
        on_go_sync: Callable[[], None],
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.state = state
        self._on_error = on_error
        self._on_toast = on_toast
        self._on_go_sync = on_go_sync
        self._updating_combos = False
        self._batch_masters: list[Path] = []
        self._batch_sources: list[Path] = []
        self._batch_rows_a: list[Gtk.Widget] = []
        self._batch_rows_b: list[Gtk.Widget] = []

        # Banner shown when B has no video stream (audio-only mode warning)
        self.audio_only_banner = Adw.Banner(
            title=_(
                "El Archivo B no contiene vídeo: se usará el pipeline de audio como método principal "
                "(menor fiabilidad cuando los idiomas son distintos)."
            ),
            revealed=False,
        )
        self.append(self.audio_only_banner)

        page = Adw.PreferencesPage()
        page.set_vexpand(True)
        self.append(page)

        batch_group = Adw.PreferencesGroup(
            title=_("Modo por lotes"),
            description=_("Sincroniza varias películas usando el mismo orden: máster 1 con fuente 1, máster 2 con fuente 2…"),
        )
        page.add(batch_group)
        batch_row = Adw.ActionRow(
            title=_("Activar sincronización por lotes"),
            subtitle=_("Selecciona dos listas de películas en el mismo orden; la carpeta se pedirá al exportar"),
        )
        self.batch_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.batch_switch.connect("state-set", self._on_batch_toggled)
        batch_row.add_suffix(self.batch_switch)
        batch_group.add(batch_row)

        # --- Group A: Master video ---
        self.group_a = Adw.PreferencesGroup(
            title=_("Archivo A — Vídeo máster (alta calidad)"),
            description=_(
                "Película de referencia cuyo vídeo, subtítulos y capítulos se conservarán sin recodificar."
            ),
        )
        page.add(self.group_a)

        self.row_file_a = Adw.ActionRow(
            title=_("Ningún archivo seleccionado"),
            subtitle=_("Haz clic en «Elegir…» o arrastra y suelta un archivo multimedia aquí"),
        )
        btn_a = Gtk.Button(label=_("Elegir…"), valign=Gtk.Align.CENTER)
        btn_a.add_css_class("suggested-action")
        btn_a.connect("clicked", lambda _b: self._on_choose_file(is_master=True))
        self.row_file_a.add_suffix(btn_a)
        self.row_file_a.set_activatable_widget(btn_a)
        self.group_a.add(self.row_file_a)

        self.row_video_a = Adw.ActionRow(
            title=_("Stream de vídeo"),
            subtitle=_("Sin datos"),
        )
        self.group_a.add(self.row_video_a)

        self.combo_audio_a = Adw.ComboRow(
            title=_("Pista de audio de referencia en A"),
            subtitle=_("Se usa para el refinamiento sub-milisegundo en la etapa 2 y el comparador A/B"),
        )
        self.combo_audio_a.connect("notify::selected", self._on_audio_a_selected)
        self.group_a.add(self.combo_audio_a)

        self._attach_drop_target(self.group_a, is_master=True)

        # --- Group B: Source audio/video ---
        self.group_b = Adw.PreferencesGroup(
            title=_("Archivo B — Fuente del audio a sincronizar"),
            description=_(
                "Versión con el doblaje o pista de audio deseada (vídeo de cualquier calidad o solo audio)."
            ),
        )
        page.add(self.group_b)

        self.row_file_b = Adw.ActionRow(
            title=_("Ningún archivo seleccionado"),
            subtitle=_("Haz clic en «Elegir…» o arrastra y suelta el archivo B aquí"),
        )
        btn_b = Gtk.Button(label=_("Elegir…"), valign=Gtk.Align.CENTER)
        btn_b.add_css_class("suggested-action")
        btn_b.connect("clicked", lambda _b: self._on_choose_file(is_master=False))
        self.row_file_b.add_suffix(btn_b)
        self.row_file_b.set_activatable_widget(btn_b)
        self.group_b.add(self.row_file_b)

        self.row_video_b = Adw.ActionRow(
            title=_("Stream de vídeo en B"),
            subtitle=_("Sin datos"),
        )
        self.group_b.add(self.row_video_b)

        self.combo_audio_b = Adw.ComboRow(
            title=_("Pista de audio de B a sincronizar"),
            subtitle=_("Selecciona la pista que se analizará como referencia principal"),
        )
        self.combo_audio_b.connect("notify::selected", self._on_audio_b_selected)
        self.group_b.add(self.combo_audio_b)

        self.row_add_more = Adw.ActionRow(
            title=_("Añadir pistas de otro archivo a sincronizar"),
            subtitle=_("Si tienes otros archivos con doblajes para sincronizar con este máster"),
        )
        btn_add_more = Gtk.Button(label=_("Añadir archivo…"), valign=Gtk.Align.CENTER)
        btn_add_more.connect("clicked", lambda _b: self._open_file_dialog(is_master=False, is_additional=True))
        self.row_add_more.add_suffix(btn_add_more)
        self.group_b.add(self.row_add_more)

        self._attach_drop_target(self.group_b, is_master=False)

        # --- Bottom action group ---
        action_group = Adw.PreferencesGroup()
        page.add(action_group)

        self.btn_proceed = Gtk.Button(
            label=_("Continuar al análisis de sincronización →"),
            halign=Gtk.Align.CENTER,
            margin_top=6,
            margin_bottom=12,
        )
        self.btn_proceed.add_css_class("pill")
        self.btn_proceed.add_css_class("suggested-action")
        self.btn_proceed.set_sensitive(False)
        self.btn_proceed.connect("clicked", lambda _b: self._on_proceed())
        action_group.add(self.btn_proceed)

        self.state.subscribe(self._on_state_changed)
        self._update_expert_visibility()
        self._refresh_ui()

    def _attach_drop_target(self, widget: Gtk.Widget, *, is_master: bool) -> None:
        target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        target.connect("drop", self._on_drop, is_master)
        widget.add_controller(target)

    def _on_drop(self, _target: Gtk.DropTarget, value, _x: float, _y: float, is_master: bool) -> bool:
        files = value.get_files() if hasattr(value, "get_files") else []
        if not files:
            return False
        if self.state.batch_enabled:
            paths = [Path(f.get_path()) for f in files if f.get_path()]
            if not paths:
                return False
            target_list = self.state.batch_masters if is_master else self.state.batch_sources
            added = 0
            for p in paths:
                if p not in target_list:
                    target_list.append(p)
                    added += 1
            if is_master:
                self._batch_masters = list(self.state.batch_masters)
            else:
                self._batch_sources = list(self.state.batch_sources)
            if added:
                msg = (
                    _("Se añadió 1 película al máster")
                    if is_master and added == 1
                    else _("Se añadieron {n} películas al máster").format(n=added)
                    if is_master
                    else _("Se añadió 1 película a sincronizar")
                    if added == 1
                    else _("Se añadieron {n} películas a sincronizar").format(n=added)
                )
                self._on_toast(msg)
                self._refresh_ui()
                self.state.notify("inputs")
            else:
                self._on_toast(_("Las películas ya estaban en la lista"))
            return True
        gfile: Gio.File = files[0]
        local_path = gfile.get_path()
        if local_path:
            self.load_path(Path(local_path), is_master=is_master)
            return True
        return False

    def _on_choose_file(self, *, is_master: bool) -> None:
        if self.state.batch_enabled:
            self._choose_batch_files(is_master)
        else:
            self._open_file_dialog(is_master=is_master)

    def _open_file_dialog(self, *, is_master: bool, is_additional: bool = False) -> None:
        title = _("Seleccionar Archivo A (máster)") if is_master else (
            _("Añadir archivo de audio adicional") if is_additional else _("Seleccionar Archivo B (fuente)")
        )
        dialog = Gtk.FileDialog(title=title)
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
        mode = "master" if is_master else ("additional" if is_additional else "source")
        dialog.open(parent, None, self._on_dialog_done, mode)

    def _on_batch_toggled(self, _switch: Gtk.Switch, active: bool) -> bool:
        if active:
            parent = self.get_root()
            if isinstance(parent, Gtk.Window):
                dialog = Adw.MessageDialog(
                    transient_for=parent,
                    heading=_("Orden de las películas"),
                    body=_(
                        "Las listas de másteres y películas a sincronizar deben estar en el mismo orden. "
                        "La primera fuente se sincronizará con el primer máster, la segunda con el segundo, etc."
                    ),
                )
                dialog.add_response("ok", _("Entendido"))
                dialog.set_default_response("ok")
                dialog.present()
        self.state.batch_enabled = active
        self._refresh_ui()
        self.state.notify("inputs")
        return False

    def _on_proceed(self) -> None:
        if not self.state.batch_enabled:
            self._on_go_sync()
            return
        if len(self.state.batch_masters) != len(self.state.batch_sources) or not self.state.batch_masters:
            self._on_toast(_("Selecciona el mismo número de másteres y fuentes, en el mismo orden."))
            return
        master_paths = list(self.state.batch_masters)
        source_paths = list(self.state.batch_sources)
        self.btn_proceed.set_sensitive(False)
        self.btn_proceed.set_label(_("Inspeccionando {n} películas del lote…").format(n=len(master_paths)))

        def work(cancel, _progress):
            masters: list[MediaInfo] = []
            sources: list[MediaInfo] = []
            for m_p, s_p in zip(master_paths, source_paths):
                m_info = probe(m_p, cancel=cancel)
                s_info = probe(s_p, cancel=cancel)
                require_audio(s_info)
                masters.append(m_info)
                sources.append(s_info)
            return masters, sources

        def done(infos) -> None:
            masters, sources = infos
            self.state.prepare_batch_entries(masters, sources)
            self._refresh_ui()
            self._on_go_sync()

        def err(exc: Exception) -> None:
            self._refresh_ui()
            self._on_error(exc)

        run_in_background(work, on_done=done, on_error=err)

    def _choose_batch_files(self, is_master: bool) -> None:
        title = _("Añadir películas del máster") if is_master else _("Añadir películas a sincronizar")
        dialog = Gtk.FileDialog(title=title)
        filters = Gio.ListStore.new(Gtk.FileFilter)
        f_media = Gtk.FileFilter()
        f_media.set_name(_("Archivos de vídeo y audio"))
        for pat in (
            "*.mkv", "*.mp4", "*.m4v", "*.avi", "*.mov", "*.webm", "*.ts", "*.m2ts",
            "*.ac3", "*.eac3", "*.aac", "*.m4a", "*.flac", "*.opus", "*.ogg", "*.wav", "*.mp3",
        ):
            f_media.add_pattern(pat)
        filters.append(f_media)
        f_all = Gtk.FileFilter()
        f_all.set_name(_("Todos los archivos"))
        f_all.add_pattern("*")
        filters.append(f_all)
        dialog.set_filters(filters)

        parent = self.get_root()
        dialog.open_multiple(
            parent if isinstance(parent, Gtk.Window) else None,
            None,
            self._on_batch_files_done,
            is_master,
        )

    def _on_batch_files_done(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult, is_master: bool) -> None:
        try:
            files = dialog.open_multiple_finish(result)
        except GLib.Error:
            return
        paths = [
            Path(files.get_item(i).get_path())
            for i in range(files.get_n_items())
            if files.get_item(i).get_path()
        ]
        if not paths:
            return
        target_list = self.state.batch_masters if is_master else self.state.batch_sources
        added = 0
        for p in paths:
            if p not in target_list:
                target_list.append(p)
                added += 1
        if is_master:
            self._batch_masters = list(self.state.batch_masters)
        else:
            self._batch_sources = list(self.state.batch_sources)

        if added:
            msg = (
                _("Se añadió 1 película al máster")
                if is_master and added == 1
                else _("Se añadieron {n} películas al máster").format(n=added)
                if is_master
                else _("Se añadió 1 película a sincronizar")
                if added == 1
                else _("Se añadieron {n} películas a sincronizar").format(n=added)
            )
            self._on_toast(msg)
            self._refresh_ui()
            self.state.notify("inputs")
        else:
            self._on_toast(_("Las películas seleccionadas ya estaban en la lista"))

    def _remove_batch_item(self, *, is_master: bool, index: int) -> None:
        target_list = self.state.batch_masters if is_master else self.state.batch_sources
        if 0 <= index < len(target_list):
            removed = target_list.pop(index)
            if is_master:
                self._batch_masters = list(self.state.batch_masters)
            else:
                self._batch_sources = list(self.state.batch_sources)
            self._on_toast(_("Se quitó «{name}» de la lista").format(name=removed.name))
            self._refresh_ui()
            self.state.notify("inputs")

    def _clear_batch_list(self, *, is_master: bool) -> None:
        target_list = self.state.batch_masters if is_master else self.state.batch_sources
        target_list.clear()
        if is_master:
            self._batch_masters.clear()
        else:
            self._batch_sources.clear()
        self._on_toast(_("Lista vaciada"))
        self._refresh_ui()
        self.state.notify("inputs")

    def _cleanup_batch_rows(self) -> None:
        for w in self._batch_rows_a:
            self.group_a.remove(w)
        self._batch_rows_a.clear()
        for w in self._batch_rows_b:
            self.group_b.remove(w)
        self._batch_rows_b.clear()

    def _rebuild_batch_group_a(self) -> None:
        for w in self._batch_rows_a:
            self.group_a.remove(w)
        self._batch_rows_a.clear()

        row_add = Adw.ActionRow(
            title=_("Añadir películas máster"),
            subtitle=_(
                "Añade una a una o varias películas máster (total: {n})"
            ).format(n=len(self.state.batch_masters)),
        )
        btn_add = Gtk.Button(label=_("Añadir…"), valign=Gtk.Align.CENTER)
        btn_add.add_css_class("suggested-action")
        btn_add.connect("clicked", lambda _b: self._choose_batch_files(is_master=True))
        row_add.add_suffix(btn_add)
        row_add.set_activatable_widget(btn_add)

        if self.state.batch_masters:
            btn_clear = Gtk.Button(label=_("Vaciar"), valign=Gtk.Align.CENTER)
            btn_clear.add_css_class("flat")
            btn_clear.connect("clicked", lambda _b: self._clear_batch_list(is_master=True))
            row_add.add_suffix(btn_clear)

        self.group_a.add(row_add)
        self._batch_rows_a.append(row_add)

        if not self.state.batch_masters:
            row_empty = Adw.ActionRow(
                title=_("Ninguna película máster añadida"),
                subtitle=_("Haz clic en «Añadir…» o arrastra archivos aquí para empezar"),
            )
            self.group_a.add(row_empty)
            self._batch_rows_a.append(row_empty)
        else:
            for idx, p in enumerate(self.state.batch_masters, 1):
                row = Adw.ActionRow(
                    title=GLib.markup_escape_text(p.name),
                    subtitle=GLib.markup_escape_text(str(p.parent)),
                )
                lbl_num = Gtk.Label(label=f"#{idx}", valign=Gtk.Align.CENTER)
                lbl_num.add_css_class("dim-label")
                lbl_num.add_css_class("heading")
                row.add_prefix(lbl_num)

                btn_del = Gtk.Button.new_from_icon_name("user-trash-symbolic")
                btn_del.set_valign(Gtk.Align.CENTER)
                btn_del.add_css_class("flat")
                btn_del.set_tooltip_text(_("Quitar de la lista"))
                btn_del.connect(
                    "clicked",
                    lambda _b, i=idx - 1: self._remove_batch_item(is_master=True, index=i),
                )
                row.add_suffix(btn_del)

                self.group_a.add(row)
                self._batch_rows_a.append(row)

    def _rebuild_batch_group_b(self) -> None:
        for w in self._batch_rows_b:
            self.group_b.remove(w)
        self._batch_rows_b.clear()

        row_add = Adw.ActionRow(
            title=_("Añadir películas a sincronizar"),
            subtitle=_(
                "Añade una a una o varias películas a sincronizar (total: {n})"
            ).format(n=len(self.state.batch_sources)),
        )
        btn_add = Gtk.Button(label=_("Añadir…"), valign=Gtk.Align.CENTER)
        btn_add.add_css_class("suggested-action")
        btn_add.connect("clicked", lambda _b: self._choose_batch_files(is_master=False))
        row_add.add_suffix(btn_add)
        row_add.set_activatable_widget(btn_add)

        if self.state.batch_sources:
            btn_clear = Gtk.Button(label=_("Vaciar"), valign=Gtk.Align.CENTER)
            btn_clear.add_css_class("flat")
            btn_clear.connect("clicked", lambda _b: self._clear_batch_list(is_master=False))
            row_add.add_suffix(btn_clear)

        self.group_b.add(row_add)
        self._batch_rows_b.append(row_add)

        if not self.state.batch_sources:
            row_empty = Adw.ActionRow(
                title=_("Ninguna película fuente añadida"),
                subtitle=_("Haz clic en «Añadir…» o arrastra archivos aquí para empezar"),
            )
            self.group_b.add(row_empty)
            self._batch_rows_b.append(row_empty)
        else:
            for idx, p in enumerate(self.state.batch_sources, 1):
                row = Adw.ActionRow(
                    title=GLib.markup_escape_text(p.name),
                    subtitle=GLib.markup_escape_text(str(p.parent)),
                )
                lbl_num = Gtk.Label(label=f"#{idx}", valign=Gtk.Align.CENTER)
                lbl_num.add_css_class("dim-label")
                lbl_num.add_css_class("heading")
                row.add_prefix(lbl_num)

                btn_del = Gtk.Button.new_from_icon_name("user-trash-symbolic")
                btn_del.set_valign(Gtk.Align.CENTER)
                btn_del.add_css_class("flat")
                btn_del.set_tooltip_text(_("Quitar de la lista"))
                btn_del.connect(
                    "clicked",
                    lambda _b, i=idx - 1: self._remove_batch_item(is_master=False, index=i),
                )
                row.add_suffix(btn_del)

                self.group_b.add(row)
                self._batch_rows_b.append(row)


    def _on_dialog_done(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult, mode: str) -> None:
        try:
            gfile = dialog.open_finish(result)
        except GLib.Error:
            return
        if gfile and gfile.get_path():
            self.load_path(
                Path(gfile.get_path()),
                is_master=(mode == "master"),
                is_additional=(mode == "additional"),
            )

    def load_path(self, path: Path, *, is_master: bool, is_additional: bool = False) -> None:
        """Probe ``path`` on a worker thread and update state."""

        def work(cancel, _progress) -> MediaInfo:
            info = probe(path, cancel=cancel)
            if is_master and not info.has_video and not info.has_audio:
                raise MediaError(_("El archivo máster no tiene ni vídeo ni audio."))
            if not is_master:
                require_audio(info)
            return info

        def done(info: MediaInfo) -> None:
            if is_master:
                self.state.set_master(info)
                self._on_toast(_("Archivo A cargado: {name}").format(name=info.path.name))
            elif is_additional:
                count = self.state.add_source_tracks_from_media(info)
                self._on_toast(
                    _("Se añadieron {n} pistas de audio de «{name}»").format(
                        n=count, name=info.path.name
                    )
                )
            else:
                self.state.set_source(info)
                self._on_toast(_("Archivo B cargado: {name}").format(name=info.path.name))

        run_in_background(work, on_done=done, on_error=self._on_error)

    def _on_audio_a_selected(self, combo: Adw.ComboRow, _pspec) -> None:
        if self._updating_combos:
            return
        pos = int(combo.get_selected())
        if pos != Gtk.INVALID_LIST_POSITION:
            self.state.set_master_track(pos)
            if self.state.master_stream:
                self._on_toast(
                    _("Pista de referencia A: {desc}").format(desc=self.state.master_stream.describe())
                )

    def _on_audio_b_selected(self, combo: Adw.ComboRow, _pspec) -> None:
        if self._updating_combos:
            return
        pos = int(combo.get_selected())
        if pos != Gtk.INVALID_LIST_POSITION:
            self.state.set_source_track(pos)
            if self.state.source_stream:
                self._on_toast(
                    _("Pista de fuente B seleccionada: {desc}").format(desc=self.state.source_stream.describe())
                )

    def _on_state_changed(self, topic: str) -> None:
        if topic in ("all", "inputs"):
            self._refresh_ui()
        elif topic == "inputs_track":
            self._updating_combos = True
            try:
                if self.state.master and self.state.master.audio_streams:
                    self.combo_audio_a.set_selected(self.state.master_audio_idx)
                if self.state.source and self.state.source.audio_streams:
                    self.combo_audio_b.set_selected(self.state.source_audio_idx)
            finally:
                self._updating_combos = False
        if topic in ("all", "preferences"):
            self._update_expert_visibility()

    def _update_expert_visibility(self) -> None:
        if self.state.batch_enabled:
            self.row_video_a.set_visible(False)
            self.row_video_b.set_visible(False)
        else:
            self.row_video_a.set_visible(self.state.expert_mode)
            self.row_video_b.set_visible(self.state.expert_mode)

    def _refresh_ui(self) -> None:
        self._updating_combos = True
        try:
            if self.state.batch_enabled:
                self.group_a.set_title(_("Archivos A — Vídeos máster por lotes (alta calidad)"))
                self.group_a.set_description(
                    _("Lista de películas máster de referencia en el orden deseado; vídeo, subtítulos y capítulos se conservan sin recodificar.")
                )
                self.group_b.set_title(_("Archivos B — Fuentes del audio a sincronizar por lotes"))
                self.group_b.set_description(
                    _("Lista de archivos con el doblaje en el mismo orden que la lista de másteres A.")
                )

                self.row_file_a.set_visible(False)
                self.row_video_a.set_visible(False)
                self.combo_audio_a.set_visible(False)
                self.row_file_b.set_visible(False)
                self.row_video_b.set_visible(False)
                self.combo_audio_b.set_visible(False)
                self.row_add_more.set_visible(False)
                self.audio_only_banner.set_revealed(False)

                self._rebuild_batch_group_a()
                self._rebuild_batch_group_b()

                n_a = len(self.state.batch_masters)
                n_b = len(self.state.batch_sources)
                batch_ready = (n_a == n_b and n_a > 0)
                self.btn_proceed.set_sensitive(batch_ready)
                if batch_ready:
                    self.btn_proceed.set_label(
                        _("Continuar a sincronización por lotes ({n} películas) →").format(n=n_a)
                    )
                elif n_a == 0 and n_b == 0:
                    self.btn_proceed.set_label(_("Añade películas para continuar →"))
                else:
                    self.btn_proceed.set_label(
                        _("Faltan películas por emparejar (A: {a}, B: {b}) →").format(a=n_a, b=n_b)
                    )
            else:
                self._cleanup_batch_rows()
                self.group_a.set_title(_("Archivo A — Vídeo máster (alta calidad)"))
                self.group_a.set_description(
                    _("Película de referencia cuyo vídeo, subtítulos y capítulos se conservarán sin recodificar.")
                )
                self.group_b.set_title(_("Archivo B — Fuente del audio a sincronizar"))
                self.group_b.set_description(
                    _("Versión con el doblaje o pista de audio deseada (vídeo de cualquier calidad o solo audio).")
                )
                self.row_file_a.set_visible(True)
                self.row_file_b.set_visible(True)
                self._populate_file_panel(
                    self.state.master,
                    self.row_file_a,
                    self.row_video_a,
                    self.combo_audio_a,
                    self.state.master_audio_idx,
                )
                self._populate_file_panel(
                    self.state.source,
                    self.row_file_b,
                    self.row_video_b,
                    self.combo_audio_b,
                    self.state.source_audio_idx,
                )
                self.combo_audio_a.set_visible(True)
                self.combo_audio_b.set_visible(True)
                self.row_add_more.set_visible(True)
                self._update_expert_visibility()
                b_audio_only = bool(self.state.source and not self.state.source.has_video)
                self.audio_only_banner.set_revealed(b_audio_only)
                ready = bool(self.state.master and self.state.source and self.state.source.has_audio)
                self.btn_proceed.set_sensitive(ready)
                self.btn_proceed.set_label(_("Continuar al análisis de sincronización →"))
        finally:
            self._updating_combos = False

    def _populate_file_panel(
        self,
        info: MediaInfo | None,
        row_file: Adw.ActionRow,
        row_video: Adw.ActionRow,
        combo_audio: Adw.ComboRow,
        selected_idx: int,
    ) -> None:
        if info is None:
            row_file.set_title(_("Ningún archivo seleccionado"))
            row_file.set_subtitle(_("Haz clic en «Elegir…» o arrastra y suelta un archivo aquí"))
            row_video.set_subtitle(_("Sin datos"))
            combo_audio.set_model(Gtk.StringList.new([_("Sin pistas de audio")]))
            combo_audio.set_sensitive(False)
            return

        row_file.set_title(GLib.markup_escape_text(info.path.name))
        row_file.set_subtitle(
            GLib.markup_escape_text(
                f"{info.path.parent} · {_fmt_duration(info.duration)} · {info.format_name}"
            )
        )
        if info.video:
            v = info.video
            hdr_badge = _(" · HDR") if v.hdr else _(" · SDR")
            fps_txt = (
                f"{v.fps_real:.3f} fps"
                if abs(v.fps_real - v.fps_avg) < 0.01
                else f"{v.fps_real:.3f} fps (medio {v.fps_avg:.3f})"
            )
            row_video.set_subtitle(
                f"{v.codec.upper()} · {v.width}×{v.height} · {fps_txt}{hdr_badge}"
            )
        else:
            row_video.set_subtitle(_("Sin pista de vídeo (solo audio)"))

        if info.audio_streams:
            labels = []
            for a in info.audio_streams:
                dur = f" · {_fmt_duration(a.duration)}" if a.duration else ""
                labels.append(f"{a.describe()} · {a.sample_rate} Hz{dur}")
            combo_audio.set_model(Gtk.StringList.new(labels))
            combo_audio.set_selected(min(selected_idx, len(labels) - 1))
            combo_audio.set_sensitive(True)
        else:
            combo_audio.set_model(Gtk.StringList.new([_("Sin pistas de audio")]))
            combo_audio.set_sensitive(False)
