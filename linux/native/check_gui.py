#!/usr/bin/env python3
"""Headless acceptance check for the native GTK/VTE tmux client.

Run this under Xvfb. It always uses a unique tmux socket and temporary state.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from app import OracleWindow
from gi.repository import GLib, Gtk
from tmux_backend import TmuxBackend


TIMEOUT = 10.0


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


def exact_line(backend: TmuxBackend, pane_id: str, marker: str) -> bool:
    return marker in backend.capture(pane_id, 100).splitlines()


def tmux_value(socket_name: str, pane_id: str, field: str) -> str:
    result = subprocess.run(
        ["tmux", "-L", socket_name, "display-message", "-p", "-t", pane_id, field],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "tmux display-message failed")
    return result.stdout.strip()


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
        raise TimeoutError("window did not detach its tmux client")


def run_check() -> dict[str, Any]:
    socket_name = f"oracle-gui-test-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    backend = TmuxBackend(socket_name)
    temporary = tempfile.TemporaryDirectory(prefix="oracle-gui-check-")
    state_dir = Path(temporary.name) / "state"
    cwd = Path(temporary.name) / "workspace"
    cwd.mkdir()
    windows: list[OracleWindow] = []
    try:
        first = backend.create_session(f"gui first {uuid.uuid4().hex[:8]}", str(cwd))
        second = backend.create_session(f"gui second {uuid.uuid4().hex[:8]}", str(cwd))
        first_id = str(first["pane_id"])

        window = OracleWindow(backend, state_dir)
        windows.append(window)
        wait_for(lambda: len(window.panes) >= 2, "session list did not populate")
        window.selected_pane = first
        window.connect_selected()
        wait_for(
            lambda: window.child_pid is not None
            and window.connected_pane is not None
            and window.connected_pane["pane_id"] == first_id,
            "first pane did not attach",
        )

        marker = f"GUI_INPUT_{uuid.uuid4().hex}"
        command = f"printf '%s\\n' {shlex.quote(marker)}\n".encode()
        window.terminal.feed_child(list(command))
        wait_for(lambda: exact_line(backend, first_id, marker), "VTE input marker was not executed")

        initial_grid = tmux_value(socket_name, first_id, "#{pane_width}x#{pane_height}")
        width, height = (int(value) for value in initial_grid.split("x"))
        window.terminal.set_size(width + 9, height + 4)
        wait_for(
            lambda: tmux_value(socket_name, first_id, "#{pane_width}x#{pane_height}")
            != initial_grid,
            "VTE resize did not reach tmux",
        )
        resized_grid = tmux_value(socket_name, first_id, "#{pane_width}x#{pane_height}")

        before_switch_client = window.child_pid
        window.selected_pane = second
        window.connect_selected()
        window.selected_pane = first
        window.connect_selected()
        wait_for(
            lambda: window.child_pid is not None
            and window.child_pid != before_switch_client
            and not window.spawning
            and window.pending_connection is None
            and window.connect_button.get_sensitive()
            and window.connected_pane is not None
            and window.connected_pane["pane_id"] == first_id,
            "rapid switching did not settle on the latest pane",
        )
        rapid_marker = f"GUI_SWITCH_{uuid.uuid4().hex}"
        rapid_command = f"printf '%s\\n' {shlex.quote(rapid_marker)}\n".encode()
        window.terminal.feed_child(list(rapid_command))
        wait_for(
            lambda: exact_line(backend, first_id, rapid_marker),
            "rapid-switch input did not reach the latest pane",
        )
        if exact_line(backend, str(second["pane_id"]), rapid_marker):
            raise AssertionError("rapid-switch input reached the stale pane")

        old_client_pid = window.child_pid
        pane_pid = tmux_value(socket_name, first_id, "#{pane_pid}")
        window.reconnect()
        wait_for(
            lambda: window.child_pid is not None
            and window.child_pid != old_client_pid
            and window.connected_pane is not None
            and window.connected_pane["pane_id"] == first_id,
            "reconnect did not replace the tmux client",
        )
        reconnect_marker = f"GUI_RECONNECT_{uuid.uuid4().hex}"
        reconnect_command = f"printf '%s\\n' {shlex.quote(reconnect_marker)}\n".encode()
        window.terminal.feed_child(list(reconnect_command))
        wait_for(
            lambda: exact_line(backend, first_id, reconnect_marker),
            "VTE input failed after reconnect",
        )

        close_attached_window(window)
        window.destroy()
        windows.remove(window)
        if tmux_value(socket_name, first_id, "#{pane_pid}") != pane_pid:
            raise AssertionError("closing the window replaced or killed the pane shell")

        reopened = OracleWindow(backend, state_dir)
        windows.append(reopened)
        wait_for(
            lambda: reopened.selected_pane is not None
            and reopened.selected_pane["pane_id"] == first_id,
            "saved pane selection was not restored",
        )
        settle_until = time.monotonic() + 0.4
        while time.monotonic() < settle_until:
            pump()
            time.sleep(0.02)
        if reopened.child_pid is not None or reopened.spawning or reopened.pending_connection:
            raise AssertionError("restoring selection auto-attached a tmux client")

        reopened.connect_selected()
        wait_for(
            lambda: reopened.child_pid is not None
            and reopened.connected_pane is not None
            and reopened.connected_pane["pane_id"] == first_id,
            "restored selection did not attach on request",
        )
        if tmux_value(socket_name, first_id, "#{pane_pid}") != pane_pid:
            raise AssertionError("reopening the app replaced the pane shell")
        reopen_marker = f"GUI_REOPEN_{uuid.uuid4().hex}"
        reopen_command = f"printf '%s\\n' {shlex.quote(reopen_marker)}\n".encode()
        reopened.terminal.feed_child(list(reopen_command))
        wait_for(
            lambda: exact_line(backend, first_id, reopen_marker),
            "VTE input failed after reopening",
        )
        close_attached_window(reopened)
        reopened.destroy()
        windows.remove(reopened)

        return {
            "ok": True,
            "input": marker,
            "switch_input": rapid_marker,
            "reconnect_input": reconnect_marker,
            "reopen_input": reopen_marker,
            "grid": {"before": initial_grid, "after": resized_grid},
            "pane_pid": pane_pid,
            "selection_restored_without_attach": True,
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
        socket_root = Path(os.environ.get("TMUX_TMPDIR", tempfile.gettempdir()))
        (socket_root / f"tmux-{os.getuid()}" / socket_name).unlink(missing_ok=True)
        temporary.cleanup()


def main() -> int:
    if not os.environ.get("DISPLAY"):
        print(json.dumps({"ok": False, "error": "DISPLAY is required; run under Xvfb"}))
        return 2
    try:
        result = run_check()
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
