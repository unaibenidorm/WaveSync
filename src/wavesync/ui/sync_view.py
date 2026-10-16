"""Synchronisation analysis tab: parameters, progress/cancel, verdict, timeline and segments table."""

from __future__ import annotations

from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from wavesync.core.errors import WaveSyncError  # noqa: E402
from wavesync.core.i18n import _  # noqa: E402
from wavesync.core.models import SyncResult, Verdict  # noqa: E402
from wavesync.core.sync import SyncParams, analyze  # noqa: E402
from wavesync.ui.state import AppState  # noqa: E402
from wavesync.ui.timeline import TimelineWidget  # noqa: E402
from wavesync.ui.workers import BackgroundTask, run_in_background  # noqa: E402


def _fmt_time_range(s0: float, s1: float) -> str:
    def f(x: float) -> str:
        m, s = divmod(int(round(max(0.0, x))), 60)
        h, m = divmod(m, 60)
        return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

    return f"{f(s0)} – {f(s1)}"


class SyncView(Gtk.Box):
    """View for running automatic synchronisation and inspecting segments & confidence."""

    def __init__(
        self,
        state: AppState,
        on_error: Callable[[WaveSyncError], None],
        on_toast: Callable[[str], None],
        on_go_compare: Callable[[], None],
        on_sync_completed: Callable[[SyncResult], None] | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.state = state
        self._on_error = on_error
        self._on_toast = on_toast
        self._on_go_compare = on_go_compare
        self._on_sync_completed = on_sync_completed or (lambda _res: None)
        self._task: BackgroundTask | None = None
        self._segment_rows: list[Gtk.Widget] = []
        self._batch_rows: list[Gtk.Widget] = []

        page = Adw.PreferencesPage()
        page.set_vexpand(True)
        self.append(page)

        # --- Parameters & Launch Group ---
        self.param_group = Adw.PreferencesGroup(
            title=_("Análisis automático (vídeo primero, audio como refinamiento)"),
            description=_(
                "Alinea las imágenes con normalización por rangos y refina a precisión sub-milisegundo "
                "con GCC-PHAT sobre la banda sonora."
            ),
        )
        page.add(self.param_group)

        self.spin_search = Adw.SpinRow.new_with_range(5.0, 300.0, 5.0)
        self.spin_search.set_title(_("Rango de búsqueda inicial (± segundos)"))
        self.spin_search.set_subtitle(_("Auméntalo si hay logos de productora o introducciones muy largas"))
        self.spin_search.set_value(self.state.params.search_range)
        self.param_group.add(self.spin_search)

        self.spin_windows = Adw.SpinRow.new_with_range(20.0, 60.0, 2.0)
        self.spin_windows.set_title(_("Ventanas de medición (mínimo 20)"))
        self.spin_windows.set_subtitle(_("Repartidas a lo largo de la película para estimar tempo y tramos"))
        self.spin_windows.set_value(float(self.state.params.n_windows))
        self.param_group.add(self.spin_windows)

        self.row_audio_refine = Adw.ComboRow(
            title=_("Refinamiento por audio (Etapa 2)"),
            subtitle=_("Independiente del idioma (música, efectos y ambiente)"),
            model=Gtk.StringList.new(
                [
                    _("GCC-PHAT con atenuación vocal (recomendado)"),
                    _("GCC-PHAT sobre canal lateral L−R (elimina diálogo central en estéreo)"),
                    _("Correlación de envolventes de flujo espectral"),
                    _("Desactivado (solo vídeo)"),
                ]
            ),
        )
        self.row_audio_refine.set_selected(0)
        self.param_group.add(self.row_audio_refine)

        # Action row with Run / Cancel buttons + progress bar
        ctrl_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=8, margin_bottom=4)
        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, halign=Gtk.Align.CENTER)

        self.btn_run = Gtk.Button(label=_("Iniciar sincronización automática"))
        self.btn_run.add_css_class("pill")
        self.btn_run.add_css_class("suggested-action")
        self.btn_run.connect("clicked", self._on_run_clicked)
        btn_row.append(self.btn_run)

        self.btn_cancel = Gtk.Button(label=_("Cancelar"))
        self.btn_cancel.add_css_class("pill")
        self.btn_cancel.add_css_class("destructive-action")
        self.btn_cancel.set_visible(False)
        self.btn_cancel.connect("clicked", self._on_cancel_clicked)
        btn_row.append(self.btn_cancel)

        ctrl_box.append(btn_row)

        self.progress_bar = Gtk.ProgressBar(show_text=True, visible=False)
        ctrl_box.append(self.progress_bar)
        self.param_group.add(ctrl_box)

        # --- Batch Progress & Per-Movie Overview Group (visible in batch mode) ---
        self.batch_group = Adw.PreferencesGroup(
            title=_("Películas del lote"),
            description=_(
                "Estado de sincronización de cada película del lote. "
                "Pulsa «Ver detalle» para inspeccionar su línea temporal o «Comparar A/B» para ajustarla."
            ),
        )
        self.batch_group.set_visible(False)
        page.add(self.batch_group)

        # --- Verdict & Summary Group ---
        self.summary_group = Adw.PreferencesGroup(
            title=_("Resultado y veredicto"),
            description=_("Resumen de los parámetros calculados por el motor."),
        )
        page.add(self.summary_group)

        self.row_verdict = Adw.ActionRow(
            title=_("Pendiente de análisis"),
            subtitle=_("Carga los archivos A y B y pulsa «Iniciar sincronización automática»."),
        )
        self.summary_group.add(self.row_verdict)

        self.row_offset = Adw.ActionRow(title=_("Offset principal"), subtitle="—")
        self.summary_group.add(self.row_offset)

        self.row_tempo = Adw.ActionRow(title=_("Factor de tempo (multiplicador sobre B)"), subtitle="—")
        self.summary_group.add(self.row_tempo)

        self.row_notes = Adw.ActionRow(title=_("Observaciones"), subtitle="—")
        self.summary_group.add(self.row_notes)

        # --- Timeline Group ---
        timeline_group = Adw.PreferencesGroup(
            title=_("Línea temporal de tramos y ventanas"),
            description=_(
                "Banda superior: tramos detectados · Puntos inferiores: offset por ventana "
                "(verde = alta confianza, ámbar = media, rojo = baja)."
            ),
        )
        page.add(timeline_group)

        self.timeline = TimelineWidget()
        frame = Gtk.Frame(child=self.timeline)
        timeline_group.add(frame)

        # --- Segments Table Group ---
        self.segments_group = Adw.PreferencesGroup(
            title=_("Tramos de montaje detectados"),
            description=_("Detalle de cada tramo constante a lo largo del máster A."),
        )
        page.add(self.segments_group)

        # Proceed to comparator button
        nav_group = Adw.PreferencesGroup()
        page.add(nav_group)
        self.btn_compare = Gtk.Button(
            label=_("Abrir en el comparador A/B y ajuste manual →"),
            halign=Gtk.Align.CENTER,
            margin_top=6,
            margin_bottom=12,
        )
        self.btn_compare.add_css_class("pill")
        self.btn_compare.connect("clicked", lambda _b: self._on_go_compare())
        nav_group.add(self.btn_compare)

        self._expert_widgets = (self.spin_search, self.spin_windows, self.row_audio_refine)
        self.state.subscribe(self._on_state_changed)
        self._update_expert_visibility()
        self._rebuild_batch_group()
        self._refresh_sensitivity()

    def _on_state_changed(self, topic: str) -> None:
        if topic in ("all", "inputs", "batch"):
            self._rebuild_batch_group()
            self._refresh_sensitivity()
        if topic in ("all", "result", "batch"):
            self._render_result(self.state.result)
        if topic in ("all", "preferences"):
            self._update_expert_visibility()

    def _update_expert_visibility(self) -> None:
        for widget in self._expert_widgets:
            widget.set_visible(self.state.expert_mode)

    def _refresh_sensitivity(self) -> None:
        running = self._task is not None and self._task.alive
        if self.state.batch_enabled:
            n = len(self.state.batch_entries)
            ready = n > 0
            self.btn_run.set_label(
                _("Sincronizar todo el lote ({n} películas)").format(n=n)
                if n > 0
                else _("Sincronizar lote")
            )
            any_done = any(e.result is not None for e in self.state.batch_entries)
            self.btn_run.set_sensitive(ready and not running)
            self.btn_compare.set_sensitive((self.state.result is not None or any_done) and not running)
        else:
            ready = bool(self.state.master and self.state.source and self.state.source.has_audio)
            self.btn_run.set_label(_("Iniciar sincronización automática"))
            self.btn_run.set_sensitive(ready and not running)
            self.btn_compare.set_sensitive(self.state.result is not None and not running)

    def _rebuild_batch_group(self) -> None:
        for w in self._batch_rows:
            self.batch_group.remove(w)
        self._batch_rows.clear()

        if not self.state.batch_enabled:
            self.batch_group.set_visible(False)
            return

        self.batch_group.set_visible(True)
        entries = self.state.batch_entries
        done_count = sum(1 for e in entries if e.result is not None)
        self.batch_group.set_title(
            _("Películas del lote ({done}/{total} sincronizadas)").format(
                done=done_count, total=len(entries)
            )
        )

        if not entries:
            row = Adw.ActionRow(
                title=_("No hay películas preparadas en el lote"),
                subtitle=_("Vuelve a la pestaña «1. Archivos» y pulsa «Continuar a sincronización por lotes»."),
            )
            self.batch_group.add(row)
            self._batch_rows.append(row)
            return

        for idx, entry in enumerate(entries):
            is_active = idx == self.state.active_batch_idx
            title = f"{entry.master_path.name}  ⟷  {entry.source_path.name}"
            if entry.result is not None:
                res = entry.result
                badge = {
                    Verdict.SIMPLE: "🟢",
                    Verdict.SEGMENTS: "🟡",
                    Verdict.UNRELIABLE: "🔴",
                }[res.verdict]
                real_segs = [s for s in entry.segments if not s.is_gap]
                seg_txt = (
                    _(" · {k} tramos").format(k=len(real_segs))
                    if len(real_segs) > 1
                    else ""
                )
                sub = _(
                    "{badge} {verdict} · Offset: {ms:+.1f} ms · Tempo: {tempo:.6f} · Confianza: {c:.0%}{segs}"
                ).format(
                    badge=badge,
                    verdict=res.verdict.label,
                    ms=entry.offset_ms,
                    tempo=entry.tempo,
                    c=res.confidence,
                    segs=seg_txt,
                )
            elif entry.error:
                sub = f"⚠️ {entry.error}"
            else:
                sub = _("Pendiente de sincronizar")

            row = Adw.ActionRow(
                title=GLib.markup_escape_text(title),
                subtitle=GLib.markup_escape_text(sub),
            )
            lbl_num = Gtk.Label(
                label=f"▶ #{idx + 1}" if is_active else f"#{idx + 1}",
                valign=Gtk.Align.CENTER,
            )
            lbl_num.add_css_class("heading")
            if not is_active:
                lbl_num.add_css_class("dim-label")
            row.add_prefix(lbl_num)

            btn_view = Gtk.Button(
                label=_("Viendo") if is_active else _("Ver detalle"),
                valign=Gtk.Align.CENTER,
            )
            btn_view.add_css_class("flat")
            btn_view.set_sensitive(not is_active)
            btn_view.connect("clicked", lambda _b, i=idx: self.state.select_batch_entry(i))
            row.add_suffix(btn_view)

            btn_cmp = Gtk.Button(
                label=_("Comparar A/B"),
                valign=Gtk.Align.CENTER,
            )
            btn_cmp.add_css_class("flat")
            btn_cmp.set_sensitive(entry.result is not None)
            btn_cmp.connect("clicked", lambda _b, i=idx: self._open_batch_entry_in_compare(i))
            row.add_suffix(btn_cmp)

            self.batch_group.add(row)
            self._batch_rows.append(row)

    def _open_batch_entry_in_compare(self, idx: int) -> None:
        self.state.select_batch_entry(idx)
        self._on_go_compare()

    def _collect_params(self) -> SyncParams:
        sel = int(self.row_audio_refine.get_selected())
        audio_refine = sel != 3
        audio_mode = "sides" if sel == 1 else "full"
        audio_method = "envelope" if sel == 2 else "gcc"
        p = SyncParams(
            search_range=float(self.spin_search.get_value()),
            n_windows=max(20, int(round(self.spin_windows.get_value()))),
            audio_refine=audio_refine,
            audio_mode=audio_mode,
            audio_method=audio_method,
        )
        self.state.params = p
        return p

    def _on_run_clicked(self, _btn: Gtk.Button) -> None:
        if self.state.batch_enabled and self.state.batch_entries:
            self._run_batch_analysis()
            return
        if not self.state.master or not self.state.source or not self.state.source_stream:
            return
        params = self._collect_params()
        master = self.state.master
        source = self.state.source
        m_stream = self.state.master_stream
        s_stream = self.state.source_stream

        self.state.set_sync_in_progress(True)
        self.btn_run.set_sensitive(False)
        self.btn_compare.set_sensitive(False)
        self.btn_cancel.set_visible(True)
        self.progress_bar.set_visible(True)
        self.progress_bar.set_fraction(0.0)
        self.progress_bar.set_text(_("Iniciando…"))

        def work(cancel, progress) -> SyncResult:
            return analyze(
                master,
                source,
                m_stream,
                s_stream,
                params=params,
                on_progress=progress,
                cancel=cancel,
            )

        def on_prog(frac: float, msg: str) -> None:
            self.progress_bar.set_fraction(max(0.0, min(1.0, frac)))
            self.progress_bar.set_text(msg)

        def on_done(res: SyncResult) -> None:
            self._finish_task_ui()
            self.state.apply_result(res)
            self._on_sync_completed(res)
            self._on_toast(
                _("Análisis completado: {verdict} ({ms:+.1f} ms)").format(
                    verdict=res.verdict.label, ms=res.offset_ms
                )
            )

        def on_err(err: WaveSyncError) -> None:
            self._finish_task_ui()
            self._on_error(err)

        def on_cancel() -> None:
            self._finish_task_ui()
            self._on_toast(_("Análisis cancelado."))

        self._task = run_in_background(
            work,
            on_progress=on_prog,
            on_done=on_done,
            on_error=on_err,
            on_cancelled=on_cancel,
        )

    def _run_batch_analysis(self) -> None:
        from wavesync.core.probe import probe, require_audio

        params = self._collect_params()
        entries = list(self.state.batch_entries)
        total = len(entries)
        if total == 0:
            return

        self.state.set_sync_in_progress(True)
        self.btn_run.set_sensitive(False)
        self.btn_compare.set_sensitive(False)
        self.btn_cancel.set_visible(True)
        self.progress_bar.set_visible(True)
        self.progress_bar.set_fraction(0.0)
        self.progress_bar.set_text(_("Iniciando sincronización por lotes…"))

        def work(cancel, progress) -> list[SyncResult]:
            results: list[SyncResult] = []
            for idx, entry in enumerate(entries):
                cancel.raise_if_cancelled()
                m_info = entry.master or probe(entry.master_path, cancel=cancel)
                s_info = entry.source or probe(entry.source_path, cancel=cancel)
                require_audio(s_info)
                entry.master = m_info
                entry.source = s_info
                m_stream = (
                    m_info.audio_streams[min(entry.master_audio_idx, len(m_info.audio_streams) - 1)]
                    if m_info.audio_streams
                    else None
                )
                s_stream = s_info.audio_streams[
                    min(entry.source_audio_idx, len(s_info.audio_streams) - 1)
                ]

                def item_prog(f: float, m: str, i: int = idx, name: str = m_info.path.name) -> None:
                    overall = (i + max(0.0, min(1.0, f))) / total
                    progress(overall, f"[{i + 1}/{total}] {name} — {m}")

                res = analyze(
                    m_info,
                    s_info,
                    m_stream,
                    s_stream,
                    params=params,
                    on_progress=item_prog,
                    cancel=cancel,
                )
                results.append(res)
                GLib.idle_add(self._on_batch_item_analyzed, idx, res)
            return results

        def on_prog(frac: float, msg: str) -> None:
            self.progress_bar.set_fraction(max(0.0, min(1.0, frac)))
            self.progress_bar.set_text(msg)

        def on_done(results: list[SyncResult]) -> None:
            self._finish_task_ui()
            self._rebuild_batch_group()
            self._on_toast(
                _("Lote sincronizado: {n} películas analizadas.").format(n=len(results))
            )

        def on_err(err: WaveSyncError) -> None:
            self._finish_task_ui()
            self._rebuild_batch_group()
            self._on_error(err)

        def on_cancel() -> None:
            self._finish_task_ui()
            self._rebuild_batch_group()
            self._on_toast(_("Sincronización por lotes cancelada."))

        self._task = run_in_background(
            work,
            on_progress=on_prog,
            on_done=on_done,
            on_error=on_err,
            on_cancelled=on_cancel,
        )

    def _on_batch_item_analyzed(self, idx: int, res: SyncResult) -> bool:
        self.state.apply_batch_entry_result(idx, res)
        self._on_sync_completed(res)
        return GLib.SOURCE_REMOVE

    def _on_cancel_clicked(self, _btn: Gtk.Button) -> None:
        if self._task is not None:
            self._task.cancel()

    def _finish_task_ui(self) -> None:
        self._task = None
        self.state.set_sync_in_progress(False)
        self.btn_cancel.set_visible(False)
        self.progress_bar.set_visible(False)
        self._refresh_sensitivity()

    def _render_result(self, res: SyncResult | None) -> None:
        for w in self._segment_rows:
            self.segments_group.remove(w)
        self._segment_rows.clear()

        if self.state.batch_enabled and self.state.master:
            self.summary_group.set_title(
                _("Resultado y veredicto — #{i}: {name}").format(
                    i=self.state.active_batch_idx + 1,
                    name=GLib.markup_escape_text(self.state.master.path.name),
                )
            )
        else:
            self.summary_group.set_title(_("Resultado y veredicto"))

        dur = self.state.master.duration if self.state.master else 0.0
        self.timeline.set_data(dur, res)

        if res is None:
            self.row_verdict.set_title(_("Pendiente de análisis"))
            self.row_verdict.set_subtitle(
                _("Carga los archivos A y B y pulsa «Iniciar sincronización automática».")
            )
            self.row_offset.set_subtitle("—")
            self.row_tempo.set_subtitle("—")
            self.row_notes.set_subtitle(_("Ninguno"))
            return

        badge = {
            Verdict.SIMPLE: "🟢",
            Verdict.SEGMENTS: "🟡",
            Verdict.UNRELIABLE: "🔴",
        }[res.verdict]
        self.row_verdict.set_title(f"{badge} {res.verdict.label}")
        mode_lbl = _("Vídeo + Audio") if res.audio_refined else (
            _("Vídeo") if res.mode == "video" else _("Solo audio")
        )
        self.row_verdict.set_subtitle(
            _("Confianza global: {c:.1%} · Modo: {m} · Ventanas válidas: {k}/{n}").format(
                c=res.confidence,
                m=mode_lbl,
                k=sum(1 for w in res.windows if w.inlier),
                n=len(res.windows),
            )
        )

        direction = (
            _("el audio B entra más tarde → se inserta silencio al inicio")
            if res.offset >= 0
            else _("sobra inicio en el audio B → se recorta el principio")
        )
        self.row_offset.set_subtitle(f"{res.offset_ms:+.1f} ms ({res.offset:+.6f} s) — {direction}")

        preset_str = f" · Preajuste {res.tempo_preset}" if res.tempo_preset else ""
        self.row_tempo.set_subtitle(
            f"{res.tempo:.6f} (medido: {res.tempo_measured:.6f}){preset_str}"
        )

        display_messages = list(res.messages)
        if (res.confidence < 0.45 or res.verdict == Verdict.UNRELIABLE) and not any(
            "demasiada diferencia" in m or "diferencia" in m for m in display_messages
        ):
            display_messages.insert(
                0,
                _(
                    "Confianza baja ({c:.0%}): puede que no sea el mismo vídeo o que haya demasiada diferencia."
                ).format(c=res.confidence),
            )

        if display_messages:
            self.row_notes.set_subtitle(GLib.markup_escape_text(" · ".join(display_messages)))
        else:
            self.row_notes.set_subtitle(_("Sin incidencias."))

        active_segs = self.state.segments if self.state.segments else res.segments
        for idx, seg in enumerate(active_segs, 1):
            t_range = _fmt_time_range(seg.start, seg.end)
            if seg.is_gap:
                row = Adw.ActionRow(
                    title=_("Tramo #{i}: {r} — Hueco (relleno con silencio)").format(i=idx, r=t_range),
                    subtitle=_("Material presente en A sin equivalente en B"),
                )
            else:
                row = Adw.ActionRow(
                    title=_("Tramo #{i}: {r} — Offset {ms:+.1f} ms").format(
                        i=idx, r=t_range, ms=seg.offset_ms or 0.0
                    ),
                    subtitle=_("Confianza: {c:.0%} · {n} ventanas de medición").format(
                        c=seg.confidence, n=seg.n_windows
                    ),
                )
            self.segments_group.add(row)
            self._segment_rows.append(row)
