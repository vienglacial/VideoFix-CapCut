"""Native Explorer file drops for a Tk toplevel, without extra packages.

Call ``attach_drop_target(root, on_paths)`` on Tk's UI thread after creating
the root. Keep the returned object and call ``close()`` before destroying the
GUI. Destruction also detaches automatically. ``enabled`` is false when the
feature cannot be installed; file-picker and command-line input can continue.

Use a normal, unelevated process: Windows can block Explorer drops into an
elevated application. This helper does not change message-filter permissions.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import sys
from typing import Callable


WM_DROPFILES = 0x0233
WM_NCDESTROY = 0x0082
GWLP_WNDPROC = -4
GWL_EXSTYLE = -20
WS_EX_ACCEPTFILES = 0x00000010
GA_ROOT = 2

# Windows retains only a pointer to a WNDPROC, not a Python reference. Keep
# targets alive until every installed callback is removed or its HWND dies.
_LIVE_TARGETS: dict[int, "_DropTarget"] = {}


@dataclass
class _Hook:
    hwnd: int
    previous_proc: int
    accepted_files: bool


class _WinApi:
    def __init__(self) -> None:
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        # LONG_PTR/LRESULT are pointer-sized, including on 64-bit Python.
        self.LONG_PTR = ctypes.c_ssize_t
        self.WNDPROC = ctypes.WINFUNCTYPE(
            self.LONG_PTR, wintypes.HWND, wintypes.UINT,
            wintypes.WPARAM, wintypes.LPARAM,
        )
        get_name = "GetWindowLongPtrW" if ctypes.sizeof(ctypes.c_void_p) == 8 else "GetWindowLongW"
        set_name = "SetWindowLongPtrW" if ctypes.sizeof(ctypes.c_void_p) == 8 else "SetWindowLongW"
        self.get_window_long = getattr(self.user32, get_name)
        self.get_window_long.argtypes = [wintypes.HWND, ctypes.c_int]
        self.get_window_long.restype = self.LONG_PTR
        self.set_window_long = getattr(self.user32, set_name)
        self.set_window_long.argtypes = [wintypes.HWND, ctypes.c_int, self.LONG_PTR]
        self.set_window_long.restype = self.LONG_PTR
        self.call_window_proc = self.user32.CallWindowProcW
        self.call_window_proc.argtypes = [
            ctypes.c_void_p, wintypes.HWND, wintypes.UINT,
            wintypes.WPARAM, wintypes.LPARAM,
        ]
        self.call_window_proc.restype = self.LONG_PTR
        self.def_window_proc = self.user32.DefWindowProcW
        self.def_window_proc.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        ]
        self.def_window_proc.restype = self.LONG_PTR
        self.get_ancestor = self.user32.GetAncestor
        self.get_ancestor.argtypes = [wintypes.HWND, wintypes.UINT]
        self.get_ancestor.restype = wintypes.HWND
        self.is_window = self.user32.IsWindow
        self.is_window.argtypes = [wintypes.HWND]
        self.is_window.restype = wintypes.BOOL
        self.window_thread = self.user32.GetWindowThreadProcessId
        self.window_thread.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.window_thread.restype = wintypes.DWORD
        self.current_thread = self.kernel32.GetCurrentThreadId
        self.current_thread.argtypes = []
        self.current_thread.restype = wintypes.DWORD
        self.accept_files = self.shell32.DragAcceptFiles
        self.accept_files.argtypes = [wintypes.HWND, wintypes.BOOL]
        self.accept_files.restype = None
        self.query_file = self.shell32.DragQueryFileW
        self.query_file.argtypes = [
            wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT,
        ]
        self.query_file.restype = wintypes.UINT
        self.finish_drop = self.shell32.DragFinish
        self.finish_drop.argtypes = [wintypes.HANDLE]
        self.finish_drop.restype = None

    def replace_proc(self, hwnd: int, proc: int) -> int:
        ctypes.set_last_error(0)
        previous = self.set_window_long(hwnd, GWLP_WNDPROC, proc)
        error = ctypes.get_last_error()
        if previous == 0 and error:
            raise ctypes.WinError(error)
        return previous


class _DropTarget:
    def __init__(self, root: object, on_paths: Callable[[list[str]], None]) -> None:
        self.enabled = False
        self.error: str | None = None
        self._root = root
        self._on_paths = on_paths
        self._api: _WinApi | None = None
        self._hooks: dict[int, _Hook] = {}
        self._wndproc = None
        self._proc_address = 0
        self._destroy_binding: str | None = None
        self._pending: set[str] = set()
        self._closed = False
        self._ui_thread = 0

        if sys.platform != "win32":
            self.error = "Native file drops are available only on Windows."
            return
        try:
            if not callable(on_paths):
                raise TypeError("on_paths must be callable")
            self._api = _WinApi()
            self._ui_thread = self._api.current_thread()
            root.update_idletasks()
            client = int(root.winfo_id())
            if not client or not self._api.is_window(client):
                raise RuntimeError("Tk did not provide a valid window handle")
            if self._api.window_thread(client, None) != self._ui_thread:
                raise RuntimeError("Attach file drops on the Tk window's UI thread")
            wrapper = int(self._api.get_ancestor(client, GA_ROOT) or client)
            self._wndproc = self._api.WNDPROC(self._dispatch)
            self._proc_address = self._api.LONG_PTR(
                ctypes.cast(self._wndproc, ctypes.c_void_p).value
            ).value
            _LIVE_TARGETS[id(self)] = self

            # Tk exposes its client HWND; Explorer can target Tk's outer
            # wrapper instead. Handle both, without installing twice.
            for hwnd in dict.fromkeys((wrapper, client)):
                accepted = bool(self._api.get_window_long(hwnd, GWL_EXSTYLE) & WS_EX_ACCEPTFILES)
                previous = self._api.replace_proc(hwnd, self._proc_address)
                self._hooks[hwnd] = _Hook(hwnd, previous, accepted)
                self._api.accept_files(hwnd, True)
            self._destroy_binding = root.bind("<Destroy>", self._on_destroy, add="+")
            self.enabled = True
        except Exception as exc:
            self.error = str(exc)
            self.close()

    def _on_destroy(self, event: object) -> None:
        if getattr(event, "widget", None) is self._root:
            self.close()

    def _queue_paths(self, paths: list[str]) -> None:
        if not paths or self._closed or not self.enabled:
            return
        token: list[str | None] = [None]

        def deliver() -> None:
            if token[0] is not None:
                self._pending.discard(token[0])
            if self._closed or not self.enabled:
                return
            try:
                self._on_paths(paths)
            except BaseException as exc:
                # Match Tk's normal callback error reporting; never unwind
                # a Python exception through a native WNDPROC frame.
                try:
                    self._root.report_callback_exception(type(exc), exc, exc.__traceback__)
                except Exception:
                    pass

        try:
            token[0] = self._root.after(0, deliver)
            self._pending.add(token[0])
        except Exception:
            # Closing/destroyed Tk interpreter: the native HDROP has already
            # been freed; there is nothing left to deliver.
            pass

    def _read_drop(self, handle: int) -> list[str]:
        api = self._api
        paths: list[str] = []
        try:
            count = api.query_file(handle, 0xFFFFFFFF, None, 0)
            for index in range(count):
                length = api.query_file(handle, index, None, 0)
                if not length:
                    continue
                buffer = ctypes.create_unicode_buffer(length + 1)
                if api.query_file(handle, index, buffer, length + 1):
                    paths.append(buffer.value)
        finally:
            api.finish_drop(handle)
        return paths

    def _dispatch(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        api = self._api
        hook = self._hooks.get(int(hwnd))
        previous = hook.previous_proc if hook is not None else 0
        if message == WM_DROPFILES and hook is not None:
            try:
                self._queue_paths(self._read_drop(wparam))
            except BaseException as exc:
                self.error = str(exc)
            # _read_drop always calls DragFinish. Never forward a freed
            # HDROP to another window procedure, including on an error.
            return 0
        try:
            if message == WM_NCDESTROY and hook is not None:
                # A later subclass may still chain through us. HWND death
                # ends that chain even if we were unable to detach earlier.
                if api.get_window_long(hwnd, GWLP_WNDPROC) == self._proc_address:
                    api.replace_proc(hwnd, previous)
                self._hooks.pop(int(hwnd), None)
                if not self._hooks:
                    self.enabled = False
                    _LIVE_TARGETS.pop(id(self), None)
        except BaseException as exc:
            self.error = str(exc)
        if previous:
            return int(api.call_window_proc(previous, hwnd, message, wparam, lparam))
        return int(api.def_window_proc(hwnd, message, wparam, lparam))

    def close(self) -> None:
        """Stop delivering drops and safely restore each previous WNDPROC.

        Normally call this on the UI thread. It is idempotent and tolerates
        already-destroyed windows. A later-installed subclass is preserved;
        our callback stays alive until that window's WM_NCDESTROY in that case.
        """
        self._closed = True
        self.enabled = False
        api = self._api
        if api is None:
            return
        on_ui_thread = api.current_thread() == self._ui_thread
        if on_ui_thread:
            for token in tuple(self._pending):
                try:
                    self._root.after_cancel(token)
                except Exception:
                    pass
            self._pending.clear()
            if self._destroy_binding is not None:
                try:
                    self._root.unbind("<Destroy>", self._destroy_binding)
                except Exception:
                    pass
                self._destroy_binding = None
        for hwnd, hook in tuple(self._hooks.items()):
            try:
                if not api.is_window(hwnd):
                    self._hooks.pop(hwnd, None)
                elif api.get_window_long(hwnd, GWLP_WNDPROC) == self._proc_address:
                    api.replace_proc(hwnd, hook.previous_proc)
                    api.accept_files(hwnd, hook.accepted_files)
                    self._hooks.pop(hwnd, None)
                # Do not overwrite a newer subclass. Keep our WNDPROC alive
                # so its saved predecessor pointer remains valid.
            except Exception as exc:
                self.error = str(exc)
        if not self._hooks:
            _LIVE_TARGETS.pop(id(self), None)


def attach_drop_target(root: object, on_paths: Callable[[list[str]], None]) -> _DropTarget:
    """Enable Unicode multiple-file/folder drops; return a closable target.

    Read ``target.enabled`` and optionally ``target.error`` to detect a
    graceful fallback. Paths are passed as a list on Tk's event loop.
    """
    return _DropTarget(root, on_paths)
