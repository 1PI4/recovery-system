#!/usr/bin/env python3
"""
recolib Dashboard - a point-and-click front end for the recovery engines.

Run:
    python dashboard.py
    (or double-click dashboard.bat)

Pick a target (drive / physical disk / image file), choose a mode, hit Start.
Live progress, a running file count, and a log are shown. "Restart as Admin"
re-launches elevated so you can scan live disks. Nothing is ever written to the
source disk - only to the Output folder you choose.
"""
from __future__ import annotations
import contextlib
import os
import queue
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from recolib import disk, carver, ntfs, recyclebin, signatures           # noqa: E402
from recolib.utils import human_size, human_duration, ensure_dir         # noqa: E402

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, scrolledtext
except Exception as e:                                                    # pragma: no cover
    print("Tkinter is required for the dashboard but isn't available:", e)
    sys.exit(1)

BG = "#0f172a"
PANEL = "#1e293b"
ACCENT = "#38bdf8"
OK = "#22c55e"
WARN = "#f59e0b"
ERR = "#ef4444"
FG = "#e2e8f0"
MUTED = "#94a3b8"


def app_dir():
    """Folder to anchor the default output to - the .exe's folder when frozen,
    else the script's folder."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def list_targets():
    """[(display, spec)] of volumes + physical drives that can be scanned."""
    out = []
    for v in disk.list_volumes():
        sz = human_size(v["total"]) if v["total"] else "?"
        out.append((f"{v['letter']}   [{v['type']}]   {sz}", v["letter"]))
    for d in disk.list_physical_drives():
        out.append((f"PhysicalDrive{d['index']}   [disk]   {human_size(d['size'])}",
                    str(d["index"])))
    return out


class _QueueWriter:
    """File-like object that funnels printed lines into the UI queue."""
    def __init__(self, q):
        self.q, self._buf = q, ""

    def write(self, s):
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self.q.put(("log", line.rstrip()))

    def flush(self):
        if self._buf.strip():
            self.q.put(("log", self._buf.rstrip()))
        self._buf = ""


class Dashboard:
    def __init__(self, root: "tk.Tk"):
        self.root = root
        self.q = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None
        self.busy = False
        self.start_time = None
        self.target_map = {}

        root.title("recolib - Data Recovery Dashboard")
        root.configure(bg=BG)
        root.geometry("900x680")
        root.minsize(820, 600)

        self._init_style()
        self._build_header()
        self._build_config()
        self._build_actions()
        self._build_progress()
        self._build_log()

        self.refresh_targets()
        self.on_mode_change()
        self.root.after(150, self._drain)

    # ------------------------------------------------------------------ UI --
    def _init_style(self):
        s = ttk.Style()
        try:
            s.theme_use("clam")
        except Exception:
            pass
        s.configure("TFrame", background=BG)
        s.configure("Panel.TFrame", background=PANEL)
        s.configure("TLabel", background=BG, foreground=FG, font=("Segoe UI", 10))
        s.configure("Muted.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 9))
        s.configure("Head.TLabel", background=BG, foreground=ACCENT,
                    font=("Segoe UI Semibold", 16))
        s.configure("Stat.TLabel", background=PANEL, foreground=FG,
                    font=("Consolas", 13, "bold"))
        s.configure("StatCap.TLabel", background=PANEL, foreground=MUTED,
                    font=("Segoe UI", 8))
        s.configure("TButton", font=("Segoe UI", 10), padding=6)
        s.configure("Accent.TButton", font=("Segoe UI Semibold", 10), padding=8)
        s.configure("TRadiobutton", background=BG, foreground=FG, font=("Segoe UI", 10))
        s.configure("TEntry", fieldbackground="#0b1220", foreground=FG)
        s.configure("TCombobox", fieldbackground="#0b1220", foreground=FG)
        s.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=PANEL)

    def _build_header(self):
        f = ttk.Frame(self.root)
        f.pack(fill="x", padx=16, pady=(14, 6))
        ttk.Label(f, text="🛟  recolib  -  Data Recovery", style="Head.TLabel").pack(side="left")
        self.admin_lbl = ttk.Label(f, text="", style="Muted.TLabel")
        self.admin_lbl.pack(side="right")
        admin = disk.is_admin()
        if admin:
            self.admin_lbl.config(text="● Administrator", foreground=OK)
        else:
            self.admin_lbl.config(text="● Not elevated - live-disk scans need admin",
                                  foreground=WARN)
            ttk.Button(f, text="Restart as Admin", command=self.restart_admin)\
                .pack(side="right", padx=(0, 10))
        ttk.Label(self.root,
                  text="Recover deleted files from a drive, disk or image. "
                       "Quick = NTFS names · Deep = carve by signature · Recycle = Recycle Bin.",
                  style="Muted.TLabel").pack(fill="x", padx=16)

    def _build_config(self):
        f = ttk.Frame(self.root)
        f.pack(fill="x", padx=16, pady=10)
        f.columnconfigure(1, weight=1)

        ttk.Label(f, text="Target:").grid(row=0, column=0, sticky="w", pady=(4, 0))
        self.target_cb = ttk.Combobox(f, state="readonly")
        self.target_cb.grid(row=0, column=1, sticky="ew", padx=8, pady=(4, 0))
        bf = ttk.Frame(f)
        bf.grid(row=0, column=2, sticky="e", pady=(4, 0))
        ttk.Button(bf, text="↻", width=3, command=self.refresh_targets).pack(side="left")
        ttk.Button(bf, text="📁 By folder…", command=self.browse_folder).pack(side="left", padx=4)
        ttk.Button(bf, text="🖿 Image…", command=self.browse_image).pack(side="left")
        ttk.Label(f, text="↑ Choose a whole DRIVE/partition to scan (e.g. C:) — not a single folder. "
                          "Deleted files no longer live in a folder; Quick scan restores their "
                          "original paths. Use “By folder…” to scan the drive a folder is on.",
                  style="Muted.TLabel", wraplength=840, justify="left")\
            .grid(row=1, column=1, columnspan=2, sticky="w", padx=8)

        ttk.Label(f, text="Mode:").grid(row=2, column=0, sticky="w", pady=4)
        mf = ttk.Frame(f)
        mf.grid(row=2, column=1, columnspan=2, sticky="w", padx=8)
        self.mode = tk.StringVar(value="recycle")
        for text, val in [("Recycle Bin (no admin)", "recycle"),
                          ("Quick — NTFS names", "quick"),
                          ("Deep — carve signatures", "deep"),
                          ("All (quick + deep)", "all")]:
            ttk.Radiobutton(mf, text=text, value=val, variable=self.mode,
                            command=self.on_mode_change).pack(side="left", padx=(0, 14))

        ttk.Label(f, text="File types:").grid(row=3, column=0, sticky="w", pady=4)
        self.types_entry = ttk.Entry(f)
        self.types_entry.grid(row=3, column=1, columnspan=2, sticky="ew", padx=8)
        self.types_hint = ttk.Label(f, text="(deep only — blank = all; e.g. jpg,png,pdf,docx,psd,ai)",
                                    style="Muted.TLabel")
        self.types_hint.grid(row=4, column=1, sticky="w", padx=8)

        ttk.Label(f, text="Output:").grid(row=5, column=0, sticky="w", pady=4)
        self.out_entry = ttk.Entry(f)
        self.out_entry.insert(0, os.path.join(app_dir(), "out"))
        self.out_entry.grid(row=5, column=1, sticky="ew", padx=8)
        ttk.Button(f, text="Choose…", command=self.choose_output).grid(row=5, column=2, sticky="e")
        ttk.Label(f, text="⚠ Send output to a DIFFERENT drive than the one you scan.",
                  style="Muted.TLabel").grid(row=6, column=1, sticky="w", padx=8)

    def _build_actions(self):
        f = ttk.Frame(self.root)
        f.pack(fill="x", padx=16, pady=6)
        self.start_btn = ttk.Button(f, text="▶  Start", style="Accent.TButton", command=self.start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(f, text="■  Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=8)
        ttk.Button(f, text="👁  Preview Recycle Bin", command=self.preview_recycle).pack(side="left", padx=8)
        ttk.Button(f, text="📂  Open Output", command=self.open_output).pack(side="left", padx=8)

    def _build_progress(self):
        f = ttk.Frame(self.root, style="Panel.TFrame")
        f.pack(fill="x", padx=16, pady=8)
        self.pbar = ttk.Progressbar(f, maximum=1000, mode="determinate")
        self.pbar.pack(fill="x", padx=12, pady=(12, 6))
        self.status = ttk.Label(f, text="Idle.", style="StatCap.TLabel", background=PANEL)
        self.status.pack(anchor="w", padx=12, pady=(0, 8))

        sf = ttk.Frame(f, style="Panel.TFrame")
        sf.pack(fill="x", padx=12, pady=(0, 12))
        self.files_var = tk.StringVar(value="0")
        self.bytes_var = tk.StringVar(value="0 B")
        self.elapsed_var = tk.StringVar(value="0s")
        for cap, var in [("FILES", self.files_var), ("RECOVERED", self.bytes_var),
                         ("ELAPSED", self.elapsed_var)]:
            cell = ttk.Frame(sf, style="Panel.TFrame")
            cell.pack(side="left", expand=True, fill="x")
            ttk.Label(cell, textvariable=var, style="Stat.TLabel").pack(anchor="center")
            ttk.Label(cell, text=cap, style="StatCap.TLabel").pack(anchor="center")

    def _build_log(self):
        f = ttk.Frame(self.root)
        f.pack(fill="both", expand=True, padx=16, pady=(0, 14))
        ttk.Label(f, text="Activity log", style="Muted.TLabel").pack(anchor="w")
        self.log = scrolledtext.ScrolledText(f, height=12, bg="#0b1220", fg=FG,
                                             insertbackground=FG, relief="flat",
                                             font=("Consolas", 9))
        self.log.pack(fill="both", expand=True)
        self._log("Ready. Pick a target and mode, then press Start. "
                  "Recycle Bin works without admin.")

    # -------------------------------------------------------------- actions --
    def refresh_targets(self):
        targets = list_targets()
        self.target_map = {d: s for d, s in targets}
        self.target_cb["values"] = [d for d, _ in targets]
        if targets and not self.target_cb.get():
            self.target_cb.current(0)

    def browse_image(self):
        path = filedialog.askopenfilename(
            title="Select a disk image",
            filetypes=[("Disk images", "*.dd *.img *.bin *.iso *.raw *.001"), ("All files", "*.*")])
        if path:
            self._set_target_to_spec(path, f"Image: {os.path.basename(path)}")

    def browse_folder(self):
        """Let the user pick the FOLDER they lost files from; scan that folder's drive."""
        d = filedialog.askdirectory(
            title="Pick the folder you lost files from — its DRIVE will be scanned")
        if not d:
            return
        drive = os.path.splitdrive(os.path.abspath(d))[0]      # e.g. 'C:'
        if not drive or len(drive) != 2 or not drive[0].isalpha():
            messagebox.showinfo("By folder",
                                "Couldn't tell which drive that folder is on.\n"
                                "Pick a folder on a normal drive (like C:\\...).")
            return
        if self.mode.get() == "recycle":
            self.mode.set("quick")
            self.on_mode_change()
        self._set_target_to_spec(drive, f"{drive}   (folder: {d})")
        self._log(f"Target = drive {drive}. The whole partition is scanned; deleted files keep "
                  f"their folder path — look under '{d}' in the Quick-scan output.")

    def _set_target_to_spec(self, spec, label=None):
        """Select an existing target for `spec`, or add one and select it."""
        for disp, s in self.target_map.items():
            if s == spec:
                self.target_cb.set(disp)
                return
        label = label or spec
        vals = list(self.target_cb["values"])
        if label not in vals:
            vals.append(label)
            self.target_cb["values"] = vals
        self.target_map[label] = spec
        self.target_cb.set(label)

    def choose_output(self):
        d = filedialog.askdirectory(title="Choose output folder")
        if d:
            self.out_entry.delete(0, "end")
            self.out_entry.insert(0, d)

    def on_mode_change(self):
        deep = self.mode.get() in ("deep", "all")
        self.types_entry.configure(state="normal" if deep else "disabled")
        recycle = self.mode.get() == "recycle"
        self.target_cb.configure(state="disabled" if recycle else "readonly")

    def _selected_spec(self):
        val = self.target_cb.get()
        return self.target_map.get(val, val)

    def start(self, dry=False):
        if self.busy:
            return
        mode = "recycle" if dry else self.mode.get()
        params = {"mode": mode, "dry": dry,
                  "out": self.out_entry.get().strip() or "out",
                  "target": self._selected_spec(),
                  "types": [t.strip() for t in self.types_entry.get().split(",") if t.strip()]}
        if mode != "recycle" and not params["target"]:
            messagebox.showwarning("No target", "Pick a drive, disk or image file first.")
            return
        self.files_var.set("0")
        self.bytes_var.set("0 B")
        self.elapsed_var.set("0s")
        self.pbar["value"] = 0
        self.stop_event.clear()
        self.start_time = time.time()
        self.worker = threading.Thread(target=self._worker, args=(params,), daemon=True)
        self.worker.start()

    def preview_recycle(self):
        self.start(dry=True)

    def stop(self):
        if self.busy:
            self.stop_event.set()
            self.status.config(text="Stopping…")
            self._log("[!] Stop requested - finishing current block…")

    def open_output(self):
        out = self.out_entry.get().strip()
        if out and os.path.isdir(out):
            try:
                os.startfile(out)                                    # noqa: B606 (Windows)
            except Exception as e:
                messagebox.showinfo("Open output", f"{out}\n\n{e}")
        else:
            messagebox.showinfo("Open output", "Nothing recovered yet.")

    def restart_admin(self):
        try:
            import ctypes
            if getattr(sys, "frozen", False):                 # packaged .exe
                exe, params, workdir = sys.executable, "", os.path.dirname(sys.executable)
            else:                                              # python dashboard.py
                exe = sys.executable
                params = f'"{os.path.abspath(__file__)}"'
                workdir = os.path.dirname(os.path.abspath(__file__))
            ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, workdir, 1)
            self.root.destroy()
        except Exception as e:
            messagebox.showerror("Elevation failed", str(e))

    # --------------------------------------------------------------- worker --
    def _worker(self, p):
        q = self.q
        stop = self.stop_event
        q.put(("busy", True))
        try:
            out = ensure_dir(os.path.abspath(p["out"]))

            if p["mode"] == "recycle":
                q.put(("indet", True))
                if p["dry"]:
                    q.put(("log", "Previewing Recycle Bin (read-only)…"))
                    with contextlib.redirect_stdout(_QueueWriter(q)):
                        res = recyclebin.recover_all("", verbose=True, dry_run=True)
                    q.put(("stat", res["recovered"], res["bytes"]))
                    q.put(("done", f"Preview: {res['recovered']} recoverable items "
                                   f"({human_size(res['bytes'])}). Press Start to recover."))
                else:
                    rdir = ensure_dir(os.path.join(out, "recycle_bin"))
                    q.put(("log", f"Recovering Recycle Bin -> {rdir}"))
                    with contextlib.redirect_stdout(_QueueWriter(q)):
                        res = recyclebin.recover_all(rdir, verbose=True)
                    q.put(("stat", res["recovered"], res["bytes"]))
                    q.put(("done", f"Recovered {res['recovered']} items "
                                   f"({human_size(res['bytes'])}) -> {rdir}"))
                q.put(("indet", False))
                return

            spec = p["target"]
            q.put(("log", f"Opening target {spec}…"))
            reader = disk.open_target(spec)
            q.put(("log", f"  size {human_size(reader.size)}, sector {getattr(reader,'sector',512)}"))
            try:
                if p["mode"] in ("quick", "all") and not stop.is_set():
                    qdir = ensure_dir(os.path.join(out, "quick_named"))
                    q.put(("log", f"Quick scan (NTFS $MFT) -> {qdir}"))

                    def mft_cb(recno, total, found):
                        q.put(("prog", recno / max(1, total),
                               f"Quick scan · MFT {recno:,}/{total:,} · {found} found"))
                        q.put(("stat", found, None))
                    try:
                        s = ntfs.quick_scan(reader, qdir, verbose=False,
                                            progress_cb=mft_cb, cancel=stop.is_set)
                        q.put(("log", f"  -> {s['recovered']} named files "
                                      f"({human_size(s['bytes'])})"))
                    except ntfs.NotNTFS as e:
                        q.put(("log", f"  quick scan skipped: {e}"))

                if p["mode"] in ("deep", "all") and not stop.is_set():
                    ddir = ensure_dir(os.path.join(out, "deep_carved"))
                    q.put(("log", f"Deep scan (carving) -> {ddir}"))
                    sigs = signatures.SIGNATURES
                    if p["types"]:
                        sigs = signatures.by_extensions(p["types"]) or signatures.SIGNATURES
                        q.put(("log", "  types: " + ", ".join(sorted({s.ext for s in sigs}))))

                    def carve_cb(scanned, total, files, recovered):
                        q.put(("prog", scanned / max(1, total),
                               f"Deep scan · {human_size(scanned)}/{human_size(total)} · {files} files"))
                        q.put(("stat", files, recovered))
                    st = carver.carve(reader, ddir, sigs=sigs, verbose=False,
                                      on_progress=carve_cb, cancel=stop.is_set)
                    q.put(("log", f"  -> carved {st.files} files ({human_size(st.bytes_recovered)})"))
                    if st.per_type:
                        q.put(("log", "  by type: " + ", ".join(
                            f"{k}:{v}" for k, v in sorted(st.per_type.items(), key=lambda x: -x[1]))))
            finally:
                reader.close()
            q.put(("done", ("Stopped - partial results saved." if stop.is_set()
                            else "Scan complete.") + f"  Output -> {out}"))
        except disk.DeviceError as e:
            msg = str(e)
            if "Access denied" in msg and not disk.is_admin():
                msg += '  ->  click "Restart as Admin".'
            q.put(("err", msg))
        except Exception as e:
            q.put(("err", f"{type(e).__name__}: {e}"))
        finally:
            q.put(("busy", False))

    # ---------------------------------------------------------------- drain --
    def _drain(self):
        try:
            while True:
                m = self.q.get_nowait()
                k = m[0]
                if k == "log":
                    self._log(m[1])
                elif k == "prog":
                    self.pbar.config(mode="determinate")
                    self.pbar["value"] = max(0, min(1000, int(m[1] * 1000)))
                    self.status.config(text=m[2])
                elif k == "stat":
                    self.files_var.set(f"{m[1]:,}")
                    if m[2] is not None:
                        self.bytes_var.set(human_size(m[2]))
                elif k == "indet":
                    if m[1]:
                        self.pbar.config(mode="indeterminate")
                        self.pbar.start(14)
                    else:
                        self.pbar.stop()
                        self.pbar.config(mode="determinate")
                        self.pbar["value"] = 1000
                elif k == "busy":
                    self._set_busy(m[1])
                elif k == "done":
                    self._log("[OK] " + m[1])
                    self.status.config(text=m[1])
                elif k == "err":
                    self._log("[ERROR] " + m[1])
                    self.status.config(text="Error.")
                    messagebox.showerror("Recovery error", m[1])
        except queue.Empty:
            pass
        if self.busy and self.start_time:
            self.elapsed_var.set(human_duration(time.time() - self.start_time))
        self.root.after(150, self._drain)

    def _set_busy(self, b):
        self.busy = b
        self.start_btn.config(state="disabled" if b else "normal")
        self.stop_btn.config(state="normal" if b else "disabled")
        if not b and self.pbar["mode"] == "indeterminate":
            self.pbar.stop()
            self.pbar.config(mode="determinate")

    def _log(self, text):
        ts = time.strftime("%H:%M:%S")
        self.log.insert("end", f"[{ts}] {text}\n")
        self.log.see("end")


def main():
    root = tk.Tk()
    app = Dashboard(root)
    if "--selftest" in sys.argv:
        root.after(300, root.destroy)       # build UI, then exit (headless check)
        root.mainloop()
        print("[OK] dashboard built and torn down cleanly.")
        return 0
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
