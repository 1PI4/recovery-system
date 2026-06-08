r"""
Raw storage access + enumeration.

A "reader" is anything with:
    .size                      -> total bytes
    .sector                    -> logical sector size
    .read_at(offset, length)   -> bytes   (handles raw-device sector alignment)
    .close()

Three reader kinds:
    RawDevice  -> a live Windows volume (\\.\C:) or physical disk (\\.\PhysicalDrive0)
    FileImage  -> a .img/.dd/.bin/.iso disk image (no admin needed, any OS)

open_target("C:"), open_target("0"), open_target(r"C:\path\to\image.dd") all work.
"""
from __future__ import annotations
import os
import struct
import sys

IS_WINDOWS = os.name == "nt"
DEFAULT_SECTOR = 512


class DeviceError(Exception):
    pass


def is_admin() -> bool:
    if IS_WINDOWS:
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    try:
        return os.geteuid() == 0
    except AttributeError:
        return False


# --------------------------------------------------------------------------- #
#  Plain image-file reader (works on every platform, no privileges)
# --------------------------------------------------------------------------- #
class FileImage:
    def __init__(self, path: str):
        self.path = path
        self._f = open(path, "rb")
        self._f.seek(0, os.SEEK_END)
        self.size = self._f.tell()
        self._f.seek(0)
        self.sector = DEFAULT_SECTOR

    def read_at(self, offset: int, length: int) -> bytes:
        if length <= 0 or offset >= self.size:
            return b""
        self._f.seek(offset)
        return self._f.read(length)

    def close(self):
        try:
            self._f.close()
        except Exception:
            pass

    def __repr__(self):
        return f"<FileImage {self.path} {self.size} bytes>"


# --------------------------------------------------------------------------- #
#  Windows raw device reader (volumes & physical disks)  -- requires elevation
# --------------------------------------------------------------------------- #
if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    GENERIC_READ = 0x80000000
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    OPEN_EXISTING = 3
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    IOCTL_DISK_GET_DRIVE_GEOMETRY = 0x00070000
    IOCTL_DISK_GET_LENGTH_INFO = 0x0007405C

    _k32.CreateFileW.restype = wintypes.HANDLE
    _k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                 wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                 wintypes.HANDLE]
    _k32.ReadFile.restype = wintypes.BOOL
    _k32.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                              ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    _k32.SetFilePointerEx.restype = wintypes.BOOL
    _k32.SetFilePointerEx.argtypes = [wintypes.HANDLE, ctypes.c_longlong,
                                      ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD]
    _k32.DeviceIoControl.restype = wintypes.BOOL
    _k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID,
                                     wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                     ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    _k32.CloseHandle.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]

    class RawDevice:
        def __init__(self, path: str):
            self.path = path
            self.sector = DEFAULT_SECTOR
            self.size = 0
            h = _k32.CreateFileW(path, GENERIC_READ,
                                 FILE_SHARE_READ | FILE_SHARE_WRITE, None,
                                 OPEN_EXISTING, 0, None)
            if h == INVALID_HANDLE_VALUE or not h:
                err = ctypes.get_last_error()
                hint = ""
                if err == 5:
                    hint = " (Access denied - run from an *elevated* Administrator shell)"
                elif err == 2:
                    hint = " (No such device)"
                raise DeviceError(f"Cannot open {path}: WinError {err}{hint}")
            self._h = h
            self._probe_geometry()

        def _ioctl(self, code, out_len):
            out = ctypes.create_string_buffer(out_len)
            returned = wintypes.DWORD(0)
            ok = _k32.DeviceIoControl(self._h, code, None, 0, out, out_len,
                                      ctypes.byref(returned), None)
            return out.raw[:returned.value] if ok else None

        def _probe_geometry(self):
            geo = self._ioctl(IOCTL_DISK_GET_DRIVE_GEOMETRY, 24)
            if geo and len(geo) >= 24:
                bps = struct.unpack_from("<I", geo, 20)[0]
                if bps in (512, 1024, 2048, 4096):
                    self.sector = bps
            length = self._ioctl(IOCTL_DISK_GET_LENGTH_INFO, 8)
            if length and len(length) >= 8:
                self.size = struct.unpack_from("<q", length, 0)[0]

        def read_at(self, offset: int, length: int) -> bytes:
            if length <= 0:
                return b""
            s = self.sector
            start = offset - (offset % s)
            end = offset + length
            if end % s:
                end += s - (end % s)
            to_read = end - start

            pos = ctypes.c_longlong(0)
            if not _k32.SetFilePointerEx(self._h, ctypes.c_longlong(start),
                                         ctypes.byref(pos), 0):
                raise DeviceError(f"seek failed @ {start}: WinError {ctypes.get_last_error()}")

            buf = ctypes.create_string_buffer(to_read)
            total = 0
            while total < to_read:
                want = min(to_read - total, 16 * 1024 * 1024)
                got = wintypes.DWORD(0)
                ok = _k32.ReadFile(self._h, ctypes.byref(buf, total), want,
                                   ctypes.byref(got), None)
                if not ok or got.value == 0:
                    break  # hit a bad sector or the end of the device
                total += got.value
            data = buf.raw[:total]
            skip = offset - start
            return data[skip:skip + length]

        def close(self):
            try:
                _k32.CloseHandle(self._h)
            except Exception:
                pass

        def __repr__(self):
            return f"<RawDevice {self.path} {self.size} bytes sector={self.sector}>"

    # -- enumeration ------------------------------------------------------- #
    def list_volumes():
        """Return [{'letter','type','total','free'}] for mounted volumes."""
        out = []
        try:
            bitmask = _k32.GetLogicalDrives()
        except Exception:
            bitmask = 0
        for i in range(26):
            if not (bitmask >> i) & 1:
                continue
            letter = f"{chr(ord('A') + i)}:"
            root = letter + "\\"
            try:
                dtype = ctypes.windll.kernel32.GetDriveTypeW(root)
            except Exception:
                dtype = 0
            kind = {2: "removable", 3: "fixed", 4: "network",
                    5: "cdrom", 6: "ramdisk"}.get(dtype, "unknown")
            free = ctypes.c_ulonglong(0)
            total = ctypes.c_ulonglong(0)
            try:
                ctypes.windll.kernel32.GetDiskFreeSpaceExW(
                    ctypes.c_wchar_p(root), None,
                    ctypes.byref(total), ctypes.byref(free))
            except Exception:
                pass
            out.append({"letter": letter, "type": kind,
                        "total": total.value, "free": free.value})
        return out

    def list_physical_drives(max_probe: int = 16):
        r"""Return [{'path','index','size'}] for \\.\PhysicalDriveN that open."""
        out = []
        for i in range(max_probe):
            path = rf"\\.\PhysicalDrive{i}"
            try:
                dev = RawDevice(path)
            except DeviceError:
                continue
            out.append({"path": path, "index": i, "size": dev.size})
            dev.close()
        return out

else:  # ---- non-Windows: keep the import working for image files ---------- #
    class RawDevice(FileImage):
        pass

    def list_volumes():
        return []

    def list_physical_drives(max_probe: int = 16):
        return []


# --------------------------------------------------------------------------- #
#  Target resolution
# --------------------------------------------------------------------------- #
def resolve_target_path(spec: str) -> str:
    """Turn a friendly target into something open_target understands."""
    if os.path.exists(spec):
        return spec
    s = spec.strip().strip("\\").strip("/")
    low = s.lower()
    if low.startswith("physicaldrive") or (s.isdigit()):
        num = "".join(ch for ch in s if ch.isdigit())
        return rf"\\.\PhysicalDrive{num}"
    # drive letter forms: C, C:, C:\
    letter = s.rstrip(":\\")
    if len(letter) == 1 and letter.isalpha():
        return rf"\\.\{letter.upper()}:"
    return spec  # let open_target raise a clear error


def open_target(spec: str):
    """Open a volume letter, physical-drive number, or image file as a reader."""
    path = resolve_target_path(spec)
    if path.startswith("\\\\.\\"):
        if not IS_WINDOWS:
            raise DeviceError("Raw devices are only supported on Windows.")
        return RawDevice(path)
    if os.path.isfile(path):
        return FileImage(path)
    raise DeviceError(f"Don't know how to open target: {spec!r}")


def chunks(reader, chunk_size: int, overlap: int = 0, start: int = 0, end: int = None):
    """Yield (absolute_offset, data) windows across a reader, with overlap so a
    signature straddling a boundary is still seen in the next window."""
    if end is None:
        end = reader.size
    pos = start
    step = max(1, chunk_size - overlap)
    while pos < end:
        data = reader.read_at(pos, min(chunk_size, end - pos))
        if not data:
            break
        yield pos, data
        if len(data) < chunk_size:
            break
        pos += step
