#!/usr/bin/env python3
"""
recover.py - a from-scratch data-recovery tool (an EaseUS-style recovery engine).

Three engines:
    quick    NTFS $MFT scan  -> deleted files WITH original names/folders
    deep     file carving    -> reconstruct files by signature (after format/corruption)
    recycle  Recycle Bin      -> restore items still in $Recycle.Bin

Usage examples (run an *elevated* PowerShell for live disks):

    python recover.py list
    python recover.py recycle --out out
    python recover.py scan E: --mode quick --out out
    python recover.py scan E: --mode deep  --out out --types jpg,png,pdf,docx
    python recover.py scan disk.dd --mode all --out out          # an image file
    python recover.py types
"""
from __future__ import annotations
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from recolib import disk, carver, ntfs, recyclebin, signatures
from recolib.utils import human_size, ensure_dir, now_stamp

BANNER = r"""
  ____  _____ ____ ___  _   _ _____ ______   __
 |  _ \| ____/ ___/ _ \| | | | ____|  _ \ \ / /   recolib - data recovery
 | |_) |  _|| |  | | | | | | |  _| | |_) \ V /    quick (MFT) | deep (carve) | recycle
 |  _ <| |__| |__| |_| | \_/ | |___|  _ < | |
 |_| \_\_____\____\___/ \___/|_____|_| \_\|_|
"""


def cmd_list(args):
    print("Volumes (mounted):")
    vols = disk.list_volumes()
    if not vols:
        print("  (none / not on Windows)")
    for v in vols:
        used = ""
        if v["total"]:
            used = f"  {human_size(v['total'] - v['free'])} used / {human_size(v['total'])}"
        print(f"  {v['letter']:4} {v['type']:10}{used}")
    print("\nPhysical drives (raw - needs Administrator):")
    drives = disk.list_physical_drives()
    if not drives:
        print("  (none found, or not elevated / not on Windows)")
    for d in drives:
        print(f"  {d['path']:22} {human_size(d['size'])}  (target: {d['index']})")
    if not disk.is_admin():
        print("\n[!] Not elevated - scanning a live volume/disk needs an "
              "Administrator shell. Image files and the Recycle Bin do not.")
    return 0


def cmd_types(args):
    print("Carvable file types (deep scan):\n")
    by_cat = {}
    for s in signatures.SIGNATURES:
        by_cat.setdefault(s.category, []).append(s)
    for cat in sorted(by_cat):
        exts = ", ".join(sorted({s.ext for s in by_cat[cat]}))
        print(f"  {cat:10} {exts}")
    print("\n(Office docx/xlsx/pptx, jar, apk come out of the ZIP engine; "
          "avi/webp from RIFF; mov/m4a from MP4.)")
    return 0


def _open_or_die(target):
    try:
        reader = disk.open_target(target)
    except disk.DeviceError as e:
        print(f"[x] {e}", file=sys.stderr)
        if "Access denied" in str(e) and not disk.is_admin():
            print("    -> Re-run from an elevated (Administrator) PowerShell.",
                  file=sys.stderr)
        sys.exit(2)
    print(f"[i] Target: {target}  ->  {human_size(reader.size)} "
          f"(sector {getattr(reader, 'sector', 512)})")
    return reader


def cmd_scan(args):
    outbase = ensure_dir(os.path.abspath(args.out))
    reader = _open_or_die(args.target)

    sigs = signatures.SIGNATURES
    if args.types:
        wanted = [t.strip() for t in args.types.split(",") if t.strip()]
        sigs = signatures.by_extensions(wanted)
        if not sigs:
            print(f"[x] No known signatures for types: {args.types}", file=sys.stderr)
            return 2
        print(f"[i] Carving only: {', '.join(sorted({s.ext for s in sigs}))}")

    do_quick = args.mode in ("quick", "all")
    do_deep = args.mode in ("deep", "all")

    try:
        if do_quick:
            qdir = ensure_dir(os.path.join(outbase, "quick_named"))
            print(f"\n=== QUICK SCAN (NTFS $MFT) -> {qdir} ===")
            try:
                summary = ntfs.quick_scan(reader, qdir, verbose=not args.quiet)
                print(f"[+] Quick scan: recovered {summary['recovered']} named files "
                      f"({human_size(summary['bytes'])}) from {summary['num_records']} MFT records.")
            except ntfs.NotNTFS as e:
                print(f"[i] Quick scan skipped: {e}")
                if args.mode == "quick":
                    print("    Tip: use --mode deep to carve by signature instead.")

        if do_deep:
            ddir = ensure_dir(os.path.join(outbase, "deep_carved"))
            print(f"\n=== DEEP SCAN (file carving) -> {ddir} ===")
            est = human_size(reader.size)
            print(f"[i] Scanning {est}. This can take a while; press Ctrl+C to stop "
                  f"(already-carved files are kept).")
            stats = carver.carve(reader, ddir, sigs=sigs, strict=args.strict,
                                 verbose=not args.quiet,
                                 start=args.start, end=args.end)
            print(f"[+] Deep scan: carved {stats.files} files "
                  f"({human_size(stats.bytes_recovered)}).")
            if stats.per_type:
                summary = ", ".join(f"{k}:{v}" for k, v in
                                    sorted(stats.per_type.items(), key=lambda x: -x[1]))
                print(f"    By type -> {summary}")
            print(f"    Manifest: {os.path.join(ddir, '_manifest.csv')}")
    except KeyboardInterrupt:
        print("\n[!] Interrupted - partial results are saved.", file=sys.stderr)
    finally:
        try:
            reader.close()
        except Exception:
            pass

    print(f"\n[OK] Done. Output in: {outbase}")
    return 0


def cmd_recycle(args):
    drives = None
    if args.drives:
        drives = [d.strip().rstrip(":\\") + ":" for d in args.drives.split(",") if d.strip()]

    if args.dry_run:
        print("=== RECYCLE BIN PREVIEW (read-only - nothing is copied) ===")
        res = recyclebin.recover_all("", drives=drives, verbose=not args.quiet, dry_run=True)
        print(f"[i] {res['recovered']} recoverable items, {human_size(res['bytes'])} total.")
        print("    Re-run without --dry-run to copy them out.")
        return 0

    outdir = ensure_dir(os.path.abspath(args.out))
    rdir = ensure_dir(os.path.join(outdir, "recycle_bin"))
    print(f"=== RECYCLE BIN RECOVERY -> {rdir} ===")
    res = recyclebin.recover_all(rdir, drives=drives, verbose=not args.quiet)
    print(f"[+] Recovered {res['recovered']} items ({human_size(res['bytes'])}); "
          f"{res['skipped']} skipped (in use / no permission).")
    if res["recovered"] == 0:
        print("    (Nothing accessible. Some items need an elevated shell, "
              "and emptied bins are gone - try 'scan ... --mode deep'.)")
    return 0


def build_parser():
    p = argparse.ArgumentParser(
        prog="recover.py",
        description="recolib - recover deleted/lost files (NTFS quick scan, "
                    "signature carving, Recycle Bin).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="List volumes and physical drives").set_defaults(func=cmd_list)
    sub.add_parser("types", help="List carvable file types").set_defaults(func=cmd_types)

    s = sub.add_parser("scan", help="Scan a volume/disk/image for lost files")
    s.add_argument("target", help="Drive letter (E:), physical drive number (0), or image file")
    s.add_argument("--out", "-o", default=f"recovered_{now_stamp()}", help="Output folder")
    s.add_argument("--mode", choices=["quick", "deep", "all"], default="all",
                   help="quick=NTFS names, deep=carving, all=both (default)")
    s.add_argument("--types", help="Limit carving to these exts, e.g. jpg,png,pdf,docx")
    s.add_argument("--strict", action="store_true",
                   help="Carve only files whose end is certain (less junk)")
    s.add_argument("--start", type=int, default=0, help="Byte offset to start deep scan")
    s.add_argument("--end", type=int, default=None, help="Byte offset to stop deep scan")
    s.add_argument("--quiet", "-q", action="store_true", help="No progress bar")
    s.set_defaults(func=cmd_scan)

    r = sub.add_parser("recycle", help="Recover items from the Recycle Bin")
    r.add_argument("--out", "-o", default=f"recovered_{now_stamp()}", help="Output folder")
    r.add_argument("--drives", help="Limit to drives, e.g. C,D (default: all)")
    r.add_argument("--dry-run", action="store_true",
                   help="List recoverable items without copying them")
    r.add_argument("--quiet", "-q", action="store_true")
    r.set_defaults(func=cmd_recycle)
    return p


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    print(BANNER)
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
