"""
NTFS $MFT parser  ==  the "quick scan".

NTFS keeps one record per file in the Master File Table ($MFT). Deleting a file
only clears bit 0 ("in use") of the record's flags - the record, the file name,
and the *data runs* (the list of clusters holding the contents) usually survive
until the record/clusters are reused. So we can:

    1. read the boot sector            -> cluster size, $MFT location
    2. follow $MFT's own data runs     -> read every record (handles a
                                          fragmented $MFT)
    3. for each record with flag.in_use == 0 and a real $DATA attribute:
         - read the original name from $FILE_NAME
         - read the contents (resident bytes, or by reading the clusters the
           data runs point at) and truncate to the real size

References: NTFS file-record / attribute layout (flatcap.github.io linux-ntfs),
SleuthKit NTFS recovery notes.
"""
from __future__ import annotations
import os
import struct
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .utils import Progress, sanitize_filename, ensure_dir

ATTR_STANDARD_INFO = 0x10
ATTR_FILE_NAME = 0x30
ATTR_DATA = 0x80
ATTR_END = 0xFFFFFFFF

FLAG_IN_USE = 0x01
FLAG_DIRECTORY = 0x02
ROOT_RECORD = 5


class NotNTFS(Exception):
    pass


@dataclass
class MFTEntry:
    recno: int
    in_use: bool
    is_dir: bool
    name: str
    parent: Optional[int]
    size: int
    resident_data: Optional[bytes]            # set if the file is tiny/resident
    runs: List[Tuple[Optional[int], int]]     # [(lcn|None, length_clusters)]
    has_data: bool


def _parse_runs(data: bytes) -> List[Tuple[Optional[int], int]]:
    """Decode an NTFS data-run list into [(lcn|None, length_in_clusters)]."""
    runs: List[Tuple[Optional[int], int]] = []
    i = 0
    lcn = 0
    n = len(data)
    while i < n:
        header = data[i]
        i += 1
        if header == 0:
            break
        len_len = header & 0x0F
        off_len = (header >> 4) & 0x0F
        if len_len == 0 or i + len_len + off_len > n:
            break
        run_len = int.from_bytes(data[i:i + len_len], "little")
        i += len_len
        if off_len == 0:
            runs.append((None, run_len))           # sparse run
        else:
            off = int.from_bytes(data[i:i + off_len], "little", signed=True)
            i += off_len
            lcn += off
            runs.append((lcn, run_len))
    return runs


class NTFSVolume:
    def __init__(self, reader, verbose: bool = True):
        self.reader = reader
        self.verbose = verbose
        self._cache: Dict[int, Optional[MFTEntry]] = {}
        self._read_boot()
        self._bootstrap_mft()

    # -- boot sector ------------------------------------------------------- #
    def _read_boot(self):
        boot = self.reader.read_at(0, 512)
        if len(boot) < 512 or boot[3:11] != b"NTFS    ":
            raise NotNTFS("Not an NTFS volume (boot signature 'NTFS' missing).")
        self.bytes_per_sector = struct.unpack_from("<H", boot, 0x0B)[0] or 512
        self.sectors_per_cluster = boot[0x0D] or 1
        self.cluster_size = self.bytes_per_sector * self.sectors_per_cluster
        self.mft_lcn = struct.unpack_from("<Q", boot, 0x30)[0]
        raw = struct.unpack_from("<b", boot, 0x40)[0]
        self.record_size = (1 << -raw) if raw < 0 else raw * self.cluster_size
        if self.record_size <= 0 or self.record_size > 1 << 20:
            self.record_size = 1024

    # -- locate the whole $MFT (it can be fragmented) ---------------------- #
    def _bootstrap_mft(self):
        base = self.mft_lcn * self.cluster_size
        raw = self.reader.read_at(base, self.record_size)
        rec = self._apply_fixup(bytearray(raw))
        entry = self._parse_record(rec, 0)
        if entry is None or not entry.runs:
            # fall back: assume a contiguous $MFT starting at mft_lcn
            self.mft_runs = [(self.mft_lcn, 1 << 30)]
            self.mft_data_size = self.reader.size - base
        else:
            self.mft_runs = entry.runs
            self.mft_data_size = entry.size or (sum(l for _, l in entry.runs) * self.cluster_size)
        # build a VCN(byte) -> volume map for the $MFT
        self._mft_map = []
        vstart = 0
        for lcn, length in self.mft_runs:
            vlen = length * self.cluster_size
            self._mft_map.append((vstart, vlen, lcn))
            vstart += vlen
        self.num_records = max(1, self.mft_data_size // self.record_size)

    # -- low level reads --------------------------------------------------- #
    def _read_mft_bytes(self, offset: int, length: int) -> bytes:
        out = bytearray()
        for vstart, vlen, lcn in self._mft_map:
            if offset >= vstart + vlen:
                continue
            if offset + length <= vstart:
                break
            seg_start = max(offset, vstart)
            seg_end = min(offset + length, vstart + vlen)
            within = seg_start - vstart
            if lcn is None:
                out += b"\x00" * (seg_end - seg_start)
            else:
                out += self.reader.read_at(lcn * self.cluster_size + within,
                                           seg_end - seg_start)
        return bytes(out)

    def _read_runlist(self, runs, real_size) -> bytes:
        out = bytearray()
        for lcn, length in runs:
            nbytes = length * self.cluster_size
            if lcn is None:
                out += b"\x00" * nbytes
            else:
                out += self.reader.read_at(lcn * self.cluster_size, nbytes)
            if len(out) >= real_size:
                break
        return bytes(out[:real_size]) if real_size else bytes(out)

    def _apply_fixup(self, rec: bytearray) -> bytearray:
        if rec[0:4] != b"FILE":
            return rec
        usa_off = struct.unpack_from("<H", rec, 0x04)[0]
        usa_cnt = struct.unpack_from("<H", rec, 0x06)[0]
        if usa_cnt == 0:
            return rec
        usn = rec[usa_off:usa_off + 2]
        ss = self.bytes_per_sector
        for i in range(1, usa_cnt):
            sector_end = i * ss
            if sector_end > len(rec):
                break
            orig = rec[usa_off + i * 2: usa_off + i * 2 + 2]
            if len(orig) < 2:
                break
            rec[sector_end - 2: sector_end] = orig
        return rec

    # -- record parsing ---------------------------------------------------- #
    def _parse_record(self, rec: bytes, recno: int) -> Optional[MFTEntry]:
        if len(rec) < 0x30 or rec[0:4] != b"FILE":
            return None
        flags = struct.unpack_from("<H", rec, 0x16)[0]
        in_use = bool(flags & FLAG_IN_USE)
        is_dir = bool(flags & FLAG_DIRECTORY)
        first_attr = struct.unpack_from("<H", rec, 0x14)[0]

        best_name = ""
        best_ns = 99
        parent = None
        size = 0
        resident_data = None
        runs: List[Tuple[Optional[int], int]] = []
        has_data = False

        off = first_attr
        guard = 0
        while off + 8 <= len(rec) and guard < 64:
            guard += 1
            atype = struct.unpack_from("<I", rec, off)[0]
            if atype == ATTR_END:
                break
            alen = struct.unpack_from("<I", rec, off + 4)[0]
            if alen == 0 or off + alen > len(rec):
                break
            non_resident = rec[off + 8]
            name_len = rec[off + 9]

            if atype == ATTR_FILE_NAME and not non_resident:
                clen = struct.unpack_from("<I", rec, off + 0x10)[0]
                coff = struct.unpack_from("<H", rec, off + 0x14)[0]
                c = rec[off + coff: off + coff + clen]
                if len(c) >= 0x42:
                    parent_ref = struct.unpack_from("<Q", c, 0)[0] & ((1 << 48) - 1)
                    fn_len = c[0x40]
                    ns = c[0x41]
                    try:
                        nm = c[0x42:0x42 + fn_len * 2].decode("utf-16-le", "replace")
                    except Exception:
                        nm = ""
                    # prefer Win32 (1) / Win32+DOS (3) over POSIX (0) over DOS (2)
                    rank = {1: 0, 3: 1, 0: 2, 2: 3}.get(ns, 4)
                    if rank < best_ns:
                        best_ns = rank
                        best_name = nm
                        parent = parent_ref

            elif atype == ATTR_DATA and name_len == 0:
                has_data = True
                if not non_resident:
                    clen = struct.unpack_from("<I", rec, off + 0x10)[0]
                    coff = struct.unpack_from("<H", rec, off + 0x14)[0]
                    resident_data = bytes(rec[off + coff: off + coff + clen])
                    size = len(resident_data)
                else:
                    real_size = struct.unpack_from("<Q", rec, off + 0x30)[0]
                    run_off = struct.unpack_from("<H", rec, off + 0x20)[0]
                    runs = _parse_runs(rec[off + run_off: off + alen])
                    size = real_size

            off += alen

        return MFTEntry(recno, in_use, is_dir, best_name, parent, size,
                        resident_data, runs, has_data)

    def read_record(self, recno: int) -> Optional[MFTEntry]:
        if recno in self._cache:
            return self._cache[recno]
        try:
            raw = self._read_mft_bytes(recno * self.record_size, self.record_size)
            entry = self._parse_record(self._apply_fixup(bytearray(raw)), recno)
        except Exception:
            entry = None
        if len(self._cache) < 200000:
            self._cache[recno] = entry
        return entry

    # -- public API -------------------------------------------------------- #
    def resolve_path(self, entry: MFTEntry, max_depth: int = 64) -> str:
        parts = [entry.name or f"record_{entry.recno}"]
        parent = entry.parent
        depth = 0
        seen = set()
        while parent is not None and parent != ROOT_RECORD and depth < max_depth:
            if parent in seen:
                break
            seen.add(parent)
            prec = self.read_record(parent)
            if not prec or not prec.name:
                parts.append("?")
                break
            parts.append(prec.name)
            parent = prec.parent
            depth += 1
        return "/".join(reversed(parts))

    def read_data(self, entry: MFTEntry) -> bytes:
        if entry.resident_data is not None:
            return entry.resident_data[:entry.size] if entry.size else entry.resident_data
        if entry.runs:
            return self._read_runlist(entry.runs, entry.size)
        return b""

    def iter_deleted(self, want_files: bool = True, progress_cb=None, cancel=None):
        """Yield deleted MFTEntry objects that still carry recoverable data."""
        prog = Progress(self.num_records, label="Quick scan ($MFT)", enabled=self.verbose)
        found = 0
        for recno in range(self.num_records):
            if recno % 4096 == 0:
                prog.update(recno)
                if progress_cb is not None:
                    progress_cb(recno, self.num_records, found)
                if cancel is not None and cancel():
                    break
            entry = self.read_record(recno)
            if not entry or entry.in_use:
                continue
            if entry.is_dir or not entry.has_data:
                continue
            if not entry.name:
                continue
            if entry.size == 0 and not entry.resident_data:
                continue
            found += 1
            yield entry
        prog.done()

    def recover(self, entry: MFTEntry, outdir: str) -> Optional[str]:
        data = self.read_data(entry)
        if not data:
            return None
        rel = self.resolve_path(entry)
        safe_parts = [sanitize_filename(p) for p in rel.split("/") if p not in ("", ".")]
        if not safe_parts:
            safe_parts = [sanitize_filename(entry.name)]
        dest_dir = ensure_dir(os.path.join(outdir, *safe_parts[:-1]) if len(safe_parts) > 1 else outdir)
        dest = os.path.join(dest_dir, safe_parts[-1])
        base, ext = os.path.splitext(dest)
        n = 1
        while os.path.exists(dest):
            dest = f"{base}_{n}{ext}"
            n += 1
        with open(dest, "wb") as f:
            f.write(data)
        return dest


def quick_scan(reader, outdir: str, verbose: bool = True, progress_cb=None, cancel=None):
    """Recover deleted, named files from an NTFS volume/image. Returns a summary
    dict: {'recovered': n, 'bytes': b, 'items': [(path, size), ...]}."""
    vol = NTFSVolume(reader, verbose=verbose)
    ensure_dir(outdir)
    recovered, total = 0, 0
    items = []
    for entry in vol.iter_deleted(progress_cb=progress_cb, cancel=cancel):
        try:
            dest = vol.recover(entry, outdir)
        except Exception:
            dest = None
        if dest:
            sz = os.path.getsize(dest)
            recovered += 1
            total += sz
            items.append((dest, sz))
    return {"recovered": recovered, "bytes": total, "items": items,
            "num_records": vol.num_records}
