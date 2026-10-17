"""Windows probes for the Warden: what is on screen, whether you are actually there, and a careful way to close things.

Only ctypes is used (no psutil / pywin32). Only the STATE of the mouse buttons is read, never which keys you type or what you
type. Every probe fails soft: an unreadable value is None / empty, never a crash. Closing is deliberately polite first
(WM_CLOSE, which is the same as pressing the window's X), forced only for a game that ignores it, and NEVER done to a browser,
a protected system program, Cerberus itself, or anything inside the Windows folder. For websites only the current TAB is closed.
"""
from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import wintypes
from pathlib import Path
from typing import Callable, Optional

WIN = sys.platform == "win32"

BROWSER_EXES = {"msedge.exe", "chrome.exe", "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe", "arc.exe", "iexplore.exe"}
# Never terminated, whatever the Warden is told. (Browsers are in here too: for them only the tab is closed.)
PROTECTED_EXES = BROWSER_EXES | {
    "explorer.exe", "system", "csrss.exe", "winlogon.exe", "dwm.exe", "services.exe", "lsass.exe", "svchost.exe",
    "wininit.exe", "smss.exe", "taskmgr.exe", "cmd.exe", "powershell.exe", "pwsh.exe", "windowsterminal.exe", "conhost.exe",
    "ollama.exe", "ollama app.exe", "cerberus.exe", "code.exe", "devenv.exe", "searchhost.exe", "startmenuexperiencehost.exe",
    "shellexperiencehost.exe", "applicationframehost.exe", "ctfmon.exe", "sihost.exe",
}

PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED = 0x1000
SYNCHRONIZE = 0x00100000
WM_CLOSE = 0x0010
TH32CS_SNAPPROCESS = 0x2
STILL_ACTIVE = 259
VK_LBUTTON, VK_RBUTTON, VK_MBUTTON = 0x01, 0x02, 0x04
VK_CONTROL, VK_W = 0x11, 0x57
KEYEVENTF_KEYUP = 0x0002

if WIN:
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _u32 = ctypes.WinDLL("user32", use_last_error=True)

    _u32.GetForegroundWindow.restype = wintypes.HWND
    _u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _u32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _u32.IsWindowVisible.argtypes = [wintypes.HWND]
    _u32.IsWindow.argtypes = [wintypes.HWND]
    _u32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    _u32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    _u32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t]
    _u32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    _k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _k32.GetTickCount.restype = wintypes.DWORD

    class _LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    _k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _ENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    _u32.EnumWindows.argtypes = [_ENUMPROC, wintypes.LPARAM]


def _norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(p)) if p else ""


# ---------------------------------------------------------------- reading the screen
def window_title(hwnd) -> str:
    if not WIN or not hwnd:
        return ""
    try:
        n = _u32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(min(n, 1000) + 2)
        _u32.GetWindowTextW(hwnd, buf, len(buf))
        return buf.value
    except OSError:
        return ""


def pid_path(pid: int) -> str:
    """Full path of the program a process was started from ('' if Windows will not say)."""
    if not WIN or not pid:
        return ""
    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED, False, int(pid))
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1040)
        size = wintypes.DWORD(len(buf))
        return buf.value if _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)) else ""
    finally:
        _k32.CloseHandle(h)


def foreground() -> Optional[dict]:
    """The window you are looking at: {hwnd, pid, exe, path, title}, or None (locked screen, desktop, no access)."""
    if not WIN:
        return None
    try:
        hwnd = _u32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = wintypes.DWORD(0)
        _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return None
        path = pid_path(pid.value)
        return {"hwnd": int(hwnd), "pid": int(pid.value), "path": path, "exe": Path(path).name.lower() if path else "",
                "title": window_title(hwnd)}
    except OSError:
        return None


def idle_seconds() -> float:
    """Seconds since the last real mouse or keyboard input. 0.0 if it cannot be read (counts as 'present': fail toward watching)."""
    if not WIN:
        return 0.0
    try:
        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
        if not _u32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        return ((_k32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0
    except (OSError, AttributeError):
        return 0.0


def mouse_buttons() -> tuple[bool, bool, bool]:
    """Which mouse buttons are down right now (left, right, middle). Nothing else about input is read."""
    if not WIN:
        return (False, False, False)
    try:
        return tuple(bool(_u32.GetAsyncKeyState(vk) & 0x8000) for vk in (VK_LBUTTON, VK_RBUTTON, VK_MBUTTON))  # type: ignore[return-value]
    except OSError:
        return (False, False, False)


def alive(pid: int) -> bool:
    if not WIN or not pid:
        return False
    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED, False, int(pid))
    if not h:
        return False
    try:
        code = wintypes.DWORD(0)
        return bool(_k32.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == STILL_ACTIVE
    finally:
        _k32.CloseHandle(h)


def window_exists(hwnd) -> bool:
    return bool(WIN and hwnd and _u32.IsWindow(hwnd))


def all_processes() -> list[tuple[int, str]]:
    """[(pid, exe name)] for every running process."""
    out: list[tuple[int, str]] = []
    if not WIN:
        return out
    snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == ctypes.c_void_p(-1).value:
        return out
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        ok = _k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out.append((int(entry.th32ProcessID), entry.szExeFile))
            ok = _k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        _k32.CloseHandle(snap)
    return out


def pids_with_path(path: str) -> list[int]:
    """Every running process started from exactly this program file (a game and its helper processes)."""
    want = _norm(path)
    if not want:
        return []
    base = Path(path).name.lower()
    return [pid for pid, name in all_processes() if name.lower() == base and _norm(pid_path(pid)) == want]


# ---------------------------------------------------------------- what may never be closed
def _windows_dir() -> str:
    return _norm(os.environ.get("SystemRoot") or r"C:\Windows")


def own_pids() -> set[int]:
    """Cerberus's own process and its parents (so closing can never take down the app or the launcher that started it)."""
    mine = {os.getpid()}
    if WIN:
        parents = {pid: ppid for pid, ppid in _parent_map().items()}
        cur = os.getpid()
        for _ in range(8):
            cur = parents.get(cur, 0)
            if not cur or cur in mine:
                break
            mine.add(cur)
    return mine


def _parent_map() -> dict[int, int]:
    out: dict[int, int] = {}
    if not WIN:
        return out
    snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == ctypes.c_void_p(-1).value:
        return out
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        ok = _k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            ok = _k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        _k32.CloseHandle(snap)
    return out


def refusal_reason(pid: int, path: str, own: Optional[set[int]] = None) -> str:
    """'' if this process may be closed, otherwise why it must not be."""
    exe = Path(path).name.lower() if path else ""
    if pid in (own if own is not None else own_pids()):
        return "that is Cerberus itself (or what launched it)"
    if not path:
        return "Windows will not say what program that is"
    if exe in PROTECTED_EXES:
        return f"{exe} is a protected program"
    win = _windows_dir()
    if _norm(path).startswith(win + os.sep) or _norm(path) == win:
        return "it lives in the Windows folder"
    return ""


# ---------------------------------------------------------------- closing things
def _top_windows_of(pids: set[int]) -> list[int]:
    found: list[int] = []
    if not WIN:
        return found

    def cb(hwnd, _lparam):
        pid = wintypes.DWORD(0)
        _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and _u32.IsWindowVisible(hwnd):
            found.append(int(hwnd))
        return True

    _u32.EnumWindows(_ENUMPROC(cb), 0)
    return found


def close_app(path: str, pid: int = 0, *, polite_wait: float = 8.0, sleep: Callable[[float], None] = time.sleep) -> dict:
    """Close a program politely (the same as clicking its X), then force it only if it ignores that.
    Returns {closed, method: polite|forced|refused|gone|failed, detail}. Never touches protected programs."""
    if not WIN:
        return {"closed": False, "method": "failed", "detail": "closing programs is only supported on Windows"}
    own = own_pids()
    why = refusal_reason(pid or 0, path, own)
    if why:
        return {"closed": False, "method": "refused", "detail": f"Not closing it: {why}."}
    want = _norm(path)
    targets = set(pids_with_path(path)) or ({pid} if pid and alive(pid) and _norm(pid_path(pid)) == want else set())
    targets -= own
    targets = {p for p in targets if not refusal_reason(p, pid_path(p) or path, own)}
    if not targets:
        return {"closed": True, "method": "gone", "detail": "It had already closed."}
    for hwnd in _top_windows_of(targets):
        _u32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    deadline = time.monotonic() + max(0.0, polite_wait)
    while time.monotonic() < deadline:
        if not any(alive(p) for p in targets):
            return {"closed": True, "method": "polite", "detail": "It closed when asked."}
        sleep(0.25)
    left = [p for p in targets if alive(p)]
    if not left:
        return {"closed": True, "method": "polite", "detail": "It closed when asked."}
    for p in left:
        if _norm(pid_path(p)) != want:      # the PID may have been recycled by another program since we looked
            continue
        h = _k32.OpenProcess(PROCESS_TERMINATE, False, p)
        if h:
            try:
                _k32.TerminateProcess(h, 1)
            finally:
                _k32.CloseHandle(h)
    sleep(0.5)
    still = [p for p in left if alive(p)]
    if still:
        return {"closed": False, "method": "failed", "detail": "It would not close (it may be protected by anti-cheat)."}
    return {"closed": True, "method": "forced", "detail": "It ignored the polite request, so it was ended."}


def close_tab(hwnd: int, still_matches: Callable[[str], bool], *, tries: int = 2,
              sleep: Callable[[float], None] = time.sleep) -> dict:
    """Close the current TAB of a browser window with Ctrl+W, but only while that window is still in front and still shows the
    page in question. The browser itself is never ended. Returns {closed, detail}."""
    if not WIN:
        return {"closed": False, "detail": "closing tabs is only supported on Windows"}
    for _ in range(max(1, tries)):
        fg = foreground()
        if not fg or fg["hwnd"] != hwnd:
            return {"closed": True, "left": True, "detail": "You had already left that page."}
        if not still_matches(fg["title"]):
            return {"closed": True, "left": True, "detail": "That page is no longer showing."}
        title_before = fg["title"]
        try:
            _u32.keybd_event(VK_CONTROL, 0, 0, 0)
            _u32.keybd_event(VK_W, 0, 0, 0)
            _u32.keybd_event(VK_W, 0, KEYEVENTF_KEYUP, 0)
        finally:
            _u32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)
        sleep(1.2)
        now = foreground()
        if not window_exists(hwnd) or not now or now["hwnd"] != hwnd or now["title"] != title_before:
            return {"closed": True, "detail": "The tab was closed."}
    return {"closed": False, "detail": "The tab would not close."}


def focus(hwnd: int) -> bool:
    """Bring a window to the front (used so a tab can be closed while the Warden's own dialog holds focus). True if it worked."""
    if not WIN or not hwnd:
        return False
    try:
        if _u32.IsIconic(hwnd):
            _u32.ShowWindow(hwnd, 9)
        _u32.SetForegroundWindow(hwnd)
        for _ in range(6):
            fg = foreground()
            if fg and fg["hwnd"] == hwnd:
                return True
            time.sleep(0.1)
    except OSError:
        pass
    return False


def message_box(title: str, text: str) -> None:
    """Always-on-top plain Windows message, used only when no Cerberus window is open to show the alert."""
    if not WIN:
        return
    ctypes.windll.user32.MessageBoxW(0, text, title, 0x00000030 | 0x00040000 | 0x00010000)   # warning icon, topmost, foreground
