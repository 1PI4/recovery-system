r"""
Windows Recycle Bin recovery.

When you "delete" to the Recycle Bin, Windows stores each item as a pair inside
<drive>:\$Recycle.Bin\<your-SID>\ :

    $I......   metadata: original full path, original size, deletion time
    $R......   the actual file contents (same data, just renamed)

So recovery here is simply: parse the $I header to learn the real name, then
copy the matching $R payload back out. No filesystem surgery required.

$I format:
    Vista..Win8 (v1): [u64 header=1][u64 filesize][u64 FILETIME][520 bytes UTF-16 path]
    Win10+    (v2): [u64 header=2][u64 filesize][u64 FILETIME][u32 namelen][UTF-16 path]
"""
from __future__ import annotations
import os
import struct
from datetime import datetime, timedelta, timezone

from .utils import sanitize_filename, ensure_dir, human_size


def _filetime_to_dt(ft: int):
    try:
        return datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=ft / 10)
    except Exception:
        return None


def parse_info(path: str):
    """Parse a $I metadata file -> dict(original_path, size, deleted)."""
    with open(path, "rb") as f:
        data = f.read()
    if len(data) < 24:
        return None
    version = struct.unpack_from("<Q", data, 0)[0]
    size = struct.unpack_from("<Q", data, 8)[0]
    ft = struct.unpack_from("<Q", data, 16)[0]
    if version == 2 and len(data) >= 28:
        name_len = struct.unpack_from("<I", data, 24)[0]
        raw = data[28:28 + name_len * 2]
    else:  # treat anything else as v1 (fixed 520-byte path field)
        raw = data[24:24 + 520]
    orig = raw.decode("utf-16-le", "replace").split("\x00", 1)[0]
    return {"original_path": orig, "size": size, "deleted": _filetime_to_dt(ft)}


def iter_recycle_dirs(drives=None):
    """Yield every accessible <drive>:\\$Recycle.Bin\\<SID> directory."""
    if drives is None:
        drives = [f"{chr(c)}:" for c in range(ord("A"), ord("Z") + 1)]
    for d in drives:
        root = os.path.join(d + "\\", "$Recycle.Bin")
        if not os.path.isdir(root):
            continue
        try:
            sids = os.listdir(root)
        except (PermissionError, OSError):
            continue
        for sid in sids:
            sub = os.path.join(root, sid)
            if os.path.isdir(sub):
                yield sub


def recover_all(outdir: str, drives=None, verbose: bool = True, dry_run: bool = False):
    """Copy every recoverable Recycle Bin item into `outdir`. Returns summary.
    With dry_run=True, nothing is copied - it only lists what *could* be recovered."""
    if not dry_run:
        ensure_dir(outdir)
    recovered, total, skipped = 0, 0, 0
    items = []
    for sub in iter_recycle_dirs(drives):
        try:
            names = os.listdir(sub)
        except (PermissionError, OSError):
            continue
        for name in names:
            if not name.startswith("$I"):
                continue
            ipath = os.path.join(sub, name)
            rpath = os.path.join(sub, "$R" + name[2:])
            if not os.path.exists(rpath):
                continue
            try:
                meta = parse_info(ipath)
            except Exception:
                meta = None
            orig = (meta or {}).get("original_path") or ("$R" + name[2:])
            base = sanitize_filename(os.path.basename(orig.replace("\\", "/")) or name)

            if dry_run:
                try:
                    sz = (meta or {}).get("size") or os.path.getsize(rpath)
                except OSError:
                    sz = (meta or {}).get("size") or 0
                recovered += 1
                total += sz
                when = (meta or {}).get("deleted")
                items.append({"dest": None, "original": orig, "size": sz, "deleted": when})
                if verbose:
                    w = when.strftime("%Y-%m-%d %H:%M") if when else "?"
                    print(f"  - {base}  ({human_size(sz)}, deleted {w})  <- {orig}")
                continue

            dest = os.path.join(outdir, base)
            stem, ext = os.path.splitext(dest)
            n = 1
            while os.path.exists(dest):
                dest = f"{stem}_{n}{ext}"
                n += 1
            try:
                if os.path.isdir(rpath):
                    import shutil
                    shutil.copytree(rpath, dest)
                    sz = sum(os.path.getsize(os.path.join(dp, f))
                             for dp, _, fs in os.walk(dest) for f in fs)
                else:
                    with open(rpath, "rb") as src, open(dest, "wb") as dst:
                        while True:
                            b = src.read(4 * 1024 * 1024)
                            if not b:
                                break
                            dst.write(b)
                    sz = os.path.getsize(dest)
            except (PermissionError, OSError):
                skipped += 1
                continue
            recovered += 1
            total += sz
            items.append({"dest": dest, "original": orig, "size": sz,
                          "deleted": (meta or {}).get("deleted")})
            if verbose:
                when = (meta or {}).get("deleted")
                when = when.strftime("%Y-%m-%d %H:%M") if when else "?"
                print(f"  + {base}  ({human_size(sz)}, deleted {when})")
    return {"recovered": recovered, "bytes": total, "skipped": skipped, "items": items}
