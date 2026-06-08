"""
recolib - a from-scratch data-recovery toolkit (an EaseUS-style recovery engine).

Modules
-------
disk        Raw device / volume / image access + drive enumeration (Windows).
signatures  Database of file signatures (magic numbers) used by the carver.
carver      File-carving engine  (the "deep / RAW scan").
ntfs        NTFS $MFT parser      (the "quick scan" - recovers original names).
recyclebin  Windows $Recycle.Bin parser.
utils       Shared helpers (sizes, progress bar, safe filenames).
"""

__version__ = "1.0.0"
__all__ = ["disk", "signatures", "carver", "ntfs", "recyclebin", "utils"]
