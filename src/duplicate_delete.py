"""Move confirmed duplicate MP4 files to the Windows Recycle Bin.

Only files listed in a duplicate group from ``scan_duplicates`` may be removed,
never the file chosen to keep, and only while both the kept file and the
target still match the size and modification time recorded by the scan.
Files go to the Recycle Bin so the user can restore them; when Windows cannot
recycle (for example on some network drives) it asks before deleting forever.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import stat

FO_DELETE = 0x0003
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040
FOF_NOERRORUI = 0x0400
FOF_WANTNUKEWARNING = 0x4000


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
                ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
                ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]


def _matches(path: Path, entry: dict) -> str | None:
    """Return a reason when ``path`` is no longer the file the scan saw."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return "File không còn tồn tại."
    except OSError as exc:
        return f"Không đọc được file: {exc}"
    if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        return "Không phải file thường."
    if info.st_size != entry.get("bytes"):
        return "Dung lượng đã thay đổi sau khi quét."
    if entry.get("mtime_ns") is not None and info.st_mtime_ns != entry["mtime_ns"]:
        return "File đã bị sửa sau khi quét."
    return None


def recycle(path: Path, hwnd: int | None = None) -> None:
    """Send one file to the Recycle Bin, raising ``OSError`` on failure."""
    operation = _SHFILEOPSTRUCTW(hwnd=hwnd, wFunc=FO_DELETE, pFrom=str(path) + "\0",
                                 fFlags=FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT
                                 | FOF_NOERRORUI | FOF_WANTNUKEWARNING)
    code = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    if operation.fAnyOperationsAborted:
        raise OSError("Đã hủy xóa.")
    if code:
        hint = (" Đường dẫn quá dài hoặc chứa tên đặc biệt của Windows (con, aux, nul…)."
                if code in (0x79, 0x7C, 0x81, 0x402) else "")
        raise OSError(f"Windows không xóa được file (mã 0x{code:X}).{hint}")
    if os.path.lexists(path):
        raise OSError("File vẫn còn sau khi xóa.")


def delete_duplicates(group: dict, keep: str, targets: list[str],
                      hwnd: int | None = None, remove=recycle) -> list[dict]:
    """Recycle ``targets`` from ``group`` while keeping ``keep``.

    Returns one ``{"path", "status", "reason"}`` entry per target, where
    status is ``deleted``, ``skipped`` or ``error``. Nothing is removed when
    the kept file is missing or changed.
    """
    entries = {os.path.normcase(str(Path(f["path"]))): f for f in group.get("files", [])}
    keep_key = os.path.normcase(str(Path(keep)))
    results = []
    keep_problem = _matches(Path(keep), entries[keep_key]) if keep_key in entries else "Bản giữ không thuộc nhóm."
    for target in targets:
        key = os.path.normcase(str(Path(target)))
        if keep_problem:
            reason = "Bản giữ lại không còn nguyên vẹn: " + keep_problem
        elif key == keep_key:
            reason = "Đây là bản được giữ lại."
        elif key not in entries:
            reason = "File không thuộc nhóm trùng."
        else:
            reason = _matches(Path(target), entries[key])
        if reason:
            results.append({"path": str(target), "status": "skipped", "reason": reason})
            continue
        try:
            remove(Path(target), hwnd)
            results.append({"path": str(target), "status": "deleted", "reason": ""})
        except OSError as exc:
            results.append({"path": str(target), "status": "error", "reason": str(exc)})
    return results
