"""
File-signature database for the carving engine.

A *signature* identifies a file type by the magic bytes at its start (header).
To carve a file we also need to know where it ENDS. Three strategies, in order
of reliability:

  1. `sizer`  - a function that reads the header's own length fields and returns
                the exact end offset (best - used for BMP, RIFF, MP4, ZIP, 7z,
                SQLite, PDF, PNG...).
  2. `footer` - a trailing magic sequence to search for (e.g. JPEG ends FF D9).
  3. neither  - dump up to `max_size` bytes (last resort, e.g. raw MP3).

Each sizer has the signature:  sizer(reader, start, max_size) -> (end|None, ext|None)
where `reader` exposes .read_at(offset, length) and .size, and the optional
`ext` lets a sizer refine the extension (e.g. a RIFF container -> wav/avi/webp).
"""
from __future__ import annotations
import struct
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

Sizer = Callable[["object", int, int], Tuple[Optional[int], Optional[str]]]

MB = 1024 * 1024


@dataclass
class Signature:
    name: str                      # human label, e.g. "JPEG image"
    ext: str                       # default extension, e.g. "jpg"
    headers: List[bytes]           # one or more magic header sequences
    footer: Optional[bytes] = None
    footer_include: bool = True    # include footer bytes in the carved file
    max_size: int = 16 * MB        # cap when end can't be determined precisely
    header_offset: int = 0         # header sits this many bytes into the file
    sizer: Optional[Sizer] = None
    category: str = "misc"

    # filled in by the loader for fast scanning
    _first_bytes: bytes = field(default=b"", repr=False)


# --------------------------------------------------------------------------- #
#  Smart sizers
# --------------------------------------------------------------------------- #
def _u16le(b, o):  return struct.unpack_from("<H", b, o)[0]
def _u32le(b, o):  return struct.unpack_from("<I", b, o)[0]
def _u16be(b, o):  return struct.unpack_from(">H", b, o)[0]
def _u32be(b, o):  return struct.unpack_from(">I", b, o)[0]
def _u64be(b, o):  return struct.unpack_from(">Q", b, o)[0]
def _u64le(b, o):  return struct.unpack_from("<Q", b, o)[0]


def size_bmp(reader, start, max_size):
    b = reader.read_at(start, 6)
    if len(b) < 6:
        return None, None
    size = _u32le(b, 2)
    if 54 <= size <= 200 * MB:
        return start + size, None
    return None, None


def size_riff(reader, start, max_size):
    # RIFF <u32 size> <4-char form> ... ; file length = 8 + size
    b = reader.read_at(start, 12)
    if len(b) < 12:
        return None, None
    size = _u32le(b, 4)
    form = b[8:12]
    ext = {b"WAVE": "wav", b"AVI ": "avi", b"WEBP": "webp"}.get(form)
    if ext is None or not (12 <= size + 8 <= 2 * 1024 * MB):
        return None, None
    return start + 8 + size, ext


def size_mp4(reader, start, max_size):
    # ISO base-media (mp4/mov/m4a): a chain of size-prefixed boxes.
    # `start` is the box start; the first box must be 'ftyp'.
    first = reader.read_at(start, 8)
    if len(first) < 8 or first[4:8] != b"ftyp":
        return None, None
    pos = start
    limit = start + max_size
    brand = reader.read_at(start + 8, 4)
    while pos < limit:
        hdr = reader.read_at(pos, 16)
        if len(hdr) < 8:
            break
        size = _u32be(hdr, 0)
        typ = hdr[4:8]
        if size == 1:
            if len(hdr) < 16:
                break
            size = _u64be(hdr, 8)
        elif size == 0:
            pos = reader.size  # extends to end of media
            break
        if size < 8 or not all(32 <= c < 127 for c in typ):
            break
        pos += size
    ext = "m4a" if brand[:3] in (b"M4A", b"M4B") else \
          ("mov" if brand == b"qt  " else "mp4")
    if pos <= start:
        return None, None
    return min(pos, reader.size), ext


def size_zip(reader, start, max_size):
    # The archive ends at its End-Of-Central-Directory record (PK 05 06). To pick
    # THIS archive's EOCD (not a later file's, and not stray bytes), require it to
    # be self-consistent: central-directory offset + size == the EOCD position.
    window = reader.read_at(start, min(max_size, 64 * MB))
    chosen, pos = -1, 0
    while True:
        e = window.find(b"PK\x05\x06", pos)
        if e < 0 or e + 22 > len(window):
            break
        if _u32le(window, e + 16) + _u32le(window, e + 12) == e:
            chosen = e
            break
        pos = e + 4
    if chosen < 0:                          # fallback: last EOCD before a zero gap
        z = window.find(b"\x00" * 512)
        chosen = window.rfind(b"PK\x05\x06", 0, z if z != -1 else len(window))
        if chosen < 0 or chosen + 22 > len(window):
            return None, None
    comment_len = _u16le(window, chosen + 20)
    end_rel = chosen + 22 + comment_len
    body = window[:end_rel]
    ext = "zip"
    if b"word/" in body or b"[Content_Types].xml" in body and b"word/" in body:
        ext = "docx"
    if b"xl/" in body:
        ext = "xlsx"
    if b"ppt/" in body:
        ext = "pptx"
    if b"AndroidManifest.xml" in body:
        ext = "apk"
    elif b"META-INF/MANIFEST.MF" in body and ext == "zip":
        ext = "jar"
    return start + end_rel, ext


def _swallow_eol(window, end):
    if end < len(window) and window[end:end + 1] in (b"\r", b"\n"):
        end += 1
        if end < len(window) and window[end:end + 1] == b"\n":
            end += 1
    return end


# A run of >=512 zero bytes (slack/gap) or the next same-family header bounds THIS
# file, so an %%EOF search can't run on into a later file and over-carve.
_FAMILY_MARKERS = (b"%PDF-", b"%!PS-Adobe", b"\xc5\xd0\xd3\xc6")


def _find_eof_end(reader, start, max_size, footer):
    """Return (window, abs_end, rel_end) for a file that ends at `footer`,
    bounding the search so it stays inside this one file."""
    window = reader.read_at(start, min(max_size, 64 * MB))
    bounds = []
    z = window.find(b"\x00" * 512)
    if z != -1:
        bounds.append(z)
    for m in _FAMILY_MARKERS:
        p = window.find(m, 8)
        if p != -1:
            bounds.append(p)
    bound = min(bounds) if bounds else len(window)
    idx = window.rfind(footer, 0, bound)
    if idx < 0:
        return None, None, 0
    rel_end = _swallow_eol(window, idx + len(footer))
    return window, start + rel_end, rel_end


def size_pdf(reader, start, max_size):
    window, abs_end, rel_end = _find_eof_end(reader, start, max_size, b"%%EOF")
    if window is None:
        return None, None
    # Modern Illustrator (.ai) files ARE PDFs - relabel if we see the marker.
    ext = "ai" if b"Adobe Illustrator" in window[:rel_end] else None
    return abs_end, ext


def size_eps(reader, start, max_size):
    # ASCII EPS / legacy Illustrator: PostScript ending at %%EOF.
    window, abs_end, rel_end = _find_eof_end(reader, start, max_size, b"%%EOF")
    if window is None:
        return None, None
    ext = "ai" if (b"Adobe Illustrator" in window[:rel_end] or
                   b"Illustrator" in window[:4096]) else "eps"
    return abs_end, ext


def size_eps_binary(reader, start, max_size):
    # DOS binary EPS: 30-byte header with offset/length of the PS/WMF/TIFF parts.
    b = reader.read_at(start, 30)
    if len(b) < 30 or b[0:4] != b"\xc5\xd0\xd3\xc6":
        return None, None
    ps_off, ps_len = _u32le(b, 4), _u32le(b, 8)
    wmf_off, wmf_len = _u32le(b, 12), _u32le(b, 16)
    tif_off, tif_len = _u32le(b, 20), _u32le(b, 24)
    end = max(ps_off + ps_len, wmf_off + wmf_len, tif_off + tif_len)
    if end <= 30 or end > max_size:
        return None, None
    head = reader.read_at(start + ps_off, min(ps_len, 4096)) if ps_len else b""
    ext = "ai" if b"Illustrator" in head else "eps"
    return start + end, ext


def size_psd(reader, start, max_size):
    # Photoshop PSD/PSB: walk the 5 sections; image-data length is implicit, so
    # compute it from channels x rows x width (raw) or the RLE scanline table.
    def rd(off, n):
        d = reader.read_at(off, n)
        return d if len(d) >= n else None

    head = rd(start, 26)
    if not head or head[0:4] != b"8BPS":
        return None, None
    version = _u16be(head, 4)
    if version not in (1, 2):
        return None, None
    psb = version == 2
    channels = _u16be(head, 12)
    height = _u32be(head, 14)
    width = _u32be(head, 18)
    depth = _u16be(head, 22)
    if not (1 <= channels <= 56) or depth not in (1, 8, 16, 32):
        return None, None
    if not (0 < width <= 300000 and 0 < height <= 300000):
        return None, None

    pos = start + 26
    limit = min(start + max_size, reader.size)
    for length_field in (4, 4, 8 if psb else 4):       # color-mode, resources, layer/mask
        b = rd(pos, length_field)
        if not b:
            return None, None
        seclen = _u64be(b, 0) if length_field == 8 else _u32be(b, 0)
        pos += length_field + seclen
        if pos > limit:
            return None, None

    comp = rd(pos, 2)
    if not comp:
        return None, None
    comp = _u16be(comp, 0)
    img_start = pos + 2
    rows, lines = height, height * channels
    row_bytes = (width + 7) // 8 if depth == 1 else width * (depth // 8)

    if comp == 0:                                       # raw
        end = img_start + row_bytes * rows * channels
    elif comp == 1:                                     # RLE scanline table + data
        cb = 4 if psb else 2
        table = rd(img_start, cb * lines)
        if not table:
            return None, None
        data_len = sum((_u32be(table, i * 4) if psb else _u16be(table, i * 2))
                       for i in range(lines))
        end = img_start + cb * lines + data_len
    else:                                               # ZIP-compressed: let fallback size it
        return None, None
    if end <= start or end > limit:
        return None, None
    return end, ("psb" if psb else "psd")


def size_png(reader, start, max_size):
    window = reader.read_at(start, min(max_size, 64 * MB))
    idx = window.find(b"IEND\xaeB`\x82")
    if idx < 0:
        return None, None
    return start + idx + 8, None


def size_sevenzip(reader, start, max_size):
    b = reader.read_at(start, 32)
    if len(b) < 32:
        return None, None
    next_off = _u64le(b, 12)
    next_size = _u64le(b, 20)
    end = start + 32 + next_off + next_size
    if end <= start or end - start > 8 * 1024 * MB:
        return None, None
    return end, None


def size_sqlite(reader, start, max_size):
    b = reader.read_at(start, 32)
    if len(b) < 32:
        return None, None
    page_size = _u16be(b, 16)
    if page_size == 1:
        page_size = 65536
    page_count = _u32be(b, 28)
    if page_size < 512 or page_count == 0:
        return None, None
    end = start + page_size * page_count
    if end - start > 4 * 1024 * MB:
        return None, None
    return end, None


def size_jpeg(reader, start, max_size):
    # Walk JPEG markers so we don't stop at an FF D9 buried inside a thumbnail.
    window = reader.read_at(start, min(max_size, 48 * MB))
    n = len(window)
    pos = 2  # past SOI (FF D8)
    while pos + 4 <= n:
        if window[pos] != 0xFF:
            # resync to next marker
            nxt = window.find(b"\xff", pos)
            if nxt < 0:
                break
            pos = nxt
            continue
        marker = window[pos + 1]
        if marker == 0xD9:                      # EOI
            return start + pos + 2, None
        if marker in (0x01,) or 0xD0 <= marker <= 0xD7:
            pos += 2                            # standalone marker
            continue
        if marker == 0xDA:                      # start of scan -> entropy data
            scan = pos + 2
            while scan + 1 < n:
                if window[scan] == 0xFF and window[scan + 1] not in (0x00,) and not (0xD0 <= window[scan + 1] <= 0xD7):
                    break
                scan += 1
            pos = scan
            continue
        seg = _u16be(window, pos + 2)
        if seg < 2:
            break
        pos += 2 + seg
    # fall back to a plain footer search
    idx = window.rfind(b"\xff\xd9")
    return (start + idx + 2, None) if idx > 2 else (None, None)


# --------------------------------------------------------------------------- #
#  The registry
# --------------------------------------------------------------------------- #
SIGNATURES: List[Signature] = [
    # images
    Signature("JPEG image", "jpg", [b"\xff\xd8\xff"], footer=b"\xff\xd9",
              max_size=48 * MB, sizer=size_jpeg, category="image"),
    Signature("PNG image", "png", [b"\x89PNG\r\n\x1a\n"], footer=b"IEND\xaeB`\x82",
              max_size=64 * MB, sizer=size_png, category="image"),
    Signature("GIF image", "gif", [b"GIF87a", b"GIF89a"], footer=b"\x00\x3b",
              max_size=32 * MB, category="image"),
    Signature("BMP image", "bmp", [b"BM"], sizer=size_bmp, max_size=64 * MB,
              category="image"),
    Signature("TIFF image", "tif", [b"II*\x00", b"MM\x00*"], max_size=64 * MB,
              category="image"),
    Signature("Canon/RAW (CR2)", "cr2", [b"II*\x00\x10\x00\x00\x00CR"],
              max_size=64 * MB, category="image"),
    Signature("Icon", "ico", [b"\x00\x00\x01\x00"], max_size=2 * MB,
              category="image"),
    # Adobe design files
    Signature("Photoshop (PSD/PSB)", "psd", [b"8BPS"], sizer=size_psd,
              max_size=2048 * MB, category="design"),
    Signature("Illustrator / EPS (PostScript)", "ai", [b"%!PS-Adobe"],
              footer=b"%%EOF", sizer=size_eps, max_size=256 * MB, category="design"),
    Signature("EPS (binary preview header)", "eps", [b"\xc5\xd0\xd3\xc6"],
              sizer=size_eps_binary, max_size=256 * MB, category="design"),
    # documents / archives (zip family must come before generic checks)
    Signature("PDF / Illustrator (.ai)", "pdf", [b"%PDF-"], footer=b"%%EOF",
              max_size=128 * MB, sizer=size_pdf, category="document"),
    Signature("ZIP / Office (docx,xlsx,pptx)", "zip", [b"PK\x03\x04"],
              sizer=size_zip, max_size=512 * MB, category="archive"),
    Signature("RAR archive", "rar", [b"Rar!\x1a\x07\x01\x00", b"Rar!\x1a\x07\x00"],
              max_size=1024 * MB, category="archive"),
    Signature("7-Zip archive", "7z", [b"7z\xbc\xaf\x27\x1c"], sizer=size_sevenzip,
              max_size=1024 * MB, category="archive"),
    Signature("GZIP", "gz", [b"\x1f\x8b\x08"], max_size=512 * MB,
              category="archive"),
    Signature("OLE (legacy doc/xls/ppt)", "ole", [b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"],
              max_size=64 * MB, category="document"),
    Signature("RTF document", "rtf", [b"{\\rtf"], max_size=32 * MB,
              category="document"),
    Signature("SQLite database", "sqlite", [b"SQLite format 3\x00"],
              sizer=size_sqlite, max_size=2048 * MB, category="database"),
    # audio / video
    Signature("RIFF (wav/avi/webp)", "wav", [b"RIFF"], sizer=size_riff,
              max_size=2048 * MB, category="media"),
    Signature("MP4 / MOV / M4A", "mp4", [b"ftyp"], header_offset=4, sizer=size_mp4,
              max_size=2048 * MB, category="media"),
    Signature("Matroska / WebM", "mkv", [b"\x1a\x45\xdf\xa3"], max_size=2048 * MB,
              category="media"),
    Signature("FLAC audio", "flac", [b"fLaC"], max_size=256 * MB,
              category="media"),
    Signature("MP3 audio (ID3)", "mp3", [b"ID3"], max_size=32 * MB,
              category="media"),
    Signature("OGG", "ogg", [b"OggS"], max_size=256 * MB, category="media"),
]


def prepare(signatures: List[Signature]) -> List[Signature]:
    for s in signatures:
        s._first_bytes = s.headers[0]
    return signatures


prepare(SIGNATURES)


def by_extensions(exts) -> List[Signature]:
    """Filter the registry to a set of extensions (case-insensitive)."""
    want = {e.lower().lstrip(".") for e in exts}
    out = []
    for s in SIGNATURES:
        if s.ext.lower() in want:
            out.append(s)
        elif s.ext == "zip" and want & {"docx", "xlsx", "pptx", "jar", "apk"}:
            out.append(s)
        elif s.ext == "wav" and want & {"avi", "webp"}:
            out.append(s)
        elif s.ext == "mp4" and want & {"mov", "m4a"}:
            out.append(s)
        elif s.ext == "psd" and want & {"psb"}:
            out.append(s)
        elif s.ext == "pdf" and want & {"ai"}:        # modern .ai is a PDF
            out.append(s)
        elif s.ext == "ai" and want & {"eps"}:        # the PostScript engine also carves .eps
            out.append(s)
        elif s.ext == "eps" and want & {"ai"}:        # binary EPS can be an .ai too
            out.append(s)
    return out


def all_extensions() -> List[str]:
    return sorted({s.ext for s in SIGNATURES})
