# ARRA Oracles — native Linux desktop

GTK 3 + VTE desktop client for local tmux and Herdr terminals, with a read-only
atom-native service/queue monitor. Both terminal providers appear in one sidebar.
This is the terminal milestone of the Linux port, not every upstream feature.

## Run

Requirements: Linux 5.3+ graphical desktop, Python 3.10+, PyGObject, GTK 3,
VTE 2.91 and tmux 3.2+. Install Herdr separately to use its terminals; the installed
version must support `terminal attach`. A missing provider is reported while
working providers remain available. No network listener or autostart is added.

```sh
./linux/native/run
./linux/native/run --herdr-session default --herdr-session development
./linux/native/run --socket isolated --tmux-only
```

By default the app discovers running local Herdr sessions on refresh and reads
the normal tmux socket. Use repeatable `--herdr-session` to restrict Herdr discovery.
Choose a pane to connect, type directly, resize, or press Reconnect. Closing the
app detaches the client. The session's existing server owns the shell process.
New session opens a selector for a tmux session or a workspace in a running Herdr
session; it does not start a new Herdr server. Existing sessions are not migrated.

Herdr direct attach does not force takeover of another controller. If Herdr
rejects attachment, its error appears in the terminal and the app disconnects.
Herdr reserves Ctrl+B Q to detach and Ctrl+B Ctrl+B for a literal Ctrl+B.
Selecting tmux panes changes the shared active pane/window for other tmux clients.

For persistence, keep multiplexer servers managed independently of the app's
service/cgroup. Closing the window preserves sessions; stopping a service that
also owns the server may kill that server. Reboot recovery is not implemented.
Scrollback is bounded, and Oracle semantic memory remains in ARRA/Muninn.

This executable runs on Linux; it is not a Windows executable or browser app.
This release discovers local sessions, not remote machines over SSH.

## Atom-native monitor

Press **Atom status** for a timestamped read-only snapshot of systemd service
state, queue totals and recent job IDs/status/attempts. Press Refresh to read again.
The default database is `~/atom-native/data/atom-native.sqlite`; override with
`--atom-db FILE`. Missing service/database/schema is displayed as unavailable.
The monitor reads SQLite with `mode=ro` and `query_only`; it never sends tasks,
changes the queue, restarts the bridge, or displays prompts and credentials.
Service active state does not by itself prove Discord delivery health.

## Shared capabilities for agents

The GUI uses the same adapters as the JSON CLI. `list` returns namespaced pane IDs
and provider errors. Use IDs returned by that call rather than guessing them.

```sh
python3 linux/native/combined_backend.py list
python3 linux/native/combined_backend.py read 'tmux:%0' --lines 100
python3 linux/native/combined_backend.py send 'tmux:%0' 'echo hello' --enter
python3 linux/native/combined_backend.py attach-pane-argv 'herdr:default:w1:p1'
python3 linux/native/atom_monitor.py --recent-limit 8
```

Run `combined_backend.py --help` for provider filters and creation arguments.
Sending with `--enter` executes input in the target terminal. No additional
privilege is granted: both people and agents use the same OS-user access.
The original `tmux_backend.py` CLI remains available for existing callers.

## Package and verify

```sh
python3 linux/native/build_package.py --output /tmp/oracle-linux-dist
python3 -m unittest discover -s linux/native -p 'test_*.py' -v
xvfb-run -a /usr/bin/python3 linux/native/check_gui.py
xvfb-run -a /usr/bin/python3 linux/native/check_dual_gui.py
```

The package contains only the native client, adapters, launcher, icon and README.
It has no post-install scripts and does not change multiplexer configuration.
The tests create isolated fixtures and never terminate the user's default server.
Memory/Map tabs, cross-machine discovery, task dispatch and session migration
remain outside this release. Atom-native integration is monitoring only.
