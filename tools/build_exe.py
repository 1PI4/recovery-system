#!/usr/bin/env python3
"""
Build a standalone Windows executable of the recovery dashboard with PyInstaller.

    python tools/build_exe.py

Produces  dist/RecoveryDashboard.exe  - a single file that runs on any Windows
PC with NO Python installed. Copy it to a USB stick and run it on the target
machine. (Build on Windows; PyInstaller is not a cross-compiler.)
"""
from __future__ import annotations
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("[i] PyInstaller not found - installing it (needs internet)…")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])

    icon = os.path.join(ROOT, "app.ico")
    if not os.path.exists(icon):
        try:
            subprocess.check_call([sys.executable, os.path.join(ROOT, "tools", "make_icon.py")])
        except Exception as e:
            print("[i] icon generation skipped:", e)
    icon_args = ["--icon", icon] if os.path.exists(icon) else []

    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onefile",                       # one self-contained .exe
        "--windowed",                      # GUI app, no console window
        "--name", "RecoveryDashboard",
        *icon_args,                        # embed the custom icon
        "--collect-submodules", "recolib",  # bundle the whole engine package
        os.path.join(ROOT, "dashboard.py"),
    ]
    env = dict(os.environ, PYTHONPATH=ROOT)
    print("[i] Building… (first run downloads/initialises PyInstaller)")
    print("    " + " ".join(args))
    subprocess.check_call(args, cwd=ROOT, env=env)

    exe = os.path.join(ROOT, "dist", "RecoveryDashboard.exe")
    if os.path.exists(exe):
        mb = os.path.getsize(exe) / (1024 * 1024)
        print(f"\n[OK] Built: {exe}  ({mb:.1f} MB)")
        print("     Copy that single file to the other PC and run it.")
    else:
        print("\n[x] Build finished but exe not found - check the PyInstaller output above.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
