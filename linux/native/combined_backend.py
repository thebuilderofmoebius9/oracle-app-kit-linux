#!/usr/bin/env python3
"""Combined tmux and Herdr backend with collision-proof public IDs."""

from __future__ import annotations

import argparse
import json
import sys
import threading
from typing import Any, Iterable, Sequence

from herdr_backend import HerdrBackend
from tmux_backend import TmuxBackend


class CompositeBackend:
    def __init__(
        self,
        socket_name: str | None = None,
        herdr_sessions: Iterable[str] | None = None,
        *,
        tmux_backend: TmuxBackend | None = None,
        herdr_backends: Iterable[HerdrBackend] | None = None,
    ):
        self.socket_name = socket_name
        self.tmux = tmux_backend if tmux_backend is not None else TmuxBackend(socket_name)
        self._configured_sessions = list(herdr_sessions) if herdr_sessions is not None else None
        self._explicit_herdr = list(herdr_backends) if herdr_backends is not None else None
        self.herdr = list(self._explicit_herdr or [])
        self.errors: dict[str, str] = {}
        self._panes: dict[str, tuple[Any, dict[str, Any]]] = {}
        self._lock = threading.RLock()
        self._refresh_serial = 0

    @staticmethod
    def _tmux_id(raw_id: str) -> str:
        return f"tmux:{raw_id}"

    @staticmethod
    def _herdr_id(session_name: str, raw_id: str) -> str:
        return f"herdr:{session_name}:{raw_id}"

    @staticmethod
    def _provider(record_id: str) -> str:
        if record_id.startswith("tmux:"):
            return "tmux"
        if record_id.startswith("herdr:"):
            return "herdr"
        raise ValueError("ID is not namespaced by a supported provider")

    def _namespace(self, backend: Any, record: dict[str, Any], provider: str) -> dict[str, Any]:
        item = dict(record)
        if provider == "tmux":
            prefix = self._tmux_id
            provider_label = "tmux"
        else:
            session_name = backend.session_name
            prefix = lambda value: self._herdr_id(session_name, value)
            provider_label = f"herdr/{session_name}"
        for field in ("session_id", "window_id", "pane_id"):
            item[field] = prefix(str(record[field]))
        item["session_name"] = f"{provider_label} · {record['session_name']}"
        item["provider"] = provider
        item["provider_label"] = provider_label
        item["raw_session_id"] = record["session_id"]
        item["raw_pane_id"] = record["pane_id"]
        return item

    def list_panes(self) -> list[dict[str, Any]]:
        with self._lock:
            self._refresh_serial += 1
            refresh_serial = self._refresh_serial
        errors: dict[str, str] = {}
        pane_index: dict[str, tuple[Any, dict[str, Any]]] = {}
        records: list[dict[str, Any]] = []
        if self._explicit_herdr is not None:
            herdr_backends = list(self._explicit_herdr)
        else:
            try:
                sessions = (
                    HerdrBackend.discover_sessions()
                    if self._configured_sessions is None
                    else self._configured_sessions
                )
                herdr_backends = [HerdrBackend(session_name) for session_name in sessions]
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                herdr_backends = []
                errors["herdr"] = str(exc)
        providers: list[tuple[str, Any]] = [("tmux", self.tmux)]
        providers.extend(("herdr", backend) for backend in herdr_backends)
        for provider, backend in providers:
            label = "tmux" if provider == "tmux" else f"herdr/{backend.session_name}"
            try:
                raw_records = backend.list_panes()
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                errors[label] = str(exc)
                continue
            for raw_record in raw_records:
                record = self._namespace(backend, raw_record, provider)
                records.append(record)
                pane_index[record["pane_id"]] = (backend, raw_record)
        with self._lock:
            if refresh_serial == self._refresh_serial:
                self.herdr = herdr_backends
                self.errors = errors
                self._panes = pane_index
        return records

    def create_session(
        self, name: str, cwd: str | None = None, provider: str = "tmux"
    ) -> dict[str, Any]:
        if provider == "tmux":
            backend: Any = self.tmux
        elif provider == "herdr" or provider.startswith("herdr/"):
            if not self.herdr:
                self.list_panes()
            if not self.herdr:
                raise RuntimeError("no Herdr session is configured")
            if provider == "herdr":
                if len(self.herdr) != 1:
                    raise ValueError(
                        "provider 'herdr' is ambiguous; use herdr/<session_name>"
                    )
                backend = self.herdr[0]
            else:
                session_name = provider.removeprefix("herdr/")
                backend = next(
                    (item for item in self.herdr if item.session_name == session_name), None
                )
                if backend is None:
                    raise ValueError(f"unknown Herdr provider: {provider}")
            provider = "herdr"
        else:
            raise ValueError("provider must be 'tmux', 'herdr', or 'herdr/<session_name>'")
        raw = backend.create_session(name, cwd)
        record = self._namespace(backend, raw, provider)
        with self._lock:
            self._panes = {**self._panes, record["pane_id"]: (backend, raw)}
        return record

    def _pane(self, pane_id: str) -> tuple[Any, dict[str, Any]]:
        if not isinstance(pane_id, str):
            raise TypeError("pane_id must be a string")
        with self._lock:
            found = self._panes.get(pane_id)
        if found is None:
            self.list_panes()
            with self._lock:
                found = self._panes.get(pane_id)
        if found is None:
            raise ValueError(f"unknown pane: {pane_id}")
        return found

    def focus(self, pane_id: str) -> None:
        backend, raw = self._pane(pane_id)
        backend.focus(str(raw["pane_id"]))

    def send_text(self, pane_id: str, text: str, enter: bool = False) -> None:
        backend, raw = self._pane(pane_id)
        backend.send_text(str(raw["pane_id"]), text, enter)

    def capture(self, pane_id: str, lines: int = 200) -> str:
        backend, raw = self._pane(pane_id)
        return backend.capture(str(raw["pane_id"]), lines)

    def attach_pane_argv(self, pane_id: str) -> list[str]:
        backend, raw = self._pane(pane_id)
        if hasattr(backend, "attach_pane_argv"):
            return backend.attach_pane_argv(str(raw["pane_id"]))
        backend.focus(str(raw["pane_id"]))
        return backend.attach_argv(str(raw["session_id"]))

    def attach_argv(self, session_id: str) -> list[str]:
        if not isinstance(session_id, str):
            raise TypeError("session_id must be a string")
        panes = self.list_panes()
        matching = [pane for pane in panes if pane["session_id"] == session_id]
        if not matching:
            raise ValueError(f"unknown session: {session_id}")
        backend, raw = self._pane(matching[0]["pane_id"])
        if self._provider(session_id) == "herdr" and len(matching) != 1:
            raise ValueError(
                "Herdr workspace attach is ambiguous; attach a pane with attach_pane_argv"
            )
        return backend.attach_argv(str(raw["session_id"]))


# Compatibility for the first prototype name; new callers should use CompositeBackend.
CombinedBackend = CompositeBackend


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        print(json.dumps({"error": message}), file=sys.stderr)
        raise SystemExit(2)


def _parser() -> argparse.ArgumentParser:
    parser = _JsonArgumentParser(description="JSON CLI for combined tmux and Herdr terminals")
    parser.add_argument("--socket", dest="socket_name", help="use a named tmux socket")
    parser.add_argument(
        "--herdr-session",
        dest="herdr_sessions",
        action="append",
        help="limit Herdr to a named session; repeat for more than one",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")

    create = commands.add_parser("create")
    create.add_argument("name")
    create.add_argument("--cwd")
    create.add_argument("--provider", default="tmux")

    focus = commands.add_parser("focus")
    focus.add_argument("pane_id")

    send = commands.add_parser("send")
    send.add_argument("pane_id")
    send.add_argument("text")
    send.add_argument("--enter", action="store_true")

    read = commands.add_parser("read")
    read.add_argument("pane_id")
    read.add_argument("--lines", type=int, default=200)

    attach = commands.add_parser("attach-pane-argv")
    attach.add_argument("pane_id")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        backend = CompositeBackend(args.socket_name, args.herdr_sessions)
        if args.command == "list":
            panes = backend.list_panes()
            result: Any = {"panes": panes, "errors": backend.errors}
        elif args.command == "create":
            result = backend.create_session(args.name, args.cwd, args.provider)
        elif args.command == "focus":
            backend.focus(args.pane_id)
            result = {"ok": True}
        elif args.command == "send":
            backend.send_text(args.pane_id, args.text, args.enter)
            result = {"ok": True}
        elif args.command == "read":
            result = {"pane_id": args.pane_id, "text": backend.capture(args.pane_id, args.lines)}
        else:
            result = backend.attach_pane_argv(args.pane_id)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        print(json.dumps({"error": str(exc), "type": type(exc).__name__}), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
