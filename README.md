# Oracle App Kit — Linux desktop

This fork adds a native Linux terminal workspace in [`linux/native`](linux/native/README.md).
It uses GTK 3 and VTE to attach to tmux sessions. Human UI and agent CLI share the
same backend actions. The upstream Apple apps remain in the repository.

```sh
./linux/native/run
```

The first Linux milestone is session/pane selection, real terminal input/output,
and reconnecting without ending the shell. It is not full feature parity with
the upstream Mac app. Read the native README for requirements, tests, CLI,
packaging, and persistence boundaries.

Upstream: https://github.com/Soul-Brews-Studio/oracle-app-kit
