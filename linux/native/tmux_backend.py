#!/usr/bin/env python3
"""Small, argv-safe tmux backend used by the Linux Oracle desktop app."""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence


_PANE_ID = re.compile(r"%[0-9]+\Z")
_SESSION_ID = re.compile(r"\$[0-9]+\Z")
_SOCKET_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\Z")
_PANE_FIELDS = (
    "session_id",
    "session_name",
    "window_id",
    "window_name",
    "pane_id",
    "pane_index",
    "pane_title",
    "pane_current_command",
    "pane_current_path",
    "pane_active",
)
# q: makes each value shell-safe. A prefix preserves empty fields, then shlex
# reverses tmux's escaping without a delimiter that user metadata can contain.
_PANE_FORMAT = " ".join(f"x#{{q:{field}}}" for field in _PANE_FIELDS)


class TmuxError(RuntimeError):
    """Raised when tmux rejects an operation or cannot be reached."""

    def __init__(self, argv: Sequence[str], returncode: int, stderr: str):
        self.argv = list(argv)
        self.returncode = returncode
        self.stderr = stderr
        detail = stderr.strip() or f"tmux exited with status {returncode}"
        super().__init__(detail)


class TmuxBackend:
    def __init__(self, socket_name: str | None = None):
        if socket_name is not None and (
            not isinstance(socket_name, str) or not _SOCKET_NAME.fullmatch(socket_name)
        ):
            raise ValueError("socket_name must contain only letters, digits, _, ., or -")
        self.socket_name = socket_name

    def _argv(self, *args: str) -> list[str]:
        argv = ["tmux"]
        if self.socket_name is not None:
            argv.extend(("-L", self.socket_name))
        argv.extend(args)
        return argv

    def _run(self, *args: str, absent_ok: bool = False) -> str:
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
            raise TmuxError(argv, 127, "tmux executable not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise TmuxError(argv, 124, "tmux command timed out") from exc
        if completed.returncode == 0:
            return completed.stdout
        if absent_ok and self._is_absent_server(completed.stderr):
            return ""
        raise TmuxError(argv, completed.returncode, completed.stderr)

    @staticmethod
    def _is_absent_server(stderr: str) -> bool:
        message = stderr.strip().lower()
        return (
            "no server running on " in message
            or (
                (
                    "error connecting to " in message
                    or "failed to connect to server" in message
                )
                and (
                    "no such file or directory" in message
                    or "connection refused" in message
                )
            )
        )

    @staticmethod
    def _pane_id(pane_id: str) -> str:
        if not isinstance(pane_id, str) or not _PANE_ID.fullmatch(pane_id):
            raise ValueError("pane_id must be a tmux stable pane ID such as %3")
        return pane_id

    @staticmethod
    def _session_id(session_id: str) -> str:
        if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
            raise ValueError("session_id must be a tmux stable session ID such as $1")
        return session_id

    @staticmethod
    def _parse_panes(output: str) -> list[dict[str, Any]]:
        try:
            tokens = shlex.split(output, posix=True)
        except ValueError as exc:
            raise RuntimeError("tmux returned malformed pane records") from exc
        if len(tokens) % len(_PANE_FIELDS) != 0 or any(
            not token.startswith("x") for token in tokens
        ):
            raise RuntimeError("tmux returned unexpected pane records")
        records = []
        for offset in range(0, len(tokens), len(_PANE_FIELDS)):
            fields = [token[1:] for token in tokens[offset : offset + len(_PANE_FIELDS)]]
            records.append(TmuxBackend._pane_record(fields))
        return records

    @staticmethod
    def _pane_record(fields: list[str]) -> dict[str, Any]:
        return {
            "session_id": fields[0],
            "session_name": fields[1],
            "window_id": fields[2],
            "window_name": fields[3],
            "pane_id": fields[4],
            "pane_index": fields[5],
            "title": fields[6],
            "command": fields[7],
            "cwd": fields[8],
            "active": fields[9] == "1",
        }

    def list_panes(self) -> list[dict[str, Any]]:
        output = self._run("list-panes", "-a", "-F", _PANE_FORMAT, absent_ok=True)
        return self._parse_panes(output)

    def create_session(self, name: str, cwd: str | None = None) -> dict[str, Any]:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("session name must not be empty")
        if name.startswith("-") or any(char in name for char in (".", ":", "\x00", "\n", "\r")):
            raise ValueError("session name must not start with '-' or contain '.', ':', NUL, or newlines")
        argv = ["new-session", "-d", "-P", "-F", _PANE_FORMAT, "-s", name]
        if cwd is not None:
            if not isinstance(cwd, str) or not cwd or "\x00" in cwd:
                raise ValueError("cwd must be a non-empty path")
            path = Path(cwd).expanduser()
            if not path.is_dir():
                raise ValueError(f"cwd is not a directory: {cwd}")
            argv.extend(("-c", str(path)))
        panes = self._parse_panes(self._run(*argv))
        if not panes:
            raise RuntimeError("tmux created a session without returning its pane")
        if len(panes) != 1:
            raise RuntimeError("tmux returned multiple panes for one new session")
        return panes[0]

    def focus(self, pane_id: str) -> None:
        pane = self._pane_id(pane_id)
        self._run("select-window", "-t", pane)
        self._run("select-pane", "-t", pane)

    def send_text(self, pane_id: str, text: str, enter: bool = False) -> None:
        pane = self._pane_id(pane_id)
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        if "\x00" in text:
            raise ValueError("text must not contain NUL")
        if not isinstance(enter, bool):
            raise TypeError("enter must be a boolean")
        self._run("send-keys", "-t", pane, "-l", "--", text)
        if enter:
            self._run("send-keys", "-t", pane, "Enter")

    def capture(self, pane_id: str, lines: int = 200) -> str:
        pane = self._pane_id(pane_id)
        if isinstance(lines, bool) or not isinstance(lines, int) or lines <= 0:
            raise ValueError("lines must be a positive integer")
        return self._run("capture-pane", "-p", "-S", f"-{lines}", "-t", pane)

    def attach_argv(self, session_id: str) -> list[str]:
        session = self._session_id(session_id)
        return self._argv("attach-session", "-t", session)


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        print(json.dumps({"error": message}), file=sys.stderr)
        raise SystemExit(2)


def _parser() -> argparse.ArgumentParser:
    parser = _JsonArgumentParser(description="JSON CLI for the Oracle tmux backend")
    parser.add_argument("--socket", dest="socket_name", help="use a named tmux socket")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")

    create = commands.add_parser("create")
    create.add_argument("name")
    create.add_argument("--cwd")

    focus = commands.add_parser("focus")
    focus.add_argument("pane_id")

    send = commands.add_parser("send")
    send.add_argument("pane_id")
    send.add_argument("text")
    send.add_argument("--enter", action="store_true")

    read = commands.add_parser("read")
    read.add_argument("pane_id")
    read.add_argument("--lines", type=int, default=200)

    attach = commands.add_parser("attach-argv")
    attach.add_argument("session_id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        backend = TmuxBackend(args.socket_name)
        if args.command == "list":
            result: Any = backend.list_panes()
        elif args.command == "create":
            result = backend.create_session(args.name, args.cwd)
        elif args.command == "focus":
            backend.focus(args.pane_id)
            result = {"ok": True}
        elif args.command == "send":
            backend.send_text(args.pane_id, args.text, args.enter)
            result = {"ok": True}
        elif args.command == "read":
            result = {"pane_id": args.pane_id, "text": backend.capture(args.pane_id, args.lines)}
        else:
            result = backend.attach_argv(args.session_id)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        print(json.dumps({"error": str(exc), "type": type(exc).__name__}), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
