"""Persistent UI preferences and the preferences window."""

from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from wavesync.core.i18n import _  # noqa: E402
from wavesync.ui.resources import APP_ID, image_path  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass(frozen=True)
class SyncHistoryEntry:
    """One completed automatic synchronization saved in user preferences."""

    master_path: str
    source_path: str
    timestamp: str
    offset: float
    tempo: float
    confidence: float
    verdict: str
    output_path: str | None = None

    @classmethod
    def from_dict(cls, data: object) -> SyncHistoryEntry | None:
        if not isinstance(data, dict):
            return None
        required_text = ("master_path", "source_path", "timestamp", "verdict")
        if any(not isinstance(data.get(key), str) or not data[key] for key in required_text):
            return None
        try:
            offset = float(data["offset"])
            tempo = float(data["tempo"])
            confidence = float(data["confidence"])
        except (KeyError, TypeError, ValueError):
            return None
        if not all(math.isfinite(value) for value in (offset, tempo, confidence)):
            return None
        output_path = data.get("output_path")
        if output_path is not None and not isinstance(output_path, str):
            return None
        return cls(
            master_path=data["master_path"], source_path=data["source_path"],
            timestamp=data["timestamp"], offset=offset, tempo=tempo,
            confidence=confidence, verdict=data["verdict"], output_path=output_path or None,
        )


@dataclass
class UserPreferences:
    """Small set of persistent, toolkit-independent UI preferences."""

    HISTORY_LIMIT = 50

    theme: str = "system"
    expert_mode: bool = False
    setup_seen: bool = False
    ui_language: str = "es"
    sync_history: list[SyncHistoryEntry] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path | None = None) -> UserPreferences:
        config_path = path or preferences_path()
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        theme = data.get("theme", "system")
        ui_language = data.get("ui_language", "es")
        raw_history = data.get("sync_history", [])
        history = [entry for item in raw_history if (entry := SyncHistoryEntry.from_dict(item))] if isinstance(raw_history, list) else []
        return cls(
            theme=theme if theme in {"system", "light", "dark"} else "system",
            expert_mode=bool(data.get("expert_mode", False)),
            setup_seen=bool(data.get("setup_seen", False)),
            ui_language=ui_language if ui_language in {"es", "en", "ca"} else "es",
            sync_history=history[: cls.HISTORY_LIMIT],
        )

    def add_sync_history(
        self, *, master_path: Path, source_path: Path, offset: float, tempo: float,
        confidence: float, verdict: str, output_path: Path | None = None,
    ) -> None:
        self.sync_history.insert(0, SyncHistoryEntry(
            master_path=str(master_path), source_path=str(source_path),
            timestamp=datetime.now(timezone.utc).isoformat(), offset=float(offset), tempo=float(tempo),
            confidence=float(confidence), verdict=str(verdict),
            output_path=str(output_path) if output_path else None,
        ))
        del self.sync_history[self.HISTORY_LIMIT :]

    def delete_sync_history(self, entry: SyncHistoryEntry) -> None:
        if entry in self.sync_history:
            self.sync_history.remove(entry)

    def clear_sync_history(self) -> None:
        self.sync_history.clear()

    def save(self, path: Path | None = None) -> None:
        config_path = path or preferences_path()
        config_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = config_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")
        temporary.replace(config_path)


def preferences_path() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "wavesync" / "preferences.json"


class SetupGuideDialog(Adw.Dialog):
    """First-run presentation of WaveSync's workflow and usage modes."""

    def __init__(
        self,
        expert_mode: bool,
        on_mode_selected: Callable[[bool], None],
    ) -> None:
        super().__init__(
            title=_("Guía de configuración"),
            content_width=720,
            content_height=560,
        )
        self._page = 0
        self._on_mode_selected = on_mode_selected

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        self.set_child(toolbar)

        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=18,
            margin_top=12,
            margin_bottom=24,
            margin_start=24,
            margin_end=24,
        )
        toolbar.set_content(content)

        self.stack = Gtk.Stack(
            transition_type=Gtk.StackTransitionType.SLIDE_LEFT_RIGHT,
            transition_duration=350,
            vexpand=True,
            hexpand=True,
        )
        content.append(self.stack)

        slides = (
            (
                "logo.png",
                _("Bienvenido a WaveSync"),
                _(
                    "Sincroniza una pista de audio con un vídeo máster conservando el vídeo, "
                    "los subtítulos y los capítulos sin recodificarlos."
                ),
            ),
            (
                None,
                _("1. Selecciona tus archivos"),
                _(
                    "Elige el vídeo máster A y la fuente B que contiene el audio deseado. "
                    "WaveSync permite seleccionar la pista exacta, incluso en archivos multicanal."
                ),
            ),
            (
                None,
                _("2. Sincroniza y compara"),
                _(
                    "El análisis automático calcula offset, tempo y cambios de montaje. Después "
                    "puedes alternar instantáneamente entre el audio A y el audio B."
                ),
            ),
            (
                None,
                _("3. Exporta sin perder calidad"),
                _(
                    "Revisa el resultado y exporta a MKV o MP4. La aplicación verifica "
                    "automáticamente el archivo final con ffprobe."
                ),
            ),
        )
        for index, (image, title, description) in enumerate(slides):
            page = Adw.StatusPage(title=title, description=description, vexpand=True)
            if image:
                picture = Gtk.Picture.new_for_filename(image_path(image))
                picture.set_content_fit(Gtk.ContentFit.CONTAIN)
                picture.set_size_request(360, 190)
                if index == 0:
                    # The welcome logo is intentionally above the slide text.
                    slide = Gtk.Box(
                        orientation=Gtk.Orientation.VERTICAL,
                        spacing=8,
                        halign=Gtk.Align.CENTER,
                        valign=Gtk.Align.CENTER,
                    )
                    slide.append(picture)
                    slide.append(Gtk.Label(label=title, css_classes=["title-2"]))
                    slide.append(Gtk.Label(label=description, wrap=True, justify=Gtk.Justification.CENTER))
                    page.set_title("")
                    page.set_description("")
                    page.set_child(slide)
                else:
                    page.set_child(picture)
            self.stack.add_named(page, str(index))

        mode_page = Adw.StatusPage(
            icon_name="preferences-system-symbolic",
            title=_("Elige cómo quieres empezar"),
            description=_(
                "El modo simple muestra solo lo esencial. El modo experto añade calibración "
                "manual, parámetros de análisis y opciones avanzadas de exportación."
            ),
            vexpand=True,
        )
        choices = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=12,
            halign=Gtk.Align.CENTER,
            margin_top=18,
        )
        simple = Gtk.Button(label=_("Empezar en modo simple"))
        simple.add_css_class("pill")
        if not expert_mode:
            simple.add_css_class("suggested-action")
        simple.connect("clicked", lambda _button: self._finish(False))
        choices.append(simple)
        expert = Gtk.Button(label=_("Activar modo experto"))
        expert.add_css_class("pill")
        if expert_mode:
            expert.add_css_class("suggested-action")
        expert.connect("clicked", lambda _button: self._finish(True))
        choices.append(expert)
        mode_page.set_child(choices)
        self.stack.add_named(mode_page, "4")

        self._page_count = 5

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.back_button = Gtk.Button(label=_("Anterior"))
        self.back_button.connect("clicked", lambda _button: self._move(-1))
        footer.append(self.back_button)
        self.progress = Gtk.Label(hexpand=True)
        self.progress.add_css_class("dim-label")
        footer.append(self.progress)
        self.next_button = Gtk.Button(label=_("Siguiente"))
        self.next_button.add_css_class("suggested-action")
        self.next_button.connect("clicked", lambda _button: self._move(1))
        footer.append(self.next_button)
        content.append(footer)

        self._refresh_navigation()

    def _move(self, delta: int) -> None:
        self._page = min(max(0, self._page + delta), self._page_count - 1)
        self.stack.set_visible_child_name(str(self._page))
        self._refresh_navigation()

    def _refresh_navigation(self) -> None:
        self.back_button.set_sensitive(self._page > 0)
        self.next_button.set_visible(self._page < self._page_count - 1)
        self.progress.set_text(
            _("Paso {current} de {total}").format(
                current=self._page + 1,
                total=self._page_count,
            )
        )

    def _finish(self, expert_mode: bool) -> None:
        self._on_mode_selected(expert_mode)
        self.close()


class SyncHistoryDialog(Adw.Dialog):
    """Dialog for viewing and removing completed automatic synchronizations."""

    def __init__(self, preferences: UserPreferences) -> None:
        super().__init__(title=_("Historial de sincronizaciones"), content_width=720, content_height=520)
        self._preferences = preferences

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)

        scrolled = Gtk.ScrolledWindow(vexpand=True)
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=16,
            margin_bottom=18,
            margin_start=18,
            margin_end=18,
        )
        scrolled.set_child(box)

        self._list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self._list.add_css_class("boxed-list")
        box.append(self._list)

        self._clear_button = Gtk.Button(label=_("Borrar todo"), halign=Gtk.Align.END)
        self._clear_button.add_css_class("destructive-action")
        self._clear_button.connect("clicked", self._confirm_clear)
        box.append(self._clear_button)

        toolbar.set_content(scrolled)
        self.set_child(toolbar)
        self._refresh()

    def _refresh(self) -> None:
        while child := self._list.get_first_child():
            self._list.remove(child)
        if not self._preferences.sync_history:
            empty_status = Adw.StatusPage(
                icon_name="document-open-recent-symbolic",
                title=_("Sin historial"),
                description=_("Las sincronizaciones automáticas que realices aparecerán aquí."),
            )
            self._list.append(empty_status)
        for entry in self._preferences.sync_history:
            master = Path(entry.master_path).name
            source = Path(entry.source_path).name
            output = f" · {_('Exportado')}: {Path(entry.output_path).name}" if entry.output_path else ""
            row = Adw.ActionRow(
                title=f"{master} ← {source}",
                subtitle=_("{date} · Offset {offset:+.3f} s · Tempo {tempo:.6f} · Confianza {confidence:.0%} · {verdict}{output}").format(
                    date=entry.timestamp.replace("T", " ").split(".")[0].removesuffix("+00:00"),
                    offset=entry.offset,
                    tempo=entry.tempo,
                    confidence=entry.confidence,
                    verdict=entry.verdict,
                    output=output,
                ),
            )
            delete = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER)
            delete.add_css_class("flat")
            delete.set_tooltip_text(_("Eliminar del historial"))
            delete.connect("clicked", lambda _button, item=entry: self._confirm_delete(item))
            row.add_suffix(delete)
            self._list.append(row)
        self._clear_button.set_sensitive(bool(self._preferences.sync_history))

    def _confirm_delete(self, entry: SyncHistoryEntry) -> None:
        dialog = Adw.AlertDialog.new(_("Eliminar entrada del historial"), _("Esta acción no se puede deshacer."))
        dialog.add_response("cancel", _("Cancelar"))
        dialog.add_response("delete", _("Eliminar"))
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", lambda _dialog, response: self._delete_if_confirmed(entry, response))
        dialog.present(self)

    def _delete_if_confirmed(self, entry: SyncHistoryEntry, response: str) -> None:
        if response == "delete":
            self._preferences.delete_sync_history(entry)
            self._preferences.save()
            self._refresh()

    def _confirm_clear(self, _button: Gtk.Button) -> None:
        dialog = Adw.AlertDialog.new(_("Borrar todo el historial"), _("Se eliminarán todas las sincronizaciones guardadas."))
        dialog.add_response("cancel", _("Cancelar"))
        dialog.add_response("clear", _("Borrar todo"))
        dialog.set_response_appearance("clear", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", self._clear_if_confirmed)
        dialog.present(self)

    def _clear_if_confirmed(self, _dialog: Adw.AlertDialog, response: str) -> None:
        if response == "clear":
            self._preferences.clear_sync_history()
            self._preferences.save()
            self._refresh()


class PreferencesDialog(Adw.PreferencesDialog):
    """Application preferences, appearance, mode and credits."""

    def __init__(
        self,
        preferences: UserPreferences,
        on_theme_changed: Callable[[str], None],
        on_expert_changed: Callable[[bool], None],
        on_show_setup: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(title=_("Preferencias"))
        self._preferences = preferences
        self._on_theme_changed = on_theme_changed
        self._on_expert_changed = on_expert_changed

        page = Adw.PreferencesPage(title=_("General"), icon_name="preferences-system-symbolic")
        self.add(page)

        appearance = Adw.PreferencesGroup(title=_("Apariencia"))
        page.add(appearance)
        self.theme_row = Adw.ComboRow(
            title=_("Tema"),
            subtitle=_("Usar el tema del sistema o forzar el modo claro u oscuro"),
            model=Gtk.StringList.new([_("Sistema"), _("Claro"), _("Oscuro")]),
        )
        self.theme_row.set_selected({"system": 0, "light": 1, "dark": 2}[preferences.theme])
        self.theme_row.connect("notify::selected", self._theme_selected)
        appearance.add(self.theme_row)

        language_group = Adw.PreferencesGroup(title=_("Idioma"))
        page.add(language_group)
        self.language_row = Adw.ComboRow(
            title=_("Idioma de la interfaz"),
            subtitle=_("El cambio se aplicará al reiniciar WaveSync"),
            model=Gtk.StringList.new([_("Español"), "English", "Català"]),
        )
        self.language_row.set_selected({"es": 0, "en": 1, "ca": 2}[preferences.ui_language])
        self.language_row.connect("notify::selected", self._language_selected)
        language_group.add(self.language_row)

        mode_group = Adw.PreferencesGroup(
            title=_("Modo de uso"),
            description=_("El modo simple oculta controles avanzados sin cambiar el resultado."),
        )
        page.add(mode_group)
        mode_row = Adw.ActionRow(
            title=_("Modo experto"),
            subtitle=_("Mostrar calibración, procesamiento y opciones avanzadas de exportación"),
        )
        self.expert_switch = Gtk.Switch(active=preferences.expert_mode, valign=Gtk.Align.CENTER)
        self.expert_switch.connect("notify::active", self._expert_toggled)
        mode_row.add_suffix(self.expert_switch)
        mode_row.set_activatable_widget(self.expert_switch)
        mode_group.add(mode_row)

        credits = Adw.PreferencesGroup(title=_("Créditos"))
        page.add(credits)
        author_row = Adw.ActionRow(
            title=_("Creado por unaibenidorm"),
            subtitle=_("Autor y mantenedor de WaveSync"),
        )
        github = Gtk.LinkButton(
            uri="https://github.com/unaibenidorm",
            label=_("GitHub"),
            valign=Gtk.Align.CENTER,
        )
        author_row.add_suffix(github)
        credits.add(author_row)

    def _theme_selected(self, row: Adw.ComboRow, _pspec) -> None:
        theme = ("system", "light", "dark")[int(row.get_selected())]
        self._preferences.theme = theme
        self._preferences.save()
        self._on_theme_changed(theme)

    def _language_selected(self, row: Adw.ComboRow, _pspec) -> None:
        self._preferences.ui_language = ("es", "en", "ca")[int(row.get_selected())]
        self._preferences.save()

    def _expert_toggled(self, switch: Gtk.Switch, _pspec) -> None:
        enabled = bool(switch.get_active())
        self._preferences.expert_mode = enabled
        self._preferences.save()
        self._on_expert_changed(enabled)
