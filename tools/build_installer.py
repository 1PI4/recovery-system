#!/usr/bin/env python3
"""
Build a Windows installer (Setup.exe) for the recovery dashboard using Inno Setup.

    python tools/build_installer.py

Steps: make sure the .exe + icon exist -> find (or winget-install) the Inno Setup
compiler ISCC.exe -> compile installer.iss. Output:
    installer_output\recolib-Recovery-Setup.exe
"""
from __future__ import annotations
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def find_iscc():
    import glob
    local = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        r"C:\Program Files\Inno Setup 6\ISCC.exe",
        os.path.join(local, "Programs", "Inno Setup 6", "ISCC.exe"),
        os.path.join(local, "Programs", "Inno Setup 5", "ISCC.exe"),
        r"C:\Program Files (x86)\Inno Setup 5\ISCC.exe",
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    # last resort: search the common install roots
    for root in (os.environ.get("ProgramFiles(x86)", ""), os.environ.get("ProgramFiles", ""),
                 os.path.join(local, "Programs")):
        if root:
            hits = glob.glob(os.path.join(root, "**", "ISCC.exe"), recursive=True)
            if hits:
                return hits[0]
    return shutil.which("ISCC")


def main():
    exe = os.path.join(ROOT, "dist", "RecoveryDashboard.exe")
    if not os.path.exists(exe):
        print("[i] exe missing - building it first…")
        subprocess.check_call([sys.executable, os.path.join(ROOT, "tools", "build_exe.py")])

    if not os.path.exists(os.path.join(ROOT, "app.ico")):
        subprocess.call([sys.executable, os.path.join(ROOT, "tools", "make_icon.py")])

    iscc = find_iscc()
    if not iscc:
        print("[i] Inno Setup not found - trying to install it via winget…")
        try:
            subprocess.check_call(["winget", "install", "--id", "JRSoftware.InnoSetup",
                                   "-e", "--accept-package-agreements",
                                   "--accept-source-agreements"])
        except Exception as e:
            print("[x] winget install failed:", e)
        iscc = find_iscc()

    if not iscc:
        print("\n[x] Inno Setup compiler (ISCC.exe) not available.")
        print("    Install it from https://jrsoftware.org/isdl.php and re-run,")
        print("    or just ship dist\\RecoveryDashboard.exe directly (no installer needed).")
        return 1

    print(f"[i] Using {iscc}")
    subprocess.check_call([iscc, os.path.join(ROOT, "installer.iss")], cwd=ROOT)
    out = os.path.join(ROOT, "installer_output", "recolib-Recovery-Setup.exe")
    if os.path.exists(out):
        mb = os.path.getsize(out) / (1024 * 1024)
        print(f"\n[OK] Installer built: {out}  ({mb:.1f} MB)")
        return 0
    print("\n[x] ISCC ran but installer not found - check output above.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
