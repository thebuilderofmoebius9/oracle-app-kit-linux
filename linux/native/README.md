# ARRA Oracles — native Linux desktop

GTK 3 + VTE desktop client for local tmux sessions. This is the terminal milestone
of the Linux port, not a port of every upstream feature.

## Run

Requirements: Linux 5.3+ graphical desktop, Python 3.10+, PyGObject, GTK 3, VTE 2.91,
and tmux 3.2+. The Debian package declares the corresponding system dependencies.
The app does not install packages, start a network listener, or enable a service.

```sh
./linux/native/run
```

Choose a pane in the sidebar to connect. Type directly in the terminal. Close
the app to detach; the shell remains owned by the tmux server. Reopen and connect
to the same session. Use **New session** to create a workspace. Existing Herdr
sessions are not migrated or terminated.

This executable runs on Linux. A Windows browser cannot run a Linux desktop app;
remote desktop or a separate remote/web client would be needed for that use case.

## Architecture

`app.py` draws the native UI and embeds a real terminal. `tmux_backend.py` owns
list/create/focus/send/read actions and exposes them as a JSON CLI for agents.
VTE runs the backend's attach command through a PTY, allowing tmux to handle
terminal input, rendering, resizing, and detach. No duplicate UI-only shell logic.
Session state lives in tmux and can be inspected from the CLI. The app saves only
a selection hint locally; it does not treat that hint as proof a session exists.

The default socket is the user's normal tmux server. Pass `--socket NAME` to use
a separate server. Tests use unique sockets and never terminate the default server.
Selecting a pane changes tmux's shared active pane/window, so other clients of
that session can observe the focus change.

## Persistence is not long-term memory

- Detaching or closing this app leaves tmux sessions and their processes running.
- Scrollback is bounded by tmux's history limit; it is not an unlimited transcript.
- Rebooting or terminating the tmux server loses live processes and in-memory
  scrollback. This release does not implement reboot restoration.
- Oracle semantic memory remains a separate ARRA/Muninn concern. Herdr also has
  persistent sessions; using tmux is a backend choice, not proof Herdr lacks memory.

## Package and verify

```sh
python3 linux/native/build_package.py --output /tmp/oracle-linux-dist
python3 -m unittest discover -s linux/native -p 'test_*.py' -v
xvfb-run -a python3 linux/native/check_gui.py
```

The `.deb` includes only the new native client, its launcher, icon and README.
It has no post-install scripts and does not change tmux configuration.

## Agent CLI

Use the same OS user and socket as the desktop. Commands return JSON; failures
return a nonzero status with a JSON error on stderr. Sending text with `--enter`
can execute commands in the selected shell, so inspect the returned pane IDs.

```sh
python3 linux/native/tmux_backend.py list
python3 linux/native/tmux_backend.py create workspace
python3 linux/native/tmux_backend.py read '%0' --lines 100
python3 linux/native/tmux_backend.py focus '%0'
python3 linux/native/tmux_backend.py send '%0' 'echo hello' --enter
python3 linux/native/tmux_backend.py attach-argv '$0'
```

Replace `%0` and `$0` with IDs returned by `list`. For a separate server, place
`--socket NAME` before the subcommand. No HTTP endpoint or additional privilege
is required by either the GUI or the CLI.

## Scope

This milestone does not implement fleet discovery, remote SSH connection UI,
agent task dispatch, Memory/Map tabs, or migration of Herdr sessions. The earlier
local web Hub can continue separately. Test evidence must distinguish GUI/PTY
verification from a health endpoint or a successful backend command.

Upstream protocol reference:
https://github.com/Soul-Brews-Studio/oracle-app-kit/blob/3fcd91bb4dfca86625aa2880e7661f35df6977ce/OracleKit/Sources/OracleKit/HerdrStream.swift

tmux documentation: https://github.com/tmux/tmux/wiki/Getting-Started
