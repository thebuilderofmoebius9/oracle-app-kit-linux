#!/usr/bin/env python3
"""Native GTK/VTE tmux client for ARRA Oracles on Linux."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Sequence

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Pango", "1.0")
gi.require_version("Vte", "2.91")
from gi.repository import Gdk, GLib, Gtk, Pango, Vte  # noqa: E402

from tmux_backend import TmuxBackend


APP_ID = "arra-oracles-linux"
CSS = b"""
window { background: #11131a; color: #e8eaf0; }
#topbar { background: #171a23; border-bottom: 1px solid #2b3040; padding: 10px 12px; }
#brand { color: #f2f3f8; font-weight: 700; font-size: 17px; }
#sidebar { background: #171a23; border-right: 1px solid #2b3040; }
#sidebar-title { color: #8c93a8; font-weight: 700; font-size: 11px; padding: 14px 14px 7px; }
list { background: transparent; }
row { border-radius: 8px; margin: 2px 8px; padding: 0; }
row:hover { background: #222635; }
row:selected { background: #343c57; }
.session { color: #f1f2f6; font-weight: 700; }
.pane-title { color: #c7ccda; }
.pane-meta { color: #7f879d; font-size: 11px; }
.active-dot { color: #65d49a; font-size: 15px; }
#statusbar { background: #151821; border-top: 1px solid #2b3040; padding: 7px 12px; }
#status { color: #9299ad; }
#terminal-frame { background: #0c0e13; padding: 8px; }
button { background: #262b3a; color: #e8eaf0; border: 1px solid #3a4154; border-radius: 7px; padding: 6px 10px; }
button:hover { background: #303748; }
button.suggested-action { background: #7457d6; border-color: #8b72df; }
"""


def _default_state_dir() -> Path:
    root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return root / APP_ID


class OracleWindow(Gtk.Window):
    def __init__(
        self,
        backend: TmuxBackend,
        state_dir: Path,
        smoke_test: bool = False,
        screenshot: Path | None = None,
    ) -> None:
        super().__init__(title="ARRA Oracles · Linux")
        self.backend = backend
        self.state_dir = state_dir
        self.state_file = state_dir / "state.json"
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="oracle-ui")
        self.connection_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="oracle-connect")
        self.smoke_test = smoke_test
        self.screenshot = screenshot
        self.smoke_marker = f"ORACLE_VTE_{os.getpid()}_{int(time.time())}"
        self.smoke_stage = 0
        self.smoke_deadline = time.monotonic() + 15
        self.panes: list[dict[str, Any]] = []
        self.rows: dict[str, Gtk.ListBoxRow] = {}
        self.selected_pane: dict[str, Any] | None = None
        self.connected_pane: dict[str, Any] | None = None
        self.pending_connection: tuple[dict[str, Any], list[str]] | None = None
        self.child_pid: int | None = None
        self.child_pidfd: int | None = None
        self.spawning = False
        self.connection_serial = 0
        self.exit_code = 0
        self.refresh_serial = 0
        self.restoring_selection = False
        self.closing = False
        self.finished = False
        self._build_ui()
        self._load_state_hint()
        self.connect("delete-event", self._on_delete)
        self.set_default_size(1180, 760)
        self.set_size_request(780, 480)
        self.show_all()
        self.refresh()

    def _build_ui(self) -> None:
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(root)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        top.set_name("topbar")
        brand = Gtk.Label(label="ARRA Oracles · Linux", xalign=0)
        brand.set_name("brand")
        top.pack_start(brand, True, True, 0)
        self.connect_button = Gtk.Button(label="Connect")
        self.connect_button.get_style_context().add_class("suggested-action")
        self.connect_button.set_sensitive(False)
        self.connect_button.connect("clicked", lambda _button: self.connect_selected())
        top.pack_end(self.connect_button, False, False, 0)
        reconnect = Gtk.Button(label="Reconnect")
        reconnect.connect("clicked", lambda _button: self.reconnect())
        top.pack_end(reconnect, False, False, 0)
        refresh = Gtk.Button(label="Refresh")
        refresh.connect("clicked", lambda _button: self.refresh())
        top.pack_end(refresh, False, False, 0)
        create = Gtk.Button(label="New session")
        create.connect("clicked", self._show_create_dialog)
        top.pack_end(create, False, False, 0)
        root.pack_start(top, False, False, 0)

        paned = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)
        paned.set_position(310)
        root.pack_start(paned, True, True, 0)

        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        sidebar.set_name("sidebar")
        side_title = Gtk.Label(label="TMUX SESSIONS", xalign=0)
        side_title.set_name("sidebar-title")
        sidebar.pack_start(side_title, False, False, 0)
        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listbox.connect("row-selected", self._on_row_selected)
        self.listbox.connect("row-activated", lambda _box, _row: self.connect_selected())
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.add(self.listbox)
        sidebar.pack_start(scroll, True, True, 0)
        paned.pack1(sidebar, resize=False, shrink=False)

        terminal_frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        terminal_frame.set_name("terminal-frame")
        self.terminal = Vte.Terminal()
        self.terminal.set_scrollback_lines(20_000)
        self.terminal.set_scroll_on_output(False)
        self.terminal.set_scroll_on_keystroke(True)
        self.terminal.set_mouse_autohide(True)
        self.terminal.set_font(Pango.FontDescription("Monospace 11"))
        self.terminal.set_colors(
            Gdk.RGBA(0.89, 0.90, 0.94, 1),
            Gdk.RGBA(0.047, 0.055, 0.078, 1),
            [],
        )
        self.terminal.connect("child-exited", self._on_child_exited)
        terminal_frame.pack_start(self.terminal, True, True, 0)
        paned.pack2(terminal_frame, resize=True, shrink=False)

        statusbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        statusbar.set_name("statusbar")
        self.status = Gtk.Label(label="Discovering tmux sessions…", xalign=0)
        self.status.set_name("status")
        statusbar.pack_start(self.status, True, True, 0)
        self.connection = Gtk.Label(label="● Offline", xalign=1)
        statusbar.pack_end(self.connection, False, False, 0)
        root.pack_end(statusbar, False, False, 0)

    def _load_state_hint(self) -> None:
        self.restored_pane_id: str | None = None
        self.restored_session_id: str | None = None
        self.restored_session_name: str | None = None
        try:
            value = json.loads(self.state_file.read_text(encoding="utf-8"))
            pane_id = value.get("selected_pane_id") if isinstance(value, dict) else None
            saved_socket = value.get("socket_name") if isinstance(value, dict) else None
            if isinstance(pane_id, str) and saved_socket == self.backend.socket_name:
                self.restored_pane_id = pane_id
                self.restored_session_id = value.get("session_id")
                self.restored_session_name = value.get("session_name")
        except (OSError, ValueError, TypeError):
            pass

    def _save_state(self, pane_id: str) -> None:
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            temporary = self.state_file.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(
                    {
                        "socket_name": self.backend.socket_name,
                        "session_id": self.selected_pane.get("session_id") if self.selected_pane else None,
                        "session_name": self.selected_pane.get("session_name") if self.selected_pane else None,
                        "selected_pane_id": pane_id,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            temporary.replace(self.state_file)
        except OSError as exc:
            self._set_status(f"Could not save selection: {exc}", error=True)

    def _run_async(
        self,
        operation: Callable[[], Any],
        callback: Callable[[Any], None],
        error_prefix: str,
        *,
        executor: ThreadPoolExecutor | None = None,
        error_callback: Callable[[str], None] | None = None,
    ) -> None:
        future = (executor or self.executor).submit(operation)

        def done(completed: Future[Any]) -> None:
            try:
                result = completed.result()
            except Exception as exc:  # backend exceptions belong in the status bar
                if error_callback is not None:
                    GLib.idle_add(error_callback, str(exc))
                else:
                    GLib.idle_add(self._async_error, error_prefix, str(exc))
                return
            GLib.idle_add(callback, result)

        future.add_done_callback(done)

    def _async_error(self, prefix: str, detail: str) -> bool:
        self._set_status(f"{prefix}: {detail}", error=True)
        self.connect_button.set_sensitive(self.selected_pane is not None)
        if self.smoke_test:
            self._finish_smoke(False, f"{prefix}: {detail}")
        return False

    def refresh(self) -> None:
        self.refresh_serial += 1
        serial = self.refresh_serial
        self._set_status("Refreshing tmux sessions…")
        self._run_async(
            self.backend.list_panes,
            lambda panes: self._populate_panes(serial, panes),
            "Refresh failed",
        )

    def _populate_panes(self, serial: int, panes: list[dict[str, Any]]) -> bool:
        if serial != self.refresh_serial or self.closing:
            return False
        self.panes = panes
        selected_id = str(self.selected_pane["pane_id"]) if self.selected_pane else self.restored_pane_id
        connected_id = str(self.connected_pane["pane_id"]) if self.connected_pane else None
        self.selected_pane = None
        self.connect_button.set_sensitive(False)
        for child in self.listbox.get_children():
            self.listbox.remove(child)
        self.rows.clear()
        previous_group: tuple[str, str] | None = None
        restore_row: Gtk.ListBoxRow | None = None
        for pane in panes:
            group = (str(pane["session_id"]), str(pane["window_id"]))
            if group != previous_group:
                previous_group = group
                header = Gtk.ListBoxRow()
                header.set_selectable(False)
                label = Gtk.Label(
                    label=f"{pane['session_name']}  /  {pane['window_name']}",
                    xalign=0,
                )
                label.set_margin_start(10)
                label.set_margin_top(10)
                label.set_margin_bottom(4)
                label.get_style_context().add_class("session")
                header.add(label)
                self.listbox.add(header)
            row = self._pane_row(pane)
            self.rows[str(pane["pane_id"])] = row
            self.listbox.add(row)
            restored_identity_matches = (
                selected_id != self.restored_pane_id
                or (
                    pane.get("session_id") == self.restored_session_id
                    and pane.get("session_name") == self.restored_session_name
                )
            )
            if str(pane["pane_id"]) == selected_id and restored_identity_matches:
                restore_row = row
            if str(pane["pane_id"]) == connected_id:
                self.connected_pane = pane
        if connected_id is not None and not any(str(p["pane_id"]) == connected_id for p in panes):
            self.connected_pane = None
        self.listbox.show_all()
        if restore_row is not None and self.connected_pane is None:
            self.restoring_selection = True
            self.listbox.select_row(restore_row)
            self.restoring_selection = False
            self.selected_pane = restore_row.pane  # type: ignore[attr-defined]
            self.connect_button.set_sensitive(True)
            self._set_status("Previous pane selected · press Connect to attach")
        else:
            self._set_status(f"{len(panes)} panes across {len({p['session_id'] for p in panes})} sessions")
        if self.smoke_test:
            if panes:
                row = self.rows[str(panes[0]["pane_id"])]
                self.restoring_selection = True
                self.listbox.select_row(row)
                self.restoring_selection = False
                self.selected_pane = panes[0]
                self.connect_selected()
            else:
                self.state_dir.mkdir(parents=True, exist_ok=True)
                self._run_async(
                    lambda: self.backend.create_session(
                        f"oracle-smoke-{os.getpid()}", str(self.state_dir)
                    ),
                    lambda _pane: self.refresh(),
                    "Smoke fixture failed",
                )
        return False

    def _pane_row(self, pane: dict[str, Any]) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.pane = pane  # type: ignore[attr-defined]
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_margin_start(10)
        box.set_margin_end(10)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        dot = Gtk.Label(label="●" if pane["active"] else "·")
        dot.get_style_context().add_class("active-dot")
        box.pack_start(dot, False, False, 0)
        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title = (
            "Smoke terminal"
            if self.smoke_test
            else str(pane["title"] or pane["command"] or f"pane {pane['pane_index']}")
        )
        title_label = Gtk.Label(label=title, xalign=0, ellipsize=Pango.EllipsizeMode.END)
        title_label.get_style_context().add_class("pane-title")
        meta_text = (
            f"pane {pane['pane_index']} · isolated fixture"
            if self.smoke_test
            else f"pane {pane['pane_index']} · {pane['command']} · {pane['cwd']}"
        )
        meta = Gtk.Label(
            label=meta_text,
            xalign=0,
            ellipsize=Pango.EllipsizeMode.MIDDLE,
        )
        meta.get_style_context().add_class("pane-meta")
        labels.pack_start(title_label, False, False, 0)
        labels.pack_start(meta, False, False, 0)
        box.pack_start(labels, True, True, 0)
        row.add(box)
        return row

    def _on_row_selected(self, _listbox: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if row is None or not hasattr(row, "pane"):
            return
        self.selected_pane = row.pane  # type: ignore[attr-defined]
        self.connect_button.set_sensitive(True)
        if not self.restoring_selection:
            self.connect_selected()

    def connect_selected(self) -> None:
        if self.selected_pane is not None:
            self._prepare_connection(self.selected_pane)

    def reconnect(self) -> None:
        pane = self.connected_pane or self.selected_pane
        if pane is None:
            self._set_status("Select a pane before reconnecting", error=True)
            return
        self._prepare_connection(pane, force=True)

    def _prepare_connection(self, pane: dict[str, Any], force: bool = False) -> None:
        if self.closing:
            return
        self.connection_serial += 1
        serial = self.connection_serial
        self.pending_connection = None
        self.connect_button.set_sensitive(False)
        self.connection.set_text("● Connecting")
        self._set_status(f"Connecting to {pane['session_name']} / pane {pane['pane_index']}…")

        def prepare() -> list[str]:
            self.backend.focus(str(pane["pane_id"]))
            return self.backend.attach_argv(str(pane["session_id"]))

        self._run_async(
            prepare,
            lambda argv: self._queue_spawn(serial, pane, argv),
            "Connect failed",
            executor=self.connection_executor,
            error_callback=lambda detail: self._connection_error(serial, detail),
        )

    def _connection_error(self, serial: int, detail: str) -> bool:
        if serial == self.connection_serial and not self.closing:
            self._async_error("Connect failed", detail)
        return False

    def _queue_spawn(self, serial: int, pane: dict[str, Any], argv: list[str]) -> bool:
        if serial != self.connection_serial or self.closing:
            return False
        self.pending_connection = (pane, argv)
        if self.child_pid is not None:
            self._detach_client()
            GLib.timeout_add(2_000, self._detach_timeout, serial)
        elif self.spawning:
            pass
        else:
            self._spawn_pending()
        return False

    def _detach_client(self) -> None:
        if self.child_pid is None or self.child_pidfd is None:
            return
        try:
            signal.pidfd_send_signal(self.child_pidfd, signal.SIGHUP)
        except ProcessLookupError:
            self.child_pid = None
            os.close(self.child_pidfd)
            self.child_pidfd = None

    def _detach_timeout(self, serial: int) -> bool:
        if (
            serial == self.connection_serial
            and self.pending_connection is not None
            and self.child_pid is not None
        ):
            self.pending_connection = None
            self._async_error("Reconnect failed", "tmux client did not detach")
        return False

    def _spawn_pending(self) -> None:
        if self.pending_connection is None or self.closing:
            return
        pane, argv = self.pending_connection
        serial = self.connection_serial
        self.pending_connection = None
        self.spawning = True
        self.terminal.reset(True, True)
        env = [
            f"{key}={value}"
            for key, value in os.environ.items()
            if key not in {"TMUX", "TMUX_PANE", "TERM"}
        ]
        # Terminal.spawn_async owns child reaping; only disable parent env so a
        # GUI launched inside tmux does not attempt an illegal nested attach.
        flags = GLib.SpawnFlags(
            int(Vte.SPAWN_NO_PARENT_ENVV) | int(GLib.SpawnFlags.SEARCH_PATH_FROM_ENVP)
        )
        # Ubuntu's Vte-2.91 typelib exposes child_setup_data as the second None.
        self.terminal.spawn_async(
            Vte.PtyFlags.DEFAULT,
            str(pane.get("cwd") or Path.home()),
            argv,
            env,
            flags,
            None,
            None,
            -1,
            None,
            self._on_spawned,
            (serial, pane),
        )

    def _on_spawned(
        self,
        _terminal: Vte.Terminal,
        pid: int,
        error: GLib.Error | None,
        request: tuple[int, dict[str, Any]],
    ) -> None:
        serial, pane = request
        if error is not None or pid < 0:
            self.spawning = False
            self._async_error("Attach failed", str(error or "unknown VTE spawn failure"))
            return
        if self.closing or serial != self.connection_serial:
            self.child_pid = pid
            self.spawning = True
            try:
                stale_pidfd = os.pidfd_open(pid)
                self.child_pidfd = stale_pidfd
                try:
                    signal.pidfd_send_signal(stale_pidfd, signal.SIGHUP)
                except ProcessLookupError:
                    pass
            except ProcessLookupError:
                pass
            return
        self.spawning = False
        self.child_pid = pid
        try:
            self.child_pidfd = os.pidfd_open(pid)
        except ProcessLookupError:
            self.child_pid = None
            self._async_error("Attach failed", "tmux client exited before it could be tracked")
            return
        self.connected_pane = pane
        self.selected_pane = pane
        self.connect_button.set_sensitive(True)
        self.connection.set_text("● Connected")
        self._set_status(f"{pane['session_name']} / {pane['window_name']} / pane {pane['pane_index']}")
        self._save_state(str(pane["pane_id"]))
        self.terminal.grab_focus()
        if self.smoke_test:
            marker = f"{self.smoke_marker}_{self.smoke_stage}"
            GLib.timeout_add(350, self._send_smoke_marker, marker)

    def _send_smoke_marker(self, marker: str) -> bool:
        prefix = (
            "tmux set status off; clear; printf '\\033]2;terminal\\033\\\\'; "
            "export PS1='oracle-smoke$ '; "
            if self.smoke_stage == 0
            else ""
        )
        command = f"{prefix}printf '{marker}\\n'\n".encode("utf-8")
        self.terminal.feed_child(command)
        GLib.timeout_add(100, self._poll_smoke_marker, marker)
        return False

    def _on_child_exited(self, _terminal: Vte.Terminal, _status: int) -> None:
        if self.child_pidfd is not None:
            os.close(self.child_pidfd)
            self.child_pidfd = None
        self.child_pid = None
        self.spawning = False
        if self.closing:
            self._finish_close()
            return
        if self.pending_connection is not None:
            self._spawn_pending()
            return
        self.connected_pane = None
        self.connection.set_text("● Disconnected")
        self._set_status("tmux client disconnected · refresh or reconnect")

    def _poll_smoke_marker(self, marker: str) -> bool:
        if time.monotonic() > self.smoke_deadline:
            self._finish_smoke(False, f"timed out waiting for {marker}")
            return False

        def checked(output: str) -> None:
            if marker not in {line.strip() for line in output.splitlines()}:
                GLib.timeout_add(100, self._poll_smoke_marker, marker)
                return
            if self.smoke_stage == 0:
                self.smoke_stage = 1
                pane = self.connected_pane
                if pane is None:
                    self._finish_smoke(False, "connection disappeared before reconnect")
                    return
                self._prepare_connection(pane, force=True)
            else:
                self._finish_smoke(True, "VTE input survived detach and reconnect")

        pane = self.connected_pane
        if pane is None:
            self._finish_smoke(False, "no connected pane")
            return False
        self._run_async(
            lambda: self.backend.capture(str(pane["pane_id"]), 100),
            checked,
            "Smoke capture failed",
        )
        return False

    def _finish_smoke(self, ok: bool, detail: str) -> None:
        if self.closing:
            return
        screenshot_saved = False
        if ok and self.screenshot is not None:
            try:
                self.screenshot.parent.mkdir(parents=True, exist_ok=True)
                window = self.get_window()
                pixbuf = Gdk.pixbuf_get_from_window(window, 0, 0, self.get_allocated_width(), self.get_allocated_height())
                if pixbuf is not None:
                    pixbuf.savev(str(self.screenshot), "png", [], [])
                    screenshot_saved = self.screenshot.is_file() and self.screenshot.stat().st_size > 0
            except (GLib.Error, OSError):
                screenshot_saved = False
        pane = self.connected_pane or self.selected_pane or {}
        result = {
            "ok": ok,
            "detail": detail,
            "pane_id": pane.get("pane_id"),
            "session_id": pane.get("session_id"),
            "reconnected": self.smoke_stage == 1 and ok,
            "screenshot": screenshot_saved,
        }
        self.exit_code = 0 if ok else 1
        print(json.dumps(result, ensure_ascii=False), flush=True)
        self.closing = True
        self._detach_client()
        GLib.timeout_add(200, self._finish_close)

    def _show_create_dialog(self, _button: Gtk.Button) -> None:
        dialog = Gtk.Dialog(title="New tmux session", transient_for=self, modal=True)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Create", Gtk.ResponseType.OK)
        content = dialog.get_content_area()
        content.set_spacing(8)
        content.set_border_width(14)
        name = Gtk.Entry(placeholder_text="Session name")
        cwd = Gtk.Entry(placeholder_text="Working directory (optional)")
        content.pack_start(Gtk.Label(label="Create a persistent tmux session", xalign=0), False, False, 0)
        content.pack_start(name, False, False, 0)
        content.pack_start(cwd, False, False, 0)
        dialog.show_all()
        response = dialog.run()
        session_name, working_directory = name.get_text().strip(), cwd.get_text().strip()
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return
        self._run_async(
            lambda: self.backend.create_session(session_name, working_directory or None),
            self._created_session,
            "Create failed",
        )

    def _created_session(self, pane: dict[str, Any]) -> bool:
        self.restored_pane_id = str(pane["pane_id"])
        self.refresh()
        return False

    def _set_status(self, message: str, error: bool = False) -> None:
        self.status.set_text(("Error · " if error else "") + message)

    def _on_delete(self, _window: Gtk.Window, _event: Gdk.Event) -> bool:
        if self.closing:
            return True
        self.closing = True
        if self.child_pid is not None:
            self.hide()
            self._detach_client()
            GLib.timeout_add(500, self._finish_close)
            return True
        self._finish_close()
        return True

    def _finish_close(self) -> bool:
        if self.finished:
            return False
        self.finished = True
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.connection_executor.shutdown(wait=False, cancel_futures=True)
        if Gtk.main_level() > 0:
            Gtk.main_quit()
        return False


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ARRA Oracles native Linux tmux client")
    parser.add_argument("--socket", dest="socket_name", help="use an isolated named tmux socket")
    parser.add_argument("--state-dir", type=Path, default=_default_state_dir())
    parser.add_argument("--smoke-test", action="store_true", help="exercise VTE attach, input, and reconnect")
    parser.add_argument("--screenshot", type=Path, help="save a PNG after a successful smoke test")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.smoke_test and not args.socket_name:
        print(json.dumps({"ok": False, "detail": "--smoke-test requires --socket"}), file=sys.stderr)
        return 2
    window = OracleWindow(
        TmuxBackend(args.socket_name),
        args.state_dir,
        smoke_test=args.smoke_test,
        screenshot=args.screenshot,
    )
    window.present()
    Gtk.main()
    return window.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
