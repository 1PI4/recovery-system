#!/usr/bin/env python3
"""
Unit + integration test for the NTFS quick-scan engine.

It hand-builds a *real* (if tiny) NTFS volume image:
  - a boot sector,
  - an $MFT (record 0) whose $DATA data-run points at the MFT itself,
  - a deleted file with RESIDENT data (contents live inside the MFT record),
  - a deleted file with NON-RESIDENT data (contents live in a cluster the
    record's data-run points to),
  - one *in-use* file that must NOT be recovered.

Then it runs ntfs.quick_scan against the image (no admin, it's just a file) and
checks both deleted files come back with the right NAME and CONTENT. It also
unit-tests the data-run decoder.

Run:  python tools/test_ntfs.py
"""
from __future__ import annotations
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from recolib import ntfs, disk  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

SECTOR = 512
SPC = 2                      # sectors per cluster
CLUSTER = SECTOR * SPC       # 1024
RECORD = 1024
MFT_LCN = 4
MFT_CLUSTERS = 8             # 8 records
DATA_LCN = 12                # where the non-resident file's bytes live
IMAGE_CLUSTERS = 16


# --------------------------------------------------------------------------- #
#  builders
# --------------------------------------------------------------------------- #
def enc_run(length: int, lcn_delta: int) -> bytes:
    ll = length.to_bytes(max(1, (length.bit_length() + 7) // 8), "little")
    if lcn_delta == 0:
        return bytes([len(ll)]) + ll
    ob = lcn_delta.to_bytes(max(1, (lcn_delta.bit_length() + 8) // 8), "little", signed=True)
    return bytes([(len(ob) << 4) | len(ll)]) + ll + ob


def resident_attr(atype: int, content: bytes, name_length: int = 0) -> bytes:
    coff = 0x18
    total = (coff + len(content) + 7) & ~7
    a = bytearray(total)
    struct.pack_into("<I", a, 0, atype)
    struct.pack_into("<I", a, 4, total)
    a[8] = 0
    a[9] = name_length
    struct.pack_into("<H", a, 0x0A, coff)
    struct.pack_into("<I", a, 0x10, len(content))
    struct.pack_into("<H", a, 0x14, coff)
    a[coff:coff + len(content)] = content
    return bytes(a)


def nonres_data_attr(runs: bytes, real_size: int, last_vcn: int) -> bytes:
    roff = 0x40
    total = (roff + len(runs) + 7) & ~7
    a = bytearray(total)
    struct.pack_into("<I", a, 0, ntfs.ATTR_DATA)
    struct.pack_into("<I", a, 4, total)
    a[8] = 1                                   # non-resident
    struct.pack_into("<H", a, 0x0A, roff)
    struct.pack_into("<Q", a, 0x18, last_vcn)
    struct.pack_into("<H", a, 0x20, roff)
    alloc = ((real_size + CLUSTER - 1) // CLUSTER) * CLUSTER
    struct.pack_into("<Q", a, 0x28, alloc)
    struct.pack_into("<Q", a, 0x30, real_size)
    struct.pack_into("<Q", a, 0x38, real_size)
    a[roff:roff + len(runs)] = runs
    return bytes(a)


def filename_attr(name: str, parent: int = ntfs.ROOT_RECORD) -> bytes:
    nm = name.encode("utf-16-le")
    c = bytearray(0x42 + len(nm))
    struct.pack_into("<Q", c, 0, parent | (5 << 48))
    c[0x40] = len(name)
    c[0x41] = 1                                # Win32 namespace
    c[0x42:] = nm
    return resident_attr(ntfs.ATTR_FILE_NAME, bytes(c))


def build_record(flags: int, attrs, seq: int = 1) -> bytes:
    usa_off = 0x30
    usa_count = RECORD // SECTOR + 1
    first_attr = (usa_off + usa_count * 2 + 7) & ~7
    rec = bytearray(RECORD)
    rec[0:4] = b"FILE"
    struct.pack_into("<H", rec, 0x04, usa_off)
    struct.pack_into("<H", rec, 0x06, usa_count)
    struct.pack_into("<H", rec, 0x10, seq)
    struct.pack_into("<H", rec, 0x12, 1)
    struct.pack_into("<H", rec, 0x14, first_attr)
    struct.pack_into("<H", rec, 0x16, flags)
    struct.pack_into("<I", rec, 0x1C, RECORD)
    off = first_attr
    for a in attrs:
        rec[off:off + len(a)] = a
        off += len(a)
    struct.pack_into("<I", rec, off, 0xFFFFFFFF)
    struct.pack_into("<I", rec, 0x18, off + 4)
    # apply update-sequence (fixup): stamp each sector's last 2 bytes
    struct.pack_into("<H", rec, usa_off, 1)
    for i in range(1, usa_count):
        end = i * SECTOR
        rec[usa_off + i * 2: usa_off + i * 2 + 2] = rec[end - 2:end]  # original (zero)
        rec[end - 2:end] = b"\x01\x00"                               # = USN
    return bytes(rec)


def build_image(path: str):
    img = bytearray(IMAGE_CLUSTERS * CLUSTER)

    # boot sector
    img[3:11] = b"NTFS    "
    struct.pack_into("<H", img, 0x0B, SECTOR)
    img[0x0D] = SPC
    struct.pack_into("<Q", img, 0x30, MFT_LCN)
    struct.pack_into("<b", img, 0x40, 1)        # 1 cluster per record -> 1024
    img[510:512] = b"\x55\xAA"

    resident_text = b"Hello, I was a deleted text file! 1234567890"
    photo = (b"PHOTOBYTES" * 100)[:1000]

    records = [
        # 0: $MFT itself - $DATA run covers the 8-record MFT
        build_record(ntfs.FLAG_IN_USE, [
            filename_attr("$MFT"),
            nonres_data_attr(enc_run(MFT_CLUSTERS, MFT_LCN) + b"\x00",
                             MFT_CLUSTERS * RECORD, MFT_CLUSTERS - 1)]),
        # 1: DELETED, resident
        build_record(0, [
            filename_attr("resident.txt"),
            resident_attr(ntfs.ATTR_DATA, resident_text)]),
        # 2: DELETED, non-resident (bytes at DATA_LCN)
        build_record(0, [
            filename_attr("photo.dat"),
            nonres_data_attr(enc_run(1, DATA_LCN) + b"\x00", len(photo), 0)]),
        # 3: IN-USE - must be ignored
        build_record(ntfs.FLAG_IN_USE, [
            filename_attr("keep_me.txt"),
            resident_attr(ntfs.ATTR_DATA, b"do not recover me")]),
    ]
    for i, rec in enumerate(records):
        base = (MFT_LCN + i) * CLUSTER
        img[base:base + RECORD] = rec

    img[DATA_LCN * CLUSTER: DATA_LCN * CLUSTER + len(photo)] = photo

    with open(path, "wb") as f:
        f.write(img)
    return {"resident.txt": resident_text, "photo.dat": photo}


# --------------------------------------------------------------------------- #
#  tests
# --------------------------------------------------------------------------- #
def test_parse_runs():
    cases = [
        (b"\x31\x38\x73\x25\x34\x00", [(0x342573, 0x38)]),
        (b"\x21\x10\x00\x01\x11\x20\xe0\x00", [(256, 16), (224, 32)]),
        (b"\x21\x10\x00\x01\x01\x08\x00", [(256, 16), (None, 8)]),
    ]
    ok = True
    for data, expect in cases:
        got = ntfs._parse_runs(data)
        flag = "PASS" if got == expect else "FAIL"
        if got != expect:
            ok = False
        print(f"   {flag}  parse_runs {data.hex()} -> {got}")
    return ok


def main():
    print("[1] data-run decoder unit test")
    runs_ok = test_parse_runs()

    print("\n[2] full NTFS quick-scan integration test")
    img = os.path.join(HERE, "test_ntfs.img")
    outdir = os.path.join(HERE, "_ntfs_out")
    expected = build_image(img)
    print(f"   built {img} ({os.path.getsize(img)} bytes)")

    reader = disk.FileImage(img)
    summary = ntfs.quick_scan(reader, outdir, verbose=False)
    reader.close()

    recovered = {}
    for root, _, files in os.walk(outdir):
        for fn in files:
            with open(os.path.join(root, fn), "rb") as f:
                recovered[fn] = f.read()

    int_ok = True
    for name, content in expected.items():
        if recovered.get(name) == content:
            print(f"   PASS  {name:14} {len(content):5} bytes  (name + content match)")
        else:
            int_ok = False
            print(f"   FAIL  {name:14} expected {len(content)} bytes, "
                  f"got {len(recovered.get(name, b''))}")
    if "keep_me.txt" in recovered:
        int_ok = False
        print("   FAIL  in-use file 'keep_me.txt' was wrongly recovered")
    else:
        print("   PASS  in-use file correctly skipped")

    all_ok = runs_ok and int_ok
    print(f"\n[{'OK' if all_ok else 'XX'}] NTFS engine "
          f"{'verified' if all_ok else 'FAILED'} "
          f"({summary['recovered']} deleted files recovered).")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
