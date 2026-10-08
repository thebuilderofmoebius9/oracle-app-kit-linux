#!/usr/bin/env python3
"""Build a rootless Debian package from the native app's explicit file list."""
import argparse
import hashlib
from pathlib import Path
import shutil
import subprocess
import tempfile

VERSION = "0.2.0+20261008"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = Path(__file__).resolve().parent
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    target = output / f"oracle-linux_{VERSION}_all.deb"
    with tempfile.TemporaryDirectory(prefix="oracle-linux-package-") as temp:
        root = Path(temp)
        app = root / "usr/share/oracle-linux"
        app.mkdir(parents=True)
        for name in ("app.py", "tmux_backend.py", "herdr_backend.py", "combined_backend.py", "atom_monitor.py", "run"):
            shutil.copy2(source / name, app / name)
        (app / "run").chmod(0o755)
        bindir = root / "usr/bin"
        bindir.mkdir(parents=True)
        launcher = bindir / "oracle-linux"
        launcher.write_text('#!/bin/sh\nexec /usr/share/oracle-linux/run "$@"\n')
        launcher.chmod(0o755)
        for name, relative in (
            ("oracle-linux.desktop", "usr/share/applications"),
            ("oracle-linux.svg", "usr/share/icons/hicolor/scalable/apps"),
            ("README.md", "usr/share/doc/oracle-linux"),
        ):
            directory = root / relative
            directory.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / name, directory / name)
        control = root / "DEBIAN"
        control.mkdir()
        (control / "control").write_text(
            f"Package: oracle-linux\nVersion: {VERSION}\nArchitecture: all\n"
            "Maintainer: Oracle Linux contributors\nSection: x11\nPriority: optional\n"
            "Depends: python3 (>= 3.10), python3-gi, gir1.2-gtk-3.0, gir1.2-vte-2.91, tmux (>= 3.2)\n"
            "Description: Native Oracle workspace for tmux and Herdr\n"
            " GTK and VTE desktop client with session selection and a shared agent CLI.\n"
        )
        subprocess.run(["dpkg-deb", "--root-owner-group", "--build", str(root), str(target)], check=True)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_suffix(target.suffix + ".sha256").write_text(f"{digest}  {target.name}\n")
    print(target)
    print(digest)


if __name__ == "__main__":
    main()
