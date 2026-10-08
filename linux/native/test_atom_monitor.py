from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from atom_monitor import AtomNativeMonitor


HERE = Path(__file__).resolve().parent


def service_result(
    argv: list[str], **_: object
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        argv,
        0,
        "LoadState=loaded\nActiveState=active\nSubState=running\nUnitFileState=enabled\n",
        "",
    )


class AtomNativeMonitorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="atom-monitor-")
        self.database = Path(self.temporary.name) / "atom-native.sqlite"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def create_database(self) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.executescript(
                """
                CREATE TABLE work_queue (
                    id INTEGER PRIMARY KEY,
                    message_id TEXT NOT NULL,
                    author_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    attempts INTEGER NOT NULL,
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL,
                    error TEXT NOT NULL
                );
                CREATE TABLE kv (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at_ms INTEGER NOT NULL
                );
                """
            )
            connection.executemany(
                """
                INSERT INTO work_queue
                    (id, message_id, author_id, status, reason, attempts,
                     created_at_ms, updated_at_ms, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (1, "secret-message-1", "secret-author", "done", "chat", 1, 10, 20, "secret error"),
                    (2, "secret-message-2", "secret-author", "running", "code_change", 2, 30, 40, "secret error"),
                    (3, "secret-message-3", "secret-author", "pending", "runtime_debug", 0, 50, 60, "secret error"),
                ],
            )
            connection.executemany(
                "INSERT INTO kv(key, value, updated_at_ms) VALUES (?, ?, 1)",
                [
                    ("gateway:alive_age_s", "3"),
                    ("health:last_check_ms", "1234"),
                    ("discord:token", "must-not-leak"),
                ],
            )

    def test_snapshot_contains_only_bounded_operational_metadata(self) -> None:
        self.create_database()
        snapshot = AtomNativeMonitor(self.database, runner=service_result).snapshot(2)

        self.assertTrue(snapshot["available"])
        self.assertEqual(snapshot["service"]["active_state"], "active")
        database = snapshot["database"]
        self.assertEqual(database["queue"]["counts"], {"done": 1, "pending": 1, "running": 1})
        self.assertEqual([row["id"] for row in database["queue"]["recent"]], [3, 2])
        self.assertEqual(database["gateway"], {"gateway:alive_age_s": "3", "health:last_check_ms": "1234"})

        serialized = json.dumps(snapshot)
        self.assertNotIn("runtime_debug", serialized)
        self.assertNotIn("secret-message", serialized)
        self.assertNotIn("secret-author", serialized)
        self.assertNotIn("secret error", serialized)
        self.assertNotIn("must-not-leak", serialized)

    def test_missing_database_keeps_service_status(self) -> None:
        snapshot = AtomNativeMonitor(self.database, runner=service_result).snapshot()
        self.assertTrue(snapshot["available"])
        self.assertFalse(snapshot["database"]["available"])
        self.assertEqual(snapshot["database"]["detail"], "database not found")

    def test_unknown_schema_is_reported_without_exception_details(self) -> None:
        sqlite3.connect(self.database).close()
        snapshot = AtomNativeMonitor(self.database, runner=service_result).snapshot()
        self.assertFalse(snapshot["database"]["available"])
        self.assertEqual(snapshot["database"]["detail"], "database schema unavailable")

    def test_service_failure_keeps_database_snapshot(self) -> None:
        self.create_database()

        def failed(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 1, "", "permission denied")

        snapshot = AtomNativeMonitor(self.database, runner=failed).snapshot()
        self.assertTrue(snapshot["available"])
        self.assertFalse(snapshot["service"]["available"])
        self.assertNotIn("permission denied", json.dumps(snapshot))

    def test_missing_systemd_unit_is_unavailable(self) -> None:
        def not_found(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                argv,
                0,
                "LoadState=not-found\nActiveState=inactive\nSubState=dead\nUnitFileState=\n",
                "",
            )

        snapshot = AtomNativeMonitor(self.database, runner=not_found).snapshot()
        self.assertFalse(snapshot["available"])
        self.assertFalse(snapshot["service"]["available"])
        self.assertEqual(snapshot["service"]["load_state"], "not-found")

    def test_arguments_are_validated(self) -> None:
        with self.assertRaises(ValueError):
            AtomNativeMonitor(self.database, "bad service;restart")
        monitor = AtomNativeMonitor(self.database, runner=service_result)
        with self.assertRaises(ValueError):
            monitor.snapshot(0)
        with self.assertRaises(TypeError):
            monitor.snapshot(True)

    def test_cli_outputs_json(self) -> None:
        self.create_database()
        completed = subprocess.run(
            [
                sys.executable,
                str(HERE / "atom_monitor.py"),
                "--db",
                str(self.database),
                "--service",
                "missing-test.service",
                "--recent-limit",
                "1",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        snapshot = json.loads(completed.stdout)
        self.assertEqual(len(snapshot["database"]["queue"]["recent"]), 1)
        self.assertEqual(snapshot["database"]["queue"]["recent"][0]["id"], 3)


if __name__ == "__main__":
    unittest.main()
