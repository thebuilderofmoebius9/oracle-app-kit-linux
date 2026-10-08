#!/usr/bin/env python3
"""Read-only atom-native service and work-queue monitor.

The desktop app and agents can use the same ``snapshot`` capability.  This
module deliberately exposes only operational metadata: no prompts, message
content, output, errors, credentials, token usage, or environment values.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Sequence


DEFAULT_DATABASE = Path.home() / "atom-native/data/atom-native.sqlite"
DEFAULT_SERVICE = "atom-native.service"
_SERVICE_NAME = re.compile(r"[A-Za-z0-9_.@-]+\Z")
_SERVICE_PROPERTIES = ("LoadState", "ActiveState", "SubState", "UnitFileState")
_GATEWAY_KEYS = (
    "gateway:alive_age_s",
    "gateway:last_heartbeat_ack_age_s",
    "gateway:last_message_age_s",
    "health:last_check_ms",
)
_Run = Callable[..., subprocess.CompletedProcess[str]]


class AtomNativeMonitor:
    """Collect a bounded, non-sensitive snapshot without mutating atom-native."""

    def __init__(
        self,
        database: str | Path = DEFAULT_DATABASE,
        service: str = DEFAULT_SERVICE,
        *,
        runner: _Run = subprocess.run,
    ):
        self.database = Path(database).expanduser()
        if not isinstance(service, str) or not _SERVICE_NAME.fullmatch(service):
            raise ValueError("service must be a systemd unit name")
        self.service = service
        self._runner = runner

    def _service_snapshot(self) -> dict[str, Any]:
        argv = [
            "systemctl",
            "--user",
            "show",
            self.service,
            "--no-pager",
        ]
        for prop in _SERVICE_PROPERTIES:
            argv.extend(("-p", prop))
        try:
            completed = self._runner(
                argv,
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except FileNotFoundError:
            return {"available": False, "name": self.service, "detail": "systemctl not found"}
        except subprocess.TimeoutExpired:
            return {"available": False, "name": self.service, "detail": "systemctl timed out"}
        except OSError:
            return {"available": False, "name": self.service, "detail": "systemctl unavailable"}

        values: dict[str, str] = {}
        for line in completed.stdout.splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key in _SERVICE_PROPERTIES:
                values[key] = value
        if completed.returncode != 0:
            return {"available": False, "name": self.service, "detail": "service status unavailable"}
        load_state = values.get("LoadState", "unknown")
        return {
            "available": load_state != "not-found",
            "name": self.service,
            "load_state": load_state,
            "active_state": values.get("ActiveState", "unknown"),
            "sub_state": values.get("SubState", "unknown"),
            "unit_file_state": values.get("UnitFileState", "unknown"),
        }

    def _database_snapshot(self, recent_limit: int) -> dict[str, Any]:
        if not self.database.is_file():
            return {"available": False, "detail": "database not found"}

        uri = f"{self.database.resolve().as_uri()}?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=2)
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("PRAGMA query_only = ON")
                counts = {
                    str(row["status"]): int(row["count"])
                    for row in connection.execute(
                        "SELECT status, COUNT(*) AS count FROM work_queue GROUP BY status"
                    )
                }
                recent = [
                    {
                        "id": int(row["id"]),
                        "status": str(row["status"]),
                        "attempts": int(row["attempts"]),
                        "created_at_ms": int(row["created_at_ms"]),
                        "updated_at_ms": int(row["updated_at_ms"]),
                    }
                    for row in connection.execute(
                        """
                        SELECT id, status, attempts, created_at_ms, updated_at_ms
                        FROM work_queue ORDER BY id DESC LIMIT ?
                        """,
                        (recent_limit,),
                    )
                ]
                placeholders = ",".join("?" for _ in _GATEWAY_KEYS)
                gateway = {
                    str(row["key"]): str(row["value"])
                    for row in connection.execute(
                        f"SELECT key, value FROM kv WHERE key IN ({placeholders})",
                        _GATEWAY_KEYS,
                    )
                }
            finally:
                connection.close()
        except (OSError, sqlite3.Error, TypeError, ValueError):
            return {"available": False, "detail": "database schema unavailable"}

        return {
            "available": True,
            "queue": {"counts": counts, "recent": recent},
            "gateway": gateway,
        }

    def snapshot(self, recent_limit: int = 8) -> dict[str, Any]:
        if isinstance(recent_limit, bool) or not isinstance(recent_limit, int):
            raise TypeError("recent_limit must be an integer")
        if not 1 <= recent_limit <= 50:
            raise ValueError("recent_limit must be between 1 and 50")
        service = self._service_snapshot()
        database = self._database_snapshot(recent_limit)
        return {
            "available": bool(service.get("available") or database.get("available")),
            "captured_at_ms": int(time.time() * 1000),
            "service": service,
            "database": database,
        }


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        print(json.dumps({"error": message}), file=sys.stderr)
        raise SystemExit(2)


def _parser() -> argparse.ArgumentParser:
    parser = _JsonArgumentParser(description="Read-only JSON monitor for atom-native")
    parser.add_argument("--db", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--service", default=DEFAULT_SERVICE)
    parser.add_argument("--recent-limit", type=int, default=8)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = AtomNativeMonitor(args.db, args.service).snapshot(args.recent_limit)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        print(json.dumps({"error": str(exc), "type": type(exc).__name__}), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
