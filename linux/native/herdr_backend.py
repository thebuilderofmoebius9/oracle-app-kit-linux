#!/usr/bin/env python3
"""argv-safe Herdr adapter for the Linux Oracle desktop app."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Sequence


_SESSION_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\Z")
_WORKSPACE_ID = re.compile(r"w[0-9]+\Z")
_PANE_ID = re.compile(r"w[0-9]+:p[0-9]+\Z")
_TERMINAL_ID = re.compile(r"term_[A-Za-z0-9]+\Z")


class HerdrError(RuntimeError):
    """Raised when Herdr rejects an operation or returns malformed data."""

    def __init__(self, argv: Sequence[str], returncode: int, stderr: str):
        self.argv = list(argv)
        self.returncode = returncode
        self.stderr = stderr
        detail = stderr.strip() or f"herdr exited with status {returncode}"
        super().__init__(detail)


class HerdrBackend:
    def __init__(self, session_name: str = "default", executable: str = "herdr"):
        if not isinstance(session_name, str) or not _SESSION_NAME.fullmatch(session_name):
            raise ValueError("Herdr session names may contain only letters, digits, _, ., or -")
        if not isinstance(executable, str) or not executable or "\x00" in executable:
            raise ValueError("Herdr executable must be a non-empty path or command")
        self.session_name = session_name
        self.executable = executable
        self._panes: dict[str, dict[str, Any]] = {}

    @staticmethod
    def discover_sessions(executable: str = "herdr") -> list[str]:
        argv = [executable, "session", "list", "--json"]
        try:
            completed = subprocess.run(
                argv,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except FileNotFoundError as exc:
            raise HerdrError(argv, 127, "herdr executable not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise HerdrError(argv, 124, "herdr command timed out") from exc
        if completed.returncode != 0:
            raise HerdrError(argv, completed.returncode, completed.stderr)
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise HerdrError(argv, 1, "herdr returned malformed session JSON") from exc
        sessions = payload.get("sessions") if isinstance(payload, dict) else None
        if not isinstance(sessions, list):
            raise HerdrError(argv, 1, "herdr session list omitted sessions")
        names = []
        for session in sessions:
            if not isinstance(session, dict) or session.get("running") is not True:
                continue
            name = session.get("name")
            if isinstance(name, str) and _SESSION_NAME.fullmatch(name):
                names.append(name)
        return names

    def _argv(self, *args: str) -> list[str]:
        return [self.executable, "--session", self.session_name, *args]

    def _run(self, *args: str) -> str:
        argv = self._argv(*args)
        try:
            completed = subprocess.run(
                argv,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except FileNotFoundError as exc:
            raise HerdrError(argv, 127, "herdr executable not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise HerdrError(argv, 124, "herdr command timed out") from exc
        if completed.returncode != 0:
            raise HerdrError(argv, completed.returncode, completed.stderr)
        return completed.stdout

    def _run_json(self, *args: str) -> dict[str, Any]:
        output = self._run(*args)
        try:
            result = json.loads(output)
        except json.JSONDecodeError as exc:
            raise HerdrError(self._argv(*args), 1, "herdr returned malformed JSON") from exc
        if not isinstance(result, dict):
            raise HerdrError(self._argv(*args), 1, "herdr returned an unexpected JSON value")
        return result

    @staticmethod
    def _result(payload: dict[str, Any], expected_type: str) -> dict[str, Any]:
        result = payload.get("result")
        if not isinstance(result, dict) or result.get("type") != expected_type:
            raise RuntimeError(f"herdr returned an unexpected {expected_type} response")
        return result

    @staticmethod
    def _pane_id(pane_id: str) -> str:
        if not isinstance(pane_id, str) or not _PANE_ID.fullmatch(pane_id):
            raise ValueError("pane_id must be a Herdr stable pane ID such as w1:p1")
        return pane_id

    @staticmethod
    def _workspace_id(workspace_id: str) -> str:
        if not isinstance(workspace_id, str) or not _WORKSPACE_ID.fullmatch(workspace_id):
            raise ValueError("workspace_id must be a Herdr stable workspace ID such as w1")
        return workspace_id

    def list_panes(self) -> list[dict[str, Any]]:
        workspace_result = self._result(
            self._run_json("workspace", "list"), "workspace_list"
        )
        workspaces = workspace_result.get("workspaces")
        if not isinstance(workspaces, list):
            raise RuntimeError("herdr workspace list omitted workspaces")
        workspace_names: dict[str, str] = {}
        for workspace in workspaces:
            if not isinstance(workspace, dict):
                continue
            workspace_id = workspace.get("workspace_id")
            if isinstance(workspace_id, str) and _WORKSPACE_ID.fullmatch(workspace_id):
                label = workspace.get("label")
                workspace_names[workspace_id] = (
                    label.strip() if isinstance(label, str) and label.strip() else workspace_id
                )

        pane_result = self._result(self._run_json("pane", "list"), "pane_list")
        raw_panes = pane_result.get("panes")
        if not isinstance(raw_panes, list):
            raise RuntimeError("herdr pane list omitted panes")

        records: list[dict[str, Any]] = []
        counts: dict[str, int] = {}
        cached: dict[str, dict[str, Any]] = {}
        for pane in raw_panes:
            if not isinstance(pane, dict):
                continue
            pane_id = pane.get("pane_id")
            workspace_id = pane.get("workspace_id")
            tab_id = pane.get("tab_id")
            terminal_id = pane.get("terminal_id")
            if not (
                isinstance(pane_id, str)
                and _PANE_ID.fullmatch(pane_id)
                and isinstance(workspace_id, str)
                and _WORKSPACE_ID.fullmatch(workspace_id)
                and isinstance(tab_id, str)
                and isinstance(terminal_id, str)
                and _TERMINAL_ID.fullmatch(terminal_id)
            ):
                raise RuntimeError("herdr returned a pane with invalid identifiers")
            counts[workspace_id] = counts.get(workspace_id, 0) + 1
            title = pane.get("label") or pane.get("terminal_title_stripped") or pane.get("terminal_title")
            record = {
                "session_id": workspace_id,
                "session_name": workspace_names.get(workspace_id, workspace_id),
                "window_id": tab_id,
                "window_name": tab_id,
                "pane_id": pane_id,
                "pane_index": str(counts[workspace_id] - 1),
                "title": title if isinstance(title, str) else pane_id,
                "command": pane.get("agent") if isinstance(pane.get("agent"), str) else "",
                "cwd": pane.get("foreground_cwd") or pane.get("cwd") or "",
                "active": pane.get("focused") is True,
                "terminal_id": terminal_id,
                "herdr_session": self.session_name,
                "provider": "herdr",
            }
            records.append(record)
            cached[pane_id] = record
        self._panes = cached
        return records

    def create_session(self, name: str, cwd: str | None = None) -> dict[str, Any]:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("workspace name must not be empty")
        if "\x00" in name or "\n" in name or "\r" in name:
            raise ValueError("workspace name must not contain NUL or newlines")
        argv = ["workspace", "create", "--label", name, "--no-focus"]
        if cwd is not None:
            if not isinstance(cwd, str) or not cwd or "\x00" in cwd:
                raise ValueError("cwd must be a non-empty path")
            path = Path(cwd).expanduser()
            if not path.is_dir():
                raise ValueError(f"cwd is not a directory: {cwd}")
            argv.extend(("--cwd", str(path)))
        payload = self._run_json(*argv)
        result = payload.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("herdr workspace create omitted result")
        root_pane = result.get("root_pane")
        if not isinstance(root_pane, dict) or not isinstance(root_pane.get("pane_id"), str):
            raise RuntimeError("herdr workspace create omitted its root pane")
        pane_id = self._pane_id(root_pane["pane_id"])
        for record in self.list_panes():
            if record["pane_id"] == pane_id:
                return record
        raise RuntimeError("new Herdr workspace was not visible after creation")

    def focus(self, pane_id: str) -> None:
        # The native app attaches the terminal directly. Changing Herdr's shared
        # UI focus would surprise users of other Herdr clients.
        self._record(self._pane_id(pane_id))

    def send_text(self, pane_id: str, text: str, enter: bool = False) -> None:
        pane = self._pane_id(pane_id)
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        if "\x00" in text:
            raise ValueError("text must not contain NUL")
        if not isinstance(enter, bool):
            raise TypeError("enter must be a boolean")
        if enter:
            self._run("pane", "run", pane, text)
        else:
            self._run("pane", "send-text", pane, text)

    def capture(self, pane_id: str, lines: int = 200) -> str:
        pane = self._pane_id(pane_id)
        if isinstance(lines, bool) or not isinstance(lines, int) or lines <= 0:
            raise ValueError("lines must be a positive integer")
        return self._run(
            "pane", "read", pane, "--source", "recent-unwrapped", "--lines", str(lines)
        )

    def attach_pane_argv(self, pane_id: str) -> list[str]:
        record = self._record(self._pane_id(pane_id))
        terminal_id = record["terminal_id"]
        return self._argv("terminal", "attach", terminal_id)

    def attach_argv(self, workspace_id: str) -> list[str]:
        workspace = self._workspace_id(workspace_id)
        panes = [record for record in self.list_panes() if record["session_id"] == workspace]
        if len(panes) != 1:
            raise ValueError(
                "Herdr workspace attach is ambiguous; attach a pane with attach_pane_argv"
            )
        return self.attach_pane_argv(panes[0]["pane_id"])

    def _record(self, pane_id: str) -> dict[str, Any]:
        record = self._panes.get(pane_id)
        if record is None:
            self.list_panes()
            record = self._panes.get(pane_id)
        if record is None:
            raise ValueError(f"unknown Herdr pane: {pane_id}")
        return record
