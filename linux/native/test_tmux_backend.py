from __future__ import annotations

import json
import os
import pty
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from tmux_backend import TmuxBackend, TmuxError


HERE = Path(__file__).resolve().parent


class TmuxBackendIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.socket_name = f"oracle-test-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        cls.backend = TmuxBackend(cls.socket_name)

    @classmethod
    def tearDownClass(cls) -> None:
        subprocess.run(
            ["tmux", "-L", cls.socket_name, "kill-server"],
            check=False,
            capture_output=True,
            text=True,
        )
        socket_root = Path(os.environ.get("TMUX_TMPDIR", tempfile.gettempdir()))
        socket_path = socket_root / f"tmux-{os.getuid()}" / cls.socket_name
        socket_path.unlink(missing_ok=True)

    def tmux(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["tmux", "-L", self.socket_name, *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )

    def new_session(self, label: str = "session") -> dict[str, object]:
        return self.backend.create_session(f"{label} {uuid.uuid4().hex[:8]}")

    def wait_for_capture(self, pane_id: str, marker: str) -> str:
        deadline = time.monotonic() + 5
        output = ""
        while time.monotonic() < deadline:
            output = self.backend.capture(pane_id, 100)
            if marker in output.splitlines():
                return output
            time.sleep(0.05)
        self.fail(f"marker not captured: {marker!r}\n{output}")

    def test_list_without_server_is_empty(self) -> None:
        absent = TmuxBackend(f"oracle-absent-{os.getpid()}-{uuid.uuid4().hex[:8]}")
        self.assertEqual(absent.list_panes(), [])

    def test_list_surfaces_errors_other_than_absent_server(self) -> None:
        failure = subprocess.CompletedProcess(
            ["tmux"], 1, "", "failed to connect to server: Permission denied"
        )
        with patch("tmux_backend.subprocess.run", return_value=failure):
            with self.assertRaises(TmuxError):
                self.backend.list_panes()

    def test_create_list_literal_send_capture_and_cwd(self) -> None:
        with tempfile.TemporaryDirectory(prefix="oracle tmux Ω ") as cwd:
            pane = self.backend.create_session(f"Oracle Linux Ω \\037 {uuid.uuid4().hex[:8]}", cwd)
            self.assertRegex(str(pane["session_id"]), r"^\$[0-9]+$")
            self.assertRegex(str(pane["window_id"]), r"^@[0-9]+$")
            self.assertRegex(str(pane["pane_id"]), r"^%[0-9]+$")
            self.assertEqual(pane["cwd"], cwd)
            self.assertIs(pane["active"], True)

            listed = {item["pane_id"]: item for item in self.backend.list_panes()}
            self.assertIn(pane["pane_id"], listed)
            self.assertEqual(listed[pane["pane_id"]]["session_name"], pane["session_name"])

            marker = "literal $HOME ; [space] Ω"
            command = f"printf '%s\\n' {shlex.quote(marker)}"
            self.backend.send_text(str(pane["pane_id"]), command, enter=True)
            output = self.wait_for_capture(str(pane["pane_id"]), marker)
            self.assertIn(marker, output.splitlines())

            replaced = self.tmux("respawn-pane", "-k", "-t", str(pane["pane_id"]), "sleep 60")
            self.assertEqual(replaced.returncode, 0, replaced.stderr)
            title = r"title \037 with spaces Ω"
            titled = self.tmux("select-pane", "-t", str(pane["pane_id"]), "-T", title)
            self.assertEqual(titled.returncode, 0, titled.stderr)
            reparsed = {item["pane_id"]: item for item in self.backend.list_panes()}
            self.assertEqual(reparsed[pane["pane_id"]]["title"], title)

    def test_focus_and_attach_argv(self) -> None:
        first = self.new_session("focus first")
        second = self.new_session("focus second")
        self.backend.focus(str(first["pane_id"]))
        state = self.tmux(
            "display-message",
            "-p",
            "-t",
            str(first["session_id"]),
            "#{pane_id}:#{pane_active}",
        )
        self.assertEqual(state.returncode, 0, state.stderr)
        self.assertEqual(state.stdout.strip(), f"{first['pane_id']}:1")
        self.assertIn(second["pane_id"], {pane["pane_id"] for pane in self.backend.list_panes()})

        expected = ["tmux", "-L", self.socket_name, "attach-session", "-t", first["session_id"]]
        self.assertEqual(self.backend.attach_argv(str(first["session_id"])), expected)

    def test_session_survives_client_detach(self) -> None:
        pane = self.new_session("detach")
        before = self.tmux("display-message", "-p", "-t", str(pane["pane_id"]), "#{pane_pid}")
        self.assertEqual(before.returncode, 0, before.stderr)
        master, slave = pty.openpty()
        env = {**os.environ, "TERM": "xterm-256color"}
        client = subprocess.Popen(
            self.backend.attach_argv(str(pane["session_id"])),
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            start_new_session=True,
        )
        os.close(slave)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                clients = self.tmux("list-clients", "-F", "#{client_session}")
                if clients.returncode == 0 and pane["session_name"] in clients.stdout.splitlines():
                    break
                time.sleep(0.05)
            else:
                self.fail("tmux client did not attach")

            detached = self.tmux("detach-client", "-s", str(pane["session_id"]))
            self.assertEqual(detached.returncode, 0, detached.stderr)
            client.wait(timeout=5)
            pane_ids = {item["pane_id"] for item in self.backend.list_panes()}
            self.assertIn(pane["pane_id"], pane_ids)
            after = self.tmux("display-message", "-p", "-t", str(pane["pane_id"]), "#{pane_pid}")
            self.assertEqual(after.returncode, 0, after.stderr)
            self.assertEqual(after.stdout.strip(), before.stdout.strip())
        finally:
            if client.poll() is None:
                client.terminate()
                client.wait(timeout=5)
            os.close(master)

    def test_invalid_ids_arguments_and_missing_target(self) -> None:
        with self.assertRaises(ValueError):
            self.backend.focus("0; kill-server")
        with self.assertRaises(ValueError):
            self.backend.attach_argv("main")
        with self.assertRaises(ValueError):
            self.backend.create_session("bad:name")
        with self.assertRaises(ValueError):
            self.backend.create_session("-leading-option")
        with self.assertRaises(ValueError):
            self.backend.capture("%0", 0)
        with self.assertRaises(ValueError):
            self.backend.send_text("%0", "bad\x00text")
        with self.assertRaises(TmuxError):
            self.backend.capture("%99999999")

    def test_cli_outputs_json(self) -> None:
        created = subprocess.run(
            [
                sys.executable,
                str(HERE / "tmux_backend.py"),
                "--socket",
                self.socket_name,
                "create",
                f"cli Ω {uuid.uuid4().hex[:8]}",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(created.returncode, 0, created.stderr)
        self.assertRegex(json.loads(created.stdout)["pane_id"], r"^%[0-9]+$")

        completed = subprocess.run(
            [sys.executable, str(HERE / "tmux_backend.py"), "--socket", self.socket_name, "list"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        records = json.loads(completed.stdout)
        self.assertIsInstance(records, list)
        self.assertTrue(records)

        invalid = subprocess.run(
            [sys.executable, str(HERE / "tmux_backend.py"), "--socket", self.socket_name, "read", "bad"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(invalid.returncode, 1)
        self.assertEqual(json.loads(invalid.stderr)["type"], "ValueError")


if __name__ == "__main__":
    unittest.main()
