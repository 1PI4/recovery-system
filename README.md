# recolib — a data-recovery tool (EaseUS-style), built from scratch

A working file-recovery program for Windows, written in pure Python (standard
library only). It recovers **deleted and lost files** the same way commercial
tools such as *EaseUS Data Recovery Wizard* do — by reading the disk at the raw
sector level and rebuilding files either from the filesystem's own records or
directly from their byte signatures.

> ⚠️ **Recover your own data only.** Use this on disks you own or are
> authorised to examine. It is read-only against the source disk.

---

## Why deleted files are recoverable

When you "delete" a file, the operating system **does not erase the bytes**. It
just marks the file's record as free in the filesystem's bookkeeping. The actual
data stays on the platters/flash until something else happens to be written over
those exact sectors. That window — between *delete* and *overwrite* — is what
every recovery tool exploits.

This tool attacks the problem with **three engines**:

| Engine | Command | How it works | Gives you back |
|--------|---------|--------------|----------------|
| **Quick scan** | `--mode quick` | Parses the NTFS **`$MFT`** (Master File Table). A deleted file's record still has its name and its *data runs* (the list of clusters holding the contents); only the "in-use" flag is cleared. | Files **with original names & folders** |
| **Deep scan** | `--mode deep` | **File carving**: reads the whole disk sector-by-sector and reconstructs files purely from their **signatures** (magic header/footer bytes), ignoring the filesystem entirely. | File **contents**, renamed `jpg_00001.jpg`, grouped by type |
| **Recycle Bin** | `recycle` | Parses `$Recycle.Bin` `$I`/`$R` pairs. | Files still in the bin, with original names |

Quick scan is fast and keeps names but needs an intact filesystem. Deep scan is
slow but works even after a **format** or **corruption**, because it doesn't
trust the filesystem at all. `--mode all` runs quick then deep.

This is exactly the model EaseUS describes: a fast directory-based pass, then a
"RAW recovery" deep scan that "searches all data sectors directly to identify
and reconstruct files based on data signatures." ([EaseUS RAW recovery][1])

---

## 🖥️ Dashboard (easiest way — no commands)

Double-click **`dashboard.bat`** (or run `python dashboard.py`) for a point-and-click GUI:

- pick a **Target** (a volume, a whole physical disk, or an image file via *Image file…*),
- choose a **Mode** — *Recycle Bin* (no admin), *Quick* (NTFS names), *Deep* (carving), or *All*,
- set an **Output** folder (use a different drive than the one you scan),
- press **Start** and watch the live **progress bar**, **file count / bytes recovered**, and **log**.

Extra buttons: **👁 Preview Recycle Bin** (read-only — see what's recoverable before copying),
**■ Stop** (cancel safely; partial results are kept), **📂 Open Output**, and
**Restart as Admin** (re-launches elevated so you can scan live disks).

```
 🛟 recolib - Data Recovery                       ● Administrator
 ┌────────────────────────────────────────────────────────────┐
 │ Target:  [ C:  [fixed]  475 GB        ▼ ]  ↻Refresh 📁Image │
 │ Mode:    (•)Recycle  ( )Quick  ( )Deep  ( )All              │
 │ Output:  [ ...\recovery\out         ]  Choose…             │
 │ [ ▶ Start ] [ ■ Stop ] [ 👁 Preview ] [ 📂 Open Output ]    │
 │ ████████████████░░░░░░░░  Deep scan · 1.2/4.0 GB · 318 files│
 │   FILES 318      RECOVERED 1.1 GB      ELAPSED 2m14s        │
 │ [ activity log … ]                                         │
 └────────────────────────────────────────────────────────────┘
```

## Requirements

- **Windows** (raw disk access) — image-file scanning works on any OS.
- **Python 3.8+** (tested on 3.13). No third-party packages required.

```powershell
# optional, only adds nicer colored output:
pip install -r requirements.txt
```

---

## 📦 Install on another PC

Three ways, from most "polished" to most "hackable":

**Option A — the installer (`Setup.exe`).** Best for a real install.
1. Build it on this PC: double-click **`make-installer.bat`** (or `python tools/build_installer.py`).
   Output: **`installer_output\recolib-Recovery-Setup.exe`** (~12 MB).
2. Copy that one file to the other PC and run it → installs to Program Files, adds a
   **Start-menu** entry and an optional **desktop shortcut** (with the app icon), plus
   an *Add/Remove Programs* entry to uninstall cleanly.
3. For live-disk scans, right-click the icon → **Run as administrator** (or use the
   in-app *Restart as Admin* button).

**Option B — the portable `.exe` (no install).** Best for a USB rescue stick.
- Build with **`build.bat`** → **`dist\RecoveryDashboard.exe`** (~10 MB, self-contained).
- Copy that single file to a USB stick and run it on the other PC. No Python needed.
- Add a desktop shortcut anytime with **`Create-Desktop-Shortcut.bat`**.

**Option C — copy the folder + Python.** Best for editing the code.
1. Install **Python 3.8+** from python.org (tick *“Add python.exe to PATH”*; tkinter included).
2. Copy the whole **`recovery`** folder over.
3. Run **`dashboard.bat`** (GUI) or `python recover.py …` (CLI). No `pip install` needed.

Notes:
- **Don't run/install onto the drive you want to recover** — use a USB stick / other drive.
- **Antivirus may warn** about a raw-disk tool; that's expected for recovery software.
  You built it from source, so you can trust it.
- PyInstaller & Inno Setup build **on Windows for Windows** (they aren't cross-compilers).

## Quick start

```powershell
# 1. See what disks/volumes exist and which need admin
python recover.py list

# 2. See which file types the deep scan can carve
python recover.py types

# 3. Restore items sitting in the Recycle Bin (no admin needed)
python recover.py recycle --out out

# 4. Recover deleted files (with names) from a drive  [run elevated]
python recover.py scan E: --mode quick --out out

# 5. Deep-carve a formatted/corrupted drive for photos & docs  [run elevated]
python recover.py scan E: --mode deep --out out --types jpg,png,pdf,docx

# 6. Work from a disk IMAGE instead of a live disk (no admin)
python recover.py scan C:\images\stick.dd --mode all --out out
```

### Targets you can pass to `scan`
- a **drive letter** — `E:` (a mounted volume)
- a **physical drive number** — `0`, `1` … (the whole disk, all partitions)
- a **disk image file** — `.dd`, `.img`, `.bin`, `.iso`, raw dumps

---

## 🛟 The two rules of safe recovery

1. **Never write the recovered files back onto the same disk you are
   recovering from.** Doing so can overwrite the very data you are trying to
   save. Always pass `--out` pointing at a *different* drive (another disk, a
   USB stick, a network share). *(In this folder the default `out` is on
   `C:` — change it if `C:` is your source.)*
2. **If the data is precious, image first, then carve the image.** Stop using
   the drive immediately, make a raw copy, and run recovery against the copy:
   ```powershell
   # make a raw image of physical disk 1 (needs a tool that can read \\.\PhysicalDrive1;
   # dd-for-windows, ddrescue, or FTK Imager all work), then:
   python recover.py scan D:\image-of-disk1.dd --mode all --out E:\recovered
   ```

---

## Running with Administrator rights (live disks)

Reading a live volume (`\\.\E:`) or physical disk (`\\.\PhysicalDrive0`) is a
privileged operation. If you see **“Access denied (WinError 5)”**, you are not
elevated.

- Double-click **`run-as-admin.bat`** (it triggers a UAC prompt and opens an
  elevated PowerShell already in this folder), **or**
- Start menu → type *PowerShell* → right-click → **Run as administrator** →
  `cd` to this folder.

Image files and the Recycle Bin do **not** need elevation.

---

## How each engine works (under the hood)

### Quick scan — NTFS `$MFT` (`recolib/ntfs.py`)
1. Read the **boot sector** → bytes/sector, sectors/cluster, and where `$MFT`
   lives.
2. Read `$MFT` record 0 and follow its own **data runs** so we can read every
   record even when the MFT is fragmented.
3. For each record whose header **`in_use` flag is 0** (deleted) and that still
   has a `$DATA` attribute:
   - read the original name from the **`$FILE_NAME`** attribute (and walk parent
     records to rebuild the folder path);
   - read the contents — *resident* (tiny files stored inline) or *non-resident*
     (follow the **data runs** to the clusters) — and truncate to the real size.

### Deep scan — file carving (`recolib/carver.py`, `recolib/signatures.py`)
1. Stream the disk in 16 MiB windows (with overlap so a header straddling a
   boundary isn't missed).
2. Find every known **signature header** in the window.
3. Work out where each file **ends**, best method first:
   *exact size from the header's own length fields* (BMP, RIFF, MP4, ZIP, 7z,
   SQLite, PNG, PDF, JPEG marker-walk) → *footer magic search* (GIF…) →
   *bounded dump up to the next signature*.
4. Copy `[start, end)` out to `deep_carved/<EXT>/<ext>_00001.<ext>` and skip
   past it (so signatures *inside* a file aren't mistaken for new files).
   A `_manifest.csv` logs every carved file with its disk offset and size.

Carvable types: images (jpg, png, gif, bmp, tif, cr2, ico), **design files —
Photoshop `.psd`/`.psb` (`8BPS`, sized by walking the PSD section table) and
Illustrator `.ai`** (modern AI is a PDF, legacy AI/EPS is PostScript `%!PS-Adobe`
or a binary-header `.eps`; "Adobe Illustrator" inside is relabelled `.ai`),
documents (pdf, docx/xlsx/pptx via the ZIP engine, legacy ole, rtf), archives
(zip, rar, 7z, gz, jar, apk), media (mp4/mov/m4a, mkv/webm, wav/avi, flac, mp3,
ogg), and sqlite databases. Run `python recover.py types` for the live list.

### Recycle Bin (`recolib/recyclebin.py`)
Each deleted-to-bin item is a pair: `$I…` (metadata: original path, size,
deletion time) and `$R…` (the actual bytes). We parse the `$I` header and copy
the `$R` payload back out under the original name.

---

## Limitations (the honest part)

Recovery is never guaranteed. It fails or degrades when:
- **The data was overwritten** — new files reused those sectors. Unrecoverable.
- **Fragmentation** — for carving, a file split into non-contiguous pieces may
  come back truncated or stitched wrong (quick scan handles fragmentation via
  data runs; carving generally assumes contiguous files, like all carvers).
- **SSDs with TRIM** — TRIM physically discards freed blocks soon after delete,
  so deleted data on a TRIM-enabled SSD is often already gone.
- **Encryption** (BitLocker/VeraCrypt) — you must scan the *decrypted/unlocked*
  volume; raw ciphertext can't be carved.
- **Carved files lose their names** — names live in filesystem metadata; that's
  why deep-scan output is numbered and grouped by type.

---

## Project layout

```
recovery/
├── dashboard.py          # GUI dashboard (Tkinter) — the easy way
├── dashboard.bat         # double-click launcher for the dashboard
├── recover.py            # CLI entry point (list / types / scan / recycle)
├── requirements.txt
├── run-as-admin.bat      # opens an elevated PowerShell here (for live disks)
├── build.bat             # builds the standalone dist\RecoveryDashboard.exe
├── make-installer.bat    # builds installer_output\recolib-Recovery-Setup.exe
├── Create-Desktop-Shortcut.bat
├── installer.iss         # Inno Setup script for the installer
├── app.ico               # the app icon
├── recolib/
│   ├── disk.py           # raw device/volume/image access + enumeration (ctypes)
│   ├── signatures.py     # signature database + smart end-of-file sizers
│   ├── carver.py         # deep-scan / file-carving engine
│   ├── ntfs.py           # quick-scan / NTFS $MFT parser
│   ├── recyclebin.py     # $Recycle.Bin recovery
│   └── utils.py          # sizes, progress bar, safe filenames
└── tools/
    ├── selftest.py       # builds a fake disk, carves it, verifies byte-for-byte
    ├── test_ntfs.py      # builds a real tiny NTFS image, verifies quick scan
    ├── build_exe.py      # PyInstaller build script (-> dist\RecoveryDashboard.exe)
    ├── build_installer.py# Inno Setup build script (-> Setup.exe)
    ├── make_icon.py      # draws app.ico
    └── make_shortcut.ps1 # creates the desktop shortcut
```

## Verifying it works

Both engines ship with self-tests that synthesise data and check exact recovery
(no real disk or admin needed):

```powershell
python tools\selftest.py     # deep-scan: PNG/JPEG/GIF/BMP/WAV/PDF/DOCX -> 7/7 byte-exact
python tools\test_ntfs.py    # quick-scan: deleted resident + non-resident files recovered
```

Expected: `[OK] 7/7 planted files recovered byte-for-byte.` and
`[OK] NTFS engine verified.`

---

## Legal & ethical use

This software is for **legitimate data recovery** — getting back your own
accidentally deleted files, or files on systems you are authorised to work on
(e.g. forensic/IT support with permission). It only ever **reads** the source
disk. Don't use it to access data you have no right to.

[1]: https://kb.easeus.com/art.php?id=30011 "EaseUS — RAW Recovery"
