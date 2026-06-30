"""Build a clean, self-contained package for the shared drive.

Copies only what partners need (the trio_mix package, the launcher + scripts,
config, and docs) into  ../dist/TrioMixAssistant/  and zips it. Excludes tests,
caches, certs, the session DB, and any local .venv.

    python package.py
"""
from __future__ import annotations

import os
import shutil
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(os.path.dirname(HERE), "dist")
NAME = "TrioMixAssistant"
DEST = os.path.join(OUT_DIR, NAME)

FILES = [
    "run.py", "launcher.py", "osc_demo.py", "show.conf", "requirements-run.txt",
    "setup.bat", "start.bat", "list-devices.bat", "rehearse.bat", "fetch-wheels.bat",
    "setup.command", "start.command", "list-devices.command", "rehearse.command",
    "fetch-wheels.command",
    "START-HERE.txt", "RUNBOOK.md", "HARDWARE_BRINGUP.md", "README.md",
]


def _ignore(_dir, names):
    return [n for n in names if n == "__pycache__" or n.endswith((".pyc", ".pyo"))]


def _clear_contents(d: str) -> None:
    """Empty a directory WITHOUT removing the directory itself — the top folder
    may be handle-locked (IDE/indexer) on Windows, but its contents are writable."""
    for name in os.listdir(d):
        p = os.path.join(d, name)
        try:
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p, ignore_errors=True)
            else:
                os.remove(p)
        except OSError:
            pass


def main() -> None:
    os.makedirs(DEST, exist_ok=True)
    _clear_contents(DEST)

    shutil.copytree(os.path.join(HERE, "trio_mix"),
                    os.path.join(DEST, "trio_mix"), ignore=_ignore, dirs_exist_ok=True)
    # ship the example show templates (incl. the 13-input AutoFOH map)
    tdir = os.path.join(HERE, "templates")
    if os.path.isdir(tdir):
        shutil.copytree(tdir, os.path.join(DEST, "templates"),
                        ignore=_ignore, dirs_exist_ok=True)

    copied, missing = [], []
    for f in FILES:
        src = os.path.join(HERE, f)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(DEST, f))
            copied.append(f)
        else:
            missing.append(f)

    zip_path = os.path.join(OUT_DIR, NAME + ".zip")
    if os.path.exists(zip_path):
        os.remove(zip_path)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _dirs, files in os.walk(DEST):
            for fn in files:
                fp = os.path.join(root, fn)
                z.write(fp, os.path.relpath(fp, OUT_DIR))

    nfiles = sum(len(fs) for _r, _d, fs in os.walk(DEST))
    print(f"Package folder : {DEST}")
    print(f"Zip            : {zip_path}")
    print(f"Files          : {nfiles}  ({len(copied)} top-level + the trio_mix package)")
    if missing:
        print(f"  (skipped, not found): {', '.join(missing)}")


if __name__ == "__main__":
    main()
