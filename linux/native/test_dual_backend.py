from __future__ import annotations

import json
import subprocess
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

from combined_backend import CompositeBackend
from herdr_backend import HerdrBackend, HerdrError
from tmux_backend import TmuxBackend


WORKSPACES = {
    "id": "cli:workspace:list",
    "result": {
        "type": "workspace_list",
        "workspaces": [{"workspace_id": "w1", "label": "Oracle fleet"}],
    },
}
PANES = {
    "id": "cli:pane:list",
    "result": {
        "type": "pane_list",
        "panes": [
            {
                "workspace_id": "w1",
                "tab_id": "w1:t1",
                "pane_id": "w1:p1",
                "terminal_id": "term_abc123",
                "terminal_title_stripped": "Claude Code",
                "agent": "claude",
                "cwd": "/work/oracle",
                "focused": True,
            }
        ],
    },
}


class HerdrBackendTests(unittest.TestCase):
    def completed(self, stdout: str = "", stderr: str = "", returncode: int = 0):
        return subprocess.CompletedProcess(["herdr"], returncode, stdout, stderr)

    def test_list_maps_live_schema_and_direct_attach_does_not_take_over(self) -> None:
        responses = [
            self.completed(json.dumps(WORKSPACES)),
            self.completed(json.dumps(PANES)),
        ]
        with patch("herdr_backend.subprocess.run", side_effect=responses) as run:
            backend = HerdrBackend("default", "/opt/herdr")
            panes = backend.list_panes()
            self.assertEqual(panes[0]["session_name"], "Oracle fleet")
            self.assertEqual(panes[0]["pane_id"], "w1:p1")
            self.assertEqual(panes[0]["terminal_id"], "term_abc123")
            self.assertEqual(
                backend.attach_pane_argv("w1:p1"),
                [
                    "/opt/herdr",
                    "--session",
                    "default",
                    "terminal",
                    "attach",
                    "term_abc123",
                ],
            )
            self.assertNotIn("--takeover", backend.attach_pane_argv("w1:p1"))
            self.assertEqual(run.call_count, 2)

    def test_discovers_only_running_named_sessions(self) -> None:
        payload = {
            "sessions": [
                {"name": "default", "running": True},
                {"name": "lab", "running": False},
                {"name": "remote-1", "running": True},
            ]
        }
        with patch(
            "herdr_backend.subprocess.run",
            return_value=self.completed(json.dumps(payload)),
        ):
            self.assertEqual(HerdrBackend.discover_sessions(), ["default", "remote-1"])

    def test_capture_and_send_use_literal_argv(self) -> None:
        with patch(
            "herdr_backend.subprocess.run", return_value=self.completed("captured output")
        ) as run:
            backend = HerdrBackend()
            backend.send_text("w2:p3", "$HOME; echo literal", enter=False)
            self.assertEqual(
                run.call_args.args[0],
                ["herdr", "--session", "default", "pane", "send-text", "w2:p3", "$HOME; echo literal"],
            )
            self.assertEqual(backend.capture("w2:p3", 20), "captured output")

    def test_errors_and_invalid_ids_are_not_hidden(self) -> None:
        failure = self.completed(stderr="server not running", returncode=1)
        with patch("herdr_backend.subprocess.run", return_value=failure):
            with self.assertRaises(HerdrError):
                HerdrBackend().list_panes()
        with self.assertRaises(ValueError):
            HerdrBackend("bad:name")
        with self.assertRaises(ValueError):
            HerdrBackend().attach_pane_argv("--takeover")


class CompositeBackendTests(unittest.TestCase):
    def tmux_pane(self):
        return {
            "session_id": "$1",
            "session_name": "main",
            "window_id": "@1",
            "window_name": "shell",
            "pane_id": "%1",
            "pane_index": "0",
            "title": "tmux shell",
            "command": "bash",
            "cwd": "/work",
            "active": True,
        }

    def herdr_pane(self):
        return {
            "session_id": "w1",
            "session_name": "Fleet",
            "window_id": "w1:t1",
            "window_name": "w1:t1",
            "pane_id": "w1:p1",
            "pane_index": "0",
            "title": "Agent",
            "command": "claude",
            "cwd": "/work",
            "active": False,
            "terminal_id": "term_abc123",
        }

    def test_lists_both_providers_with_namespaced_ids(self) -> None:
        tmux = Mock(spec=TmuxBackend)
        tmux.list_panes.return_value = [self.tmux_pane()]
        herdr = Mock(session_name="default")
        herdr.list_panes.return_value = [self.herdr_pane()]
        backend = CompositeBackend(tmux_backend=tmux, herdr_backends=[herdr])
        panes = backend.list_panes()
        self.assertEqual([pane["pane_id"] for pane in panes], ["tmux:%1", "herdr:default:w1:p1"])
        self.assertEqual([pane["session_name"] for pane in panes], ["tmux · main", "herdr/default · Fleet"])
        self.assertEqual(backend.errors, {})

    def test_one_failed_provider_does_not_hide_the_other(self) -> None:
        tmux = Mock(spec=TmuxBackend)
        tmux.list_panes.side_effect = RuntimeError("tmux unavailable")
        herdr = Mock(session_name="default")
        herdr.list_panes.return_value = [self.herdr_pane()]
        backend = CompositeBackend(tmux_backend=tmux, herdr_backends=[herdr])
        panes = backend.list_panes()
        self.assertEqual(len(panes), 1)
        self.assertEqual(panes[0]["provider"], "herdr")
        self.assertEqual(backend.errors, {"tmux": "tmux unavailable"})

    def test_routes_actions_and_attach_by_pane(self) -> None:
        tmux = Mock(spec=TmuxBackend)
        tmux.list_panes.return_value = [self.tmux_pane()]
        tmux.attach_argv.return_value = ["tmux", "attach", "-t", "$1"]
        herdr = Mock(session_name="default")
        herdr.list_panes.return_value = [self.herdr_pane()]
        herdr.attach_pane_argv.return_value = ["herdr", "terminal", "attach", "term_abc123"]
        backend = CompositeBackend(tmux_backend=tmux, herdr_backends=[herdr])
        backend.list_panes()

        backend.send_text("herdr:default:w1:p1", "hello", True)
        herdr.send_text.assert_called_once_with("w1:p1", "hello", True)
        self.assertEqual(
            backend.attach_pane_argv("herdr:default:w1:p1"),
            ["herdr", "terminal", "attach", "term_abc123"],
        )
        self.assertEqual(
            backend.attach_pane_argv("tmux:%1"),
            ["tmux", "attach", "-t", "$1"],
        )
        tmux.focus.assert_called_once_with("%1")

    def test_create_routes_to_explicit_herdr_session(self) -> None:
        tmux = Mock(spec=TmuxBackend)
        first = Mock(session_name="default")
        second = Mock(session_name="remote-1")
        second.create_session.return_value = self.herdr_pane()
        backend = CompositeBackend(
            tmux_backend=tmux,
            herdr_backends=[first, second],
        )
        created = backend.create_session(
            "Review", "/work", provider="herdr/remote-1"
        )
        second.create_session.assert_called_once_with("Review", "/work")
        first.create_session.assert_not_called()
        self.assertEqual(created["pane_id"], "herdr:remote-1:w1:p1")

        with self.assertRaisesRegex(ValueError, "ambiguous"):
            backend.create_session("Ambiguous", provider="herdr")

    def test_herdr_session_attach_rejects_ambiguous_workspace(self) -> None:
        tmux = Mock(spec=TmuxBackend)
        tmux.list_panes.return_value = []
        herdr = Mock(session_name="default")
        second = dict(self.herdr_pane(), pane_id="w1:p2", terminal_id="term_def456")
        herdr.list_panes.return_value = [self.herdr_pane(), second]
        backend = CompositeBackend(tmux_backend=tmux, herdr_backends=[herdr])
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            backend.attach_argv("herdr:default:w1")

    def test_older_concurrent_refresh_cannot_replace_newer_index(self) -> None:
        tmux = Mock(spec=TmuxBackend)
        first_started = threading.Event()
        release_first = threading.Event()
        calls = 0
        calls_lock = threading.Lock()

        def listed():
            nonlocal calls
            with calls_lock:
                calls += 1
                current = calls
            if current == 1:
                first_started.set()
                release_first.wait(timeout=2)
                return [self.tmux_pane()]
            return [dict(self.tmux_pane(), pane_id="%2")]

        tmux.list_panes.side_effect = listed
        tmux.attach_argv.return_value = ["tmux", "attach", "-t", "$1"]
        backend = CompositeBackend(tmux_backend=tmux, herdr_backends=[])
        with ThreadPoolExecutor(max_workers=2) as executor:
            older = executor.submit(backend.list_panes)
            self.assertTrue(first_started.wait(timeout=2))
            newer = executor.submit(backend.list_panes)
            self.assertEqual(newer.result(timeout=2)[0]["pane_id"], "tmux:%2")
            release_first.set()
            self.assertEqual(older.result(timeout=2)[0]["pane_id"], "tmux:%1")

        self.assertEqual(backend.attach_pane_argv("tmux:%2"), ["tmux", "attach", "-t", "$1"])
        with self.assertRaisesRegex(ValueError, "unknown pane"):
            backend.attach_pane_argv("tmux:%1")


if __name__ == "__main__":
    unittest.main()
