"""Main GTK4/Libadwaita window with ViewStack, ViewSwitcher, ToastOverlay and AlertDialog."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, Gtk  # noqa: E402

from wavesync import __version__  # noqa: E402
from wavesync.core.errors import FFmpegNotFoundError, WaveSyncError  # noqa: E402
from wavesync.core.ffmpeg import locate_binaries  # noqa: E402
from wavesync.core.i18n import _  # noqa: E402
from wavesync.ui.compare_view import CompareView  # noqa: E402
from wavesync.ui.export_view import ExportView  # noqa: E402
from wavesync.ui.inputs_view import InputsView  # noqa: E402
from wavesync.ui.preferences import (  # noqa: E402
    PreferencesDialog,
    SetupGuideDialog,
    SyncHistoryDialog,
    UserPreferences,
)
from wavesync.ui.resources import APP_ID, install_resources  # noqa: E402
from wavesync.ui.splash import StartupSplash  # noqa: E402
from wavesync.ui.state import AppState  # noqa: E402
from wavesync.ui.sync_view import SyncView  # noqa: E402

SYNC_ICON = "image-loading-symbolic"


class WaveSyncWindow(Adw.ApplicationWindow):
    """Main application window adhering to GNOME HIG."""

    def __init__(
        self,
        application: Adw.Application,
        *,
        compare_view: Gtk.Widget | None = None,
        export_view: Gtk.Widget | None = None,
        show_splash: bool = True,
    ) -> None:
        super().__init__(application=application, title="WaveSync", icon_name=APP_ID)
        install_resources()
        self.set_default_size(1040, 760)
        self.set_size_request(360, 480)
        self.preferences = UserPreferences.load()
        self.state = AppState(
            expert_mode=self.preferences.expert_mode,
        )
        self._preferences_dialog: PreferencesDialog | None = None
        self._history_dialog: SyncHistoryDialog | None = None
        self._setup_dialog: SetupGuideDialog | None = None
        self._has_synced_once: bool = bool(self.state.result is not None)
        self._apply_theme(self.preferences.theme)
        self._install_actions()

        self.toast_overlay = Adw.ToastOverlay()
        self.set_content(self.toast_overlay)

        # Gtk.Overlay hosting the app and, on top, the opaque startup splash.
        self.content_overlay = Gtk.Overlay()
        self.toast_overlay.set_child(self.content_overlay)

        toolbar_view = Adw.ToolbarView()
        self.content_overlay.set_child(toolbar_view)

        header = Adw.HeaderBar()
        self.view_stack = Adw.ViewStack()
        self.switcher = Adw.ViewSwitcher(
            stack=self.view_stack,
            policy=Adw.ViewSwitcherPolicy.WIDE,
        )
        header.set_title_widget(self.switcher)

        self.btn_history = Gtk.Button(
            icon_name="document-open-recent-symbolic",
            tooltip_text=_("Historial de sincronizaciones"),
        )
        self.btn_history.connect("clicked", lambda _b: self.show_history())
        header.pack_end(self._build_primary_menu())
        header.pack_end(self.btn_history)
        toolbar_view.add_top_bar(header)

        # Bottom switcher for narrow windows (HIG adaptive pattern).
        self.switcher_bar = Adw.ViewSwitcherBar(stack=self.view_stack)
        toolbar_view.add_bottom_bar(self.switcher_bar)
        breakpoint_ = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 720sp"))
        breakpoint_.add_setter(self.switcher_bar, "reveal", True)
        breakpoint_.add_setter(header, "show-title", False)
        self.add_breakpoint(breakpoint_)

        # Views
        self.inputs_view = InputsView(
            self.state,
            on_error=self.show_error_dialog,
            on_toast=self.show_toast,
            on_go_sync=lambda: self.switch_tab("sync"),
        )
        self.sync_view = SyncView(
            self.state,
            on_error=self.show_error_dialog,
            on_toast=self.show_toast,
            on_go_compare=lambda: self.switch_tab("compare"),
            on_sync_completed=self._on_sync_completed,
        )
        if compare_view is None:
            compare_view = CompareView(
                self.state,
                on_toast=self.show_toast,
                on_go_export=lambda: self.switch_tab("export"),
            )
        self.compare_view = compare_view
        if export_view is None:
            export_view = ExportView(
                self.state,
                on_error=self.show_error_dialog,
                on_toast=self.show_toast,
                preferences=self.preferences,
                on_export_start=self._pause_player,
            )
        self.export_view = export_view

        pages = (
            (self.inputs_view, "inputs", _("1. Entradas"), "folder-videos-symbolic"),
            (self.sync_view, "sync", _("2. Sincronización"), SYNC_ICON),
            (self.compare_view, "compare", _("3. Comparar y ajustar"), "multimedia-player-symbolic"),
            (self.export_view, "export", _("4. Exportar"), "document-save-symbolic"),
        )
        for widget, name, title, icon in pages:
            self.view_stack.add_titled_with_icon(widget, name, title, icon)
        self.view_stack.connect("notify::visible-child-name", self._on_tab_changed)
        toolbar_view.set_content(self.view_stack)
        self._update_tab_sensitivity()
        self.state.subscribe(self._on_state_changed)

        self._splash: StartupSplash | None = None
        if show_splash:
            self._splash = StartupSplash()
            self.content_overlay.add_overlay(self._splash)
            self.connect("map", self._on_first_map)

        self.connect("close-request", self._on_close_request)
        self._check_ffmpeg_on_startup()
        if not show_splash:
            self._after_startup()

    # -- tab sensitivity ------------------------------------------------
    def _update_tab_sensitivity(self) -> None:
        sensitive = self._has_synced_once
        # ViewStackPage does not expose Gtk's sensitivity property. View
        # switchers materialise their pages as toggle buttons, so update the
        # two tab buttons directly while keeping both pages visible.
        for switcher in (self.switcher, self.switcher_bar):
            buttons: list[Gtk.ToggleButton] = []

            def collect(widget: Gtk.Widget) -> None:
                child = widget.get_first_child()
                while child is not None:
                    if isinstance(child, Gtk.ToggleButton):
                        buttons.append(child)
                    else:
                        collect(child)
                    child = child.get_next_sibling()

            collect(switcher)
            if len(buttons) >= 4:
                buttons[2].set_sensitive(sensitive)
                buttons[3].set_sensitive(sensitive)

    def _on_sync_completed(self, res: SyncResult) -> None:
        self._has_synced_once = True
        self._update_tab_sensitivity()
        if self.state.master and self.state.source:
            self.preferences.add_sync_history(
                master_path=self.state.master.path,
                source_path=self.state.source.path,
                offset=res.offset,
                tempo=res.tempo,
                confidence=res.confidence,
                verdict=res.verdict.name,
            )
            self.preferences.save()

    # -- shared state ---------------------------------------------------
    def _on_state_changed(self, topic: str) -> None:
        if self.state.result is not None and not self._has_synced_once:
            self._has_synced_once = True
            self._update_tab_sensitivity()
        if topic == "syncing":
            if self.state.sync_in_progress and self.view_stack.get_visible_child() is self.compare_view:
                self.switch_tab("sync")

    # -- startup ---------------------------------------------------------
    def _on_first_map(self, _widget: Gtk.Widget) -> None:
        self._update_tab_sensitivity()
        self.disconnect_by_func(self._on_first_map)
        if self._splash is not None:
            self._splash.play(self._on_splash_finished)

    def _on_splash_finished(self) -> None:
        if self._splash is not None:
            self.content_overlay.remove_overlay(self._splash)
            self._splash = None
        self._after_startup()

    def _after_startup(self) -> None:
        if not self.preferences.setup_seen:
            self.show_setup_guide()

    # -- header menu & actions -------------------------------------------
    def _build_primary_menu(self) -> Gtk.MenuButton:
        menu = Gio.Menu()
        section = Gio.Menu()
        section.append(_("Preferencias"), "win.preferences")
        section.append(_("Guía de configuración"), "win.setup-guide")
        menu.append_section(None, section)
        about = Gio.Menu()
        about.append(_("Acerca de WaveSync"), "win.about")
        menu.append_section(None, about)
        return Gtk.MenuButton(
            icon_name="open-menu-symbolic",
            menu_model=menu,
            primary=True,
            tooltip_text=_("Menú principal"),
        )

    def _install_actions(self) -> None:
        for name, callback in (
            ("history", self.show_history),
            ("preferences", self.show_preferences),
            ("setup-guide", self.show_setup_guide),
            ("about", self.show_about),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda _a, _p, cb=callback: cb())
            self.add_action(action)

    def _on_close_request(self, _win: Adw.ApplicationWindow) -> bool:
        if isinstance(self.compare_view, CompareView):
            self.compare_view.player.cleanup()
        return False

    def _pause_player(self) -> None:
        if isinstance(self.compare_view, CompareView) and hasattr(self.compare_view, "player"):
            self.compare_view.player.pause()

    def _on_tab_changed(self, _stack, _pspec) -> None:
        if self.view_stack.get_visible_child_name() != "compare":
            self._pause_player()
        else:
            if isinstance(self.compare_view, CompareView):
                self.compare_view.on_tab_activated()

    def switch_tab(self, name: str) -> None:
        if name in ("compare", "export") and not self._has_synced_once:
            return
        self.view_stack.set_visible_child_name(name)

    # -- feedback ----------------------------------------------------------
    def show_toast(self, message: str) -> None:
        toast = Adw.Toast.new(message)
        toast.set_timeout(4)
        self.toast_overlay.add_toast(toast)

    def show_error_dialog(self, err: WaveSyncError) -> None:
        body = err.user_message
        if err.detail:
            body = f"{body}\n\n{err.detail}"
        dialog = Adw.AlertDialog.new(_("Error"), body)
        dialog.add_response("ok", _("Aceptar"))
        dialog.set_default_response("ok")
        dialog.set_close_response("ok")
        dialog.present(self)

    # -- dialogs -----------------------------------------------------------
    def show_history(self) -> None:
        if self._history_dialog is None:
            self._history_dialog = SyncHistoryDialog(self.preferences)
            self._history_dialog.connect("closed", self._on_history_closed)
        self._history_dialog.present(self)

    def _on_history_closed(self, _dialog: Adw.Dialog) -> None:
        self._history_dialog = None

    def show_preferences(self) -> None:
        if self._preferences_dialog is None:
            self._preferences_dialog = PreferencesDialog(
                self.preferences,
                self._apply_theme,
                self._set_expert_mode,
            )
            self._preferences_dialog.connect("closed", self._on_preferences_closed)
        self._preferences_dialog.present(self)

    def _on_preferences_closed(self, _dialog: Adw.Dialog) -> None:
        self._preferences_dialog = None

    def show_setup_guide(self) -> None:
        if self._preferences_dialog is not None:
            self._preferences_dialog.close()
        if self._setup_dialog is None:
            self._setup_dialog = SetupGuideDialog(self.state.expert_mode, self._on_setup_completed)
            self._setup_dialog.connect("closed", self._on_setup_closed)
        self._setup_dialog.present(self)

    def _on_setup_closed(self, _dialog: Adw.Dialog) -> None:
        self._setup_dialog = None
        if not self.preferences.setup_seen:
            # Closing the guide counts as "seen" so it does not reappear every launch.
            self.preferences.setup_seen = True
            self.preferences.save()

    def _on_setup_completed(self, expert_mode: bool) -> None:
        self.preferences.setup_seen = True
        self._set_expert_mode(expert_mode)

    def show_about(self) -> None:
        about = Adw.AboutDialog(
            application_name="WaveSync",
            application_icon=APP_ID,
            developer_name="unaibenidorm",
            version=__version__,
            website="https://github.com/unaibenidorm/WaveSync",
            comments=_(
                "Sincroniza una pista de audio de cualquier idioma con un vídeo máster "
                "sin recodificar el vídeo."
            ),
            license_type=Gtk.License.GPL_3_0,
        )
        about.present(self)

    # -- preferences -------------------------------------------------------
    def _apply_theme(self, theme: str) -> None:
        schemes = {
            "system": Adw.ColorScheme.DEFAULT,
            "light": Adw.ColorScheme.FORCE_LIGHT,
            "dark": Adw.ColorScheme.FORCE_DARK,
        }
        Adw.StyleManager.get_default().set_color_scheme(schemes.get(theme, Adw.ColorScheme.DEFAULT))

    def _set_expert_mode(self, enabled: bool) -> None:
        self.state.set_expert_mode(enabled)
        self.preferences.expert_mode = bool(enabled)
        self.preferences.save()
        self.show_toast(_("Modo experto activado") if enabled else _("Modo simple activado"))

    def _check_ffmpeg_on_startup(self) -> None:
        try:
            locate_binaries()
        except FFmpegNotFoundError as exc:
            self.show_error_dialog(exc)
