#!/usr/bin/env python3
"""
Self-test for the carving engine.

Builds a fake "disk" (test.img): a sea of zero "free space" with several real
files (PNG, JPEG, GIF, BMP, WAV, PDF, DOCX/ZIP) dropped in at random offsets -
exactly the situation after a quick-format wipes the filesystem but leaves the
data. Then it runs the carver and checks that every planted file comes back
byte-for-byte (verified by SHA-256).

Run:  python tools/selftest.py
"""
from __future__ import annotations
import base64
import hashlib
import io
import os
import struct
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from recolib import carver, disk  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def no_byte(blob: bytes, bad: int) -> bytes:
    return bytes((b if b != bad else (bad + 1) & 0xFF) for b in blob)


def make_png() -> bytes:
    # a real 1x1 PNG
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
        "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


def make_jpeg() -> bytes:
    out = bytearray()
    out += b"\xff\xd8"                       # SOI
    out += b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"  # APP0
    out += b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00"  # SOS header
    out += no_byte(os.urandom(3000), 0xFF)   # entropy with no stray FF markers
    out += b"\xff\xd9"                        # EOI
    return bytes(out)


def make_gif() -> bytes:
    out = bytearray(b"GIF89a")
    out += struct.pack("<HHBBB", 4, 4, 0xF7, 0, 0)   # logical screen descriptor
    out += no_byte(os.urandom(500), 0x3B)            # body, no premature 00 3B
    out += b"\x00\x3b"                                 # trailer
    return bytes(out)


def make_bmp() -> bytes:
    pixels = os.urandom(16)
    size = 14 + 40 + len(pixels)
    hdr = b"BM" + struct.pack("<IHHI", size, 0, 0, 54)
    dib = struct.pack("<IiiHHIIiiII", 40, 2, 2, 1, 24, 0, len(pixels),
                      2835, 2835, 0, 0)
    return hdr + dib + pixels


def make_wav() -> bytes:
    data = os.urandom(64)
    fmt = struct.pack("<HHIIHH", 1, 1, 8000, 8000, 1, 8)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + \
           b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", len(body)) + body


def make_pdf() -> bytes:
    return (b"%PDF-1.4\n%" + b"filler-comment-" * 20 + b"\n"
            b"1 0 obj<< /Type /Catalog >>endobj\ntrailer<< /Root 1 0 R >>\n"
            b"%%EOF\n")


def make_docx() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        z.writestr("word/document.xml", "<w:document>hello world</w:document>" * 8)
    return buf.getvalue()


def make_psd() -> bytes:
    # minimal raw (uncompressed) RGB 8-bit PSD that our section-walker sizes exactly
    w = h = 8
    ch, depth = 3, 8
    head = b"8BPS" + struct.pack(">H", 1) + b"\x00" * 6 + \
        struct.pack(">HIIHH", ch, h, w, depth, 3)        # channels,h,w,depth,RGB
    sections = struct.pack(">I", 0) * 3                   # color-mode, resources, layer/mask
    comp = struct.pack(">H", 0)                           # raw
    pixels = no_byte(os.urandom(w * h * ch), 0x38)        # avoid stray '8BPS'
    return head + sections + comp + pixels


def make_ai() -> bytes:
    # legacy/EPS-style Illustrator file (PostScript ending at %%EOF)
    return (b"%!PS-Adobe-3.0 EPSF-3.0\n"
            b"%%Creator: Adobe Illustrator(R) 24.0\n"
            b"%%Title: (selftest.ai)\n"
            b"%%BoundingBox: 0 0 200 200\n"
            b"%%EndComments\n"
            b"% " + b"vector-path-data-" * 24 + b"\n"
            b"%%EOF\n")


PLANTS = {
    "png": make_png, "jpg": make_jpeg, "gif": make_gif, "bmp": make_bmp,
    "wav": make_wav, "pdf": make_pdf, "docx": make_docx,
    "psd": make_psd, "ai": make_ai,
}


def build_image(path: str):
    image = bytearray()
    expected = {}
    for name, fn in PLANTS.items():
        blob = fn()
        image += b"\x00" * 2048                # "free space" gap
        image += blob
        expected[hashlib.sha256(blob).hexdigest()] = (name, len(blob))
    image += b"\x00" * 2048
    with open(path, "wb") as f:
        f.write(image)
    return expected


def main():
    img = os.path.join(HERE, "test.img")
    outdir = os.path.join(HERE, "_selftest_out")
    expected = build_image(img)
    print(f"[i] Built {img} ({os.path.getsize(img)} bytes) "
          f"with {len(expected)} planted files.")

    reader = disk.FileImage(img)
    stats = carver.carve(reader, outdir, verbose=False)
    reader.close()
    print(f"[i] Carver recovered {stats.files} files.")

    found = {}
    for root, _, files in os.walk(outdir):
        for fn in files:
            if fn == "_manifest.csv":
                continue
            p = os.path.join(root, fn)
            with open(p, "rb") as f:
                found[hashlib.sha256(f.read()).hexdigest()] = p

    ok, fail = [], []
    for h, (name, size) in expected.items():
        if h in found:
            ok.append(name)
            print(f"   PASS  {name:5} {size:6} bytes  -> {os.path.basename(found[h])}")
        else:
            fail.append(name)
            print(f"   FAIL  {name:5} {size:6} bytes  (no byte-exact match carved)")

    print(f"\n[{'OK' if not fail else 'XX'}] {len(ok)}/{len(expected)} planted "
          f"files recovered byte-for-byte.")
    return 0 if not fail else 1


if __name__ == "__main__":
    sys.exit(main())
