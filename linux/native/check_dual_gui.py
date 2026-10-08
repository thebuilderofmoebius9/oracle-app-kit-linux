#!/usr/bin/env python3
"""Headless acceptance check for the combined tmux and Herdr GTK client.

Run under Xvfb. The check owns a unique tmux socket and a unique named Herdr
session, then removes both. It never discovers or mutates the default sessions.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from app import OracleWindow
from combined_backend import CompositeBackend
from gi.repository import Gdk, GLib, Gtk
from herdr_backend import HerdrBackend
from tmux_backend import TmuxBackend


TIMEOUT = 12.0


def pump() -> None:
    context = GLib.MainContext.default()
    while context.pending():
        context.iteration(False)


def wait_for(predicate: Callable[[], bool], label: str, timeout: float = TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pump()
        if predicate():
            return
        time.sleep(0.02)
    raise TimeoutError(label)


def exact_line(backend: CompositeBackend, pane_id: str, marker: str) -> bool:
    return marker in backend.capture(pane_id, 100).splitlines()


def herdr_json(session_name: str, *args: str) -> dict[str, Any]:
    completed = subprocess.run(
        ["herdr", "--session", session_name, *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "Herdr command failed")
    value = json.loads(completed.stdout)
    if not isinstance(value, dict):
        raise RuntimeError("Herdr returned an unexpected JSON value")
    return value


def herdr_process(session_name: str, pane_id: str) -> tuple[int, int]:
    result = herdr_json(session_name, "pane", "process-info", "--pane", pane_id).get("result")
    info = result.get("process_info") if isinstance(result, dict) else None
    if not isinstance(info, dict):
        raise RuntimeError("Herdr process-info omitted process_info")
    return int(info["shell_pid"]), int(info["foreground_process_group_id"])


def herdr_rows(session_name: str, pane_id: str) -> int:
    result = herdr_json(session_name, "pane", "get", pane_id).get("result")
    pane = result.get("pane") if isinstance(result, dict) else None
    scroll = pane.get("scroll") if isinstance(pane, dict) else None
    if not isinstance(scroll, dict):
        raise RuntimeError("Herdr pane info omitted scroll geometry")
    return int(scroll["viewport_rows"])


def close_attached_window(window: OracleWindow) -> None:
    timed_out = False

    def close() -> bool:
        window.close()
        return False

    def fallback() -> bool:
        nonlocal timed_out
        timed_out = True
        Gtk.main_quit()
        return False

    GLib.idle_add(close)
    timer = GLib.timeout_add(3_000, fallback)
    Gtk.main()
    if not timed_out:
        GLib.source_remove(timer)
    if timed_out or window.child_pid is not None:
        raise TimeoutError("window did not detach its terminal client")


def descendants(widget: Gtk.Widget) -> list[Gtk.Widget]:
    found = [widget]
    if isinstance(widget, Gtk.Container):
        for child in widget.get_children():
            found.extend(descendants(child))
    return found


def sanitize_sidebar(window: OracleWindow) -> None:
    sanitized = []
    for pane in window.panes:
        item = dict(pane)
        if pane.get("provider") == "herdr":
            item.update(
                session_name="Herdr test workspace",
                window_name="Terminal",
                title="Herdr terminal",
            )
        else:
            item.update(
                session_name="tmux test workspace",
                window_name="Terminal",
                title="tmux terminal",
            )
        item.update(command="shell", cwd="isolated fixture cwd")
        sanitized.append(item)
    window._populate_panes(window.refresh_serial, sanitized)


def save_screenshot(window: OracleWindow, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    surface = window.get_window()
    pixbuf = Gdk.pixbuf_get_from_window(
        surface,
        0,
        0,
        window.get_allocated_width(),
        window.get_allocated_height(),
    )
    if pixbuf is None:
        raise RuntimeError("GTK did not return pixels for the screenshot")
    pixbuf.savev(str(destination), "png", [], [])
    if not destination.is_file() or destination.stat().st_size == 0:
        raise RuntimeError("screenshot was not saved")


def verify_atom_dialog(window: OracleWindow) -> str:
    window._show_atom_status(None)  # type: ignore[arg-type]

    def dialog_and_view() -> tuple[Gtk.Dialog | None, Gtk.TextView | None]:
        for top in Gtk.Window.list_toplevels():
            if isinstance(top, Gtk.Dialog) and top is not window:
                views = [item for item in descendants(top) if isinstance(item, Gtk.TextView)]
                if views:
                    return top, views[0]
        return None, None

    wait_for(
        lambda: (
            dialog_and_view()[1] is not None
            and "Queue: database not found"
            in dialog_and_view()[1].get_buffer().get_text(  # type: ignore[union-attr]
                dialog_and_view()[1].get_buffer().get_start_iter(),  # type: ignore[union-attr]
                dialog_and_view()[1].get_buffer().get_end_iter(),  # type: ignore[union-attr]
                True,
            )
        ),
        "Atom status dialog did not render the isolated missing database",
    )
    dialog, view = dialog_and_view()
    if dialog is None or view is None:
        raise RuntimeError("Atom status dialog disappeared")
    buffer = view.get_buffer()
    text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
    dialog.response(Gtk.ResponseType.CLOSE)
    pump()
    return text


def run_check(screenshot: Path | None = None) -> dict[str, Any]:
    suffix = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
    socket_name = f"oracle-dual-test-{suffix}"
    herdr_session = f"oracle-dual-test-{suffix}"
    temporary = tempfile.TemporaryDirectory(prefix="oracle-dual-gui-")
    state_dir = Path(temporary.name) / "state"
    cwd = Path(temporary.name) / "workspace"
    cwd.mkdir()
    tmux = TmuxBackend(socket_name)
    herdr = HerdrBackend(herdr_session)
    backend = CompositeBackend(
        socket_name,
        [herdr_session],
        tmux_backend=tmux,
        herdr_backends=[herdr],
    )
    windows: list[OracleWindow] = []
    server: subprocess.Popen[bytes] | None = None
    try:
        server = subprocess.Popen(
            ["herdr", "--session", herdr_session, "server"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        def herdr_ready() -> bool:
            if server is None or server.poll() is not None:
                return False
            try:
                return herdr.list_panes() == []
            except (OSError, RuntimeError):
                return False

        wait_for(herdr_ready, "Herdr fixture did not start")

        tmux_pane = backend.create_session(f"tmux {suffix}", str(cwd), provider="tmux")
        herdr_pane = backend.create_session(f"Herdr {suffix}", str(cwd), provider="herdr")
        tmux_id = str(tmux_pane["pane_id"])
        herdr_id = str(herdr_pane["pane_id"])
        raw_herdr_id = str(herdr_pane["raw_pane_id"])

        missing_atom_database = Path(temporary.name) / "missing-atom-native.sqlite"
        window = OracleWindow(backend, state_dir, atom_database=missing_atom_database)
        windows.append(window)
        wait_for(
            lambda: {pane.get("provider") for pane in window.panes} == {"tmux", "herdr"},
            "mixed provider list did not populate",
        )

        window.selected_pane = tmux_pane
        window.connect_selected()
        wait_for(
            lambda: window.child_pid is not None
            and window.connected_pane is not None
            and window.connected_pane["pane_id"] == tmux_id,
            "tmux pane did not attach",
        )
        tmux_marker = f"DUAL_TMUX_{uuid.uuid4().hex}"
        window.terminal.feed_child(f"printf '%s\\n' {shlex.quote(tmux_marker)}\n".encode())
        wait_for(lambda: exact_line(backend, tmux_id, tmux_marker), "tmux VTE input was not executed")
        if exact_line(backend, herdr_id, tmux_marker):
            raise AssertionError("tmux marker reached the Herdr pane")

        tmux_client = window.child_pid
        window.selected_pane = herdr_pane
        window.connect_selected()
        wait_for(
            lambda: window.child_pid is not None
            and window.child_pid != tmux_client
            and window.connected_pane is not None
            and window.connected_pane["pane_id"] == herdr_id,
            "Herdr pane did not attach after provider switch",
        )
        herdr_marker = f"DUAL_HERDR_{uuid.uuid4().hex}"
        window.terminal.feed_child(f"printf '%s\\n' {shlex.quote(herdr_marker)}\n".encode())
        wait_for(lambda: exact_line(backend, herdr_id, herdr_marker), "Herdr VTE input was not executed")
        if exact_line(backend, tmux_id, herdr_marker):
            raise AssertionError("Herdr marker reached the tmux pane")

        shell_pid, foreground_pgid = herdr_process(herdr_session, raw_herdr_id)
        initial_rows = herdr_rows(herdr_session, raw_herdr_id)
        width, height = window.get_size()
        window.resize(width + 100, height + 80)
        wait_for(
            lambda: herdr_rows(herdr_session, raw_herdr_id) != initial_rows,
            "VTE resize did not reach Herdr",
        )
        resized_rows = herdr_rows(herdr_session, raw_herdr_id)

        old_client = window.child_pid
        window.reconnect()
        wait_for(
            lambda: window.child_pid is not None
            and window.child_pid != old_client
            and window.connected_pane is not None
            and window.connected_pane["pane_id"] == herdr_id,
            "Herdr reconnect did not replace its client",
        )
        reconnect_marker = f"DUAL_RECONNECT_{uuid.uuid4().hex}"
        window.terminal.feed_child(f"printf '%s\\n' {shlex.quote(reconnect_marker)}\n".encode())
        wait_for(
            lambda: exact_line(backend, herdr_id, reconnect_marker),
            "Herdr VTE input failed after reconnect",
        )

        close_attached_window(window)
        window.destroy()
        windows.remove(window)
        if herdr_process(herdr_session, raw_herdr_id) != (shell_pid, foreground_pgid):
            raise AssertionError("closing the window replaced or killed the Herdr shell")

        reopened = OracleWindow(backend, state_dir, atom_database=missing_atom_database)
        windows.append(reopened)
        wait_for(
            lambda: reopened.selected_pane is not None
            and reopened.selected_pane["pane_id"] == herdr_id,
            "saved Herdr pane selection was not restored",
        )
        settle_until = time.monotonic() + 0.4
        while time.monotonic() < settle_until:
            pump()
            time.sleep(0.02)
        if reopened.child_pid is not None or reopened.spawning or reopened.pending_connection:
            raise AssertionError("restoring selection auto-attached a terminal client")

        reopened.connect_selected()
        wait_for(
            lambda: reopened.child_pid is not None
            and reopened.connected_pane is not None
            and reopened.connected_pane["pane_id"] == herdr_id,
            "restored Herdr selection did not attach on request",
        )
        if herdr_process(herdr_session, raw_herdr_id) != (shell_pid, foreground_pgid):
            raise AssertionError("reopening the app replaced the Herdr shell")
        reopen_marker = f"DUAL_REOPEN_{uuid.uuid4().hex}"
        reopened.terminal.feed_child(f"printf '%s\\n' {shlex.quote(reopen_marker)}\n".encode())
        wait_for(lambda: exact_line(backend, herdr_id, reopen_marker), "Herdr input failed after reopen")

        demo_marker = "Herdr connected - tmux available"
        demo_command = (
            "clear; export PS1='oracle-demo$ '; "
            f"printf '%s\\n' {shlex.quote(demo_marker)}\n"
        )
        reopened.terminal.feed_child(demo_command.encode())
        wait_for(
            lambda: exact_line(backend, herdr_id, demo_marker),
            "readable Herdr demo marker was not executed",
        )
        sanitize_sidebar(reopened)
        settle_until = time.monotonic() + 0.25
        while time.monotonic() < settle_until:
            pump()
            time.sleep(0.02)
        if screenshot is not None:
            save_screenshot(reopened, screenshot)
        atom_status = verify_atom_dialog(reopened)
        close_attached_window(reopened)
        reopened.destroy()
        windows.remove(reopened)

        return {
            "ok": True,
            "providers": ["tmux", "herdr"],
            "tmux_input": tmux_marker,
            "herdr_input": herdr_marker,
            "reconnect_input": reconnect_marker,
            "reopen_input": reopen_marker,
            "herdr_rows": {"before": initial_rows, "after": resized_rows},
            "herdr_shell_pid": shell_pid,
            "selection_restored_without_attach": True,
            "atom_status_dialog": "Queue: database not found" in atom_status,
            "screenshot": bool(
                screenshot is not None and screenshot.is_file() and screenshot.stat().st_size > 0
            ),
            "isolated": {"tmux_socket": socket_name, "herdr_session": herdr_session},
        }
    finally:
        for window in windows:
            window.closing = True
            window.executor.shutdown(wait=False, cancel_futures=True)
            window.connection_executor.shutdown(wait=False, cancel_futures=True)
            window.destroy()
        subprocess.run(
            ["tmux", "-L", socket_name, "kill-server"],
            check=False,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["herdr", "session", "stop", herdr_session, "--json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if server is not None:
            try:
                server.wait(timeout=3)
            except subprocess.TimeoutExpired:
                server.terminate()
                server.wait(timeout=3)
        subprocess.run(
            ["herdr", "session", "delete", herdr_session, "--json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        socket_root = Path(os.environ.get("TMUX_TMPDIR", tempfile.gettempdir()))
        (socket_root / f"tmux-{os.getuid()}" / socket_name).unlink(missing_ok=True)
        temporary.cleanup()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--screenshot", type=Path)
    args = parser.parse_args()
    if not os.environ.get("DISPLAY"):
        print(json.dumps({"ok": False, "error": "DISPLAY is required; run under Xvfb"}))
        return 2
    try:
        result = run_check(args.screenshot)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
