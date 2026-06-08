"""
File-carving engine  ==  the "deep / RAW scan".

It reads the target sector-by-sector (in big windows) and, independently of any
filesystem, reconstructs files from their signatures:

    for every window:
        find every signature header
    process matches left-to-right:
        figure out where the file ends (sizer -> footer -> bounded dump)
        copy [start, end) out to  <outdir>/<EXT>/<ext>_00001.<ext>
        skip past it so we don't re-detect headers embedded inside it

This is exactly how PhotoRec / EaseUS "RAW recovery" find files on a formatted
or corrupted volume - filenames are gone (they live in filesystem metadata), so
carved files are numbered and grouped by type.
"""
from __future__ import annotations
import os
import csv
from dataclasses import dataclass, field
from typing import List, Optional

from . import signatures as sigmod
from .disk import chunks
from .utils import Progress, ensure_dir, human_size

WINDOW = 16 * 1024 * 1024     # 16 MiB scan window
OVERLAP = 64 * 1024           # straddle guard (>= longest header)
COPY_CHUNK = 4 * 1024 * 1024


@dataclass
class CarvedFile:
    index: int
    sig: str
    ext: str
    offset: int
    size: int
    path: str


@dataclass
class CarveStats:
    bytes_scanned: int = 0
    files: int = 0
    bytes_recovered: int = 0
    per_type: dict = field(default_factory=dict)
    items: List[CarvedFile] = field(default_factory=list)


class Carver:
    def __init__(self, reader, outdir: str, sigs=None, min_size: int = 64,
                 strict: bool = False, verbose: bool = True):
        self.reader = reader
        self.outdir = ensure_dir(outdir)
        self.sigs = sigs if sigs is not None else sigmod.SIGNATURES
        self.min_size = min_size
        self.strict = strict          # skip files whose end is uncertain
        self.verbose = verbose
        self.stats = CarveStats()
        self._counter = 0
        self._manifest = open(os.path.join(self.outdir, "_manifest.csv"), "w",
                              newline="", encoding="utf-8")
        self._csv = csv.writer(self._manifest)
        self._csv.writerow(["index", "type", "ext", "offset", "size", "path"])

    # ----- header scanning ------------------------------------------------ #
    def _matches_in_window(self, base: int, data: str):
        """Yield (abs_offset_of_file_start, signature) for every header here."""
        found = []
        for sig in self.sigs:
            for header in sig.headers:
                idx = data.find(header)
                while idx != -1:
                    file_start = base + idx - sig.header_offset
                    if file_start >= 0:
                        found.append((file_start, sig))
                    idx = data.find(header, idx + 1)
        found.sort(key=lambda t: t[0])
        return found

    # ----- end-of-file resolution ----------------------------------------- #
    def _resolve_end(self, sig, start: int, next_header: Optional[int]):
        """Return (end_offset, ext) or (None, None) if we should skip."""
        max_end = min(start + sig.max_size, self.reader.size)

        if sig.sizer is not None:
            try:
                end, ext = sig.sizer(self.reader, start, sig.max_size)
            except Exception:
                end, ext = None, None
            if end and start < end <= self.reader.size:
                return end, (ext or sig.ext)
            if self.strict:
                return None, None

        if sig.footer is not None:
            window = self.reader.read_at(start, min(sig.max_size, self.reader.size - start))
            fidx = window.find(sig.footer, len(sig.headers[0]))
            if fidx != -1:
                end = start + fidx + (len(sig.footer) if sig.footer_include else 0)
                return end, sig.ext
            if self.strict:
                return None, None

        # last resort: dump up to the next signature (or the size cap)
        bound = max_end
        if next_header is not None and start < next_header < bound:
            bound = next_header
        if bound - start < self.min_size:
            return None, None
        return bound, sig.ext

    # ----- extraction ----------------------------------------------------- #
    def _extract(self, sig, start: int, end: int, ext: str):
        size = end - start
        if size < self.min_size:
            return None
        self._counter += 1
        sub = ensure_dir(os.path.join(self.outdir, ext.upper()))
        name = f"{ext}_{self._counter:05d}.{ext}"
        path = os.path.join(sub, name)
        written = 0
        with open(path, "wb") as out:
            pos = start
            while pos < end:
                buf = self.reader.read_at(pos, min(COPY_CHUNK, end - pos))
                if not buf:
                    break
                out.write(buf)
                written += len(buf)
                pos += len(buf)
        if written < self.min_size:
            try:
                os.remove(path)
            except OSError:
                pass
            self._counter -= 1
            return None
        cf = CarvedFile(self._counter, sig.name, ext, start, written, path)
        self.stats.items.append(cf)
        self.stats.files += 1
        self.stats.bytes_recovered += written
        self.stats.per_type[ext] = self.stats.per_type.get(ext, 0) + 1
        self._csv.writerow([cf.index, sig.name, ext, start, written, path])
        return cf

    # ----- main loop ------------------------------------------------------ #
    def run(self, start: int = 0, end: int = None, on_progress=None, cancel=None):
        if end is None:
            end = self.reader.size
        prog = Progress(end - start, label="Deep scan (carving)", enabled=self.verbose)
        skip_before = start
        for base, data in chunks(self.reader, WINDOW, OVERLAP, start, end):
            if cancel is not None and cancel():
                break
            matches = self._matches_in_window(base, data)
            for i, (fstart, sig) in enumerate(matches):
                if fstart < skip_before:
                    continue
                next_header = None
                for j in range(i + 1, len(matches)):
                    if matches[j][0] > fstart:
                        next_header = matches[j][0]
                        break
                fend, ext = self._resolve_end(sig, fstart, next_header)
                if not fend:
                    continue
                cf = self._extract(sig, fstart, fend, ext)
                if cf:
                    skip_before = fend
            self.stats.bytes_scanned = min(end, base + len(data)) - start
            prog.update(self.stats.bytes_scanned,
                        extra=f"{self.stats.files} files / {human_size(self.stats.bytes_recovered)}")
            if on_progress is not None:
                on_progress(self.stats.bytes_scanned, end - start,
                            self.stats.files, self.stats.bytes_recovered)
        prog.done()
        self.close()
        return self.stats

    def close(self):
        try:
            self._manifest.close()
        except Exception:
            pass


def carve(reader, outdir, sigs=None, strict=False, verbose=True,
          start=0, end=None, on_progress=None, cancel=None) -> CarveStats:
    """Convenience wrapper: carve `reader` into `outdir`, return stats."""
    return Carver(reader, outdir, sigs=sigs, strict=strict,
                  verbose=verbose).run(start=start, end=end,
                                       on_progress=on_progress, cancel=cancel)
