"""
Finds which other programs are currently using the OBS Virtual Camera.

The OBS virtual camera is a DirectShow filter DLL. Any app that opens the
camera (Chrome, Zoom, ...) loads that DLL into its own process, so we look
for processes that have it loaded. Windows only.
"""
import ctypes
import os
from ctypes import wintypes

# DirectShow "Video Capture Sources" category
VIDEO_INPUT_CATEGORY = "{860BB310-5D01-11D0-BD3B-00A0C911CE86}"
FALLBACK_PREFIX = "obs-virtualcam-module"

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
LIST_MODULES_ALL = 0x03

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_psapi = ctypes.WinDLL("psapi", use_last_error=True)

_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.CloseHandle.restype = wintypes.BOOL
_psapi.EnumProcesses.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD,
                                 ctypes.POINTER(wintypes.DWORD)]
_psapi.EnumProcesses.restype = wintypes.BOOL
_psapi.EnumProcessModulesEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.HMODULE),
                                        wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                        wintypes.DWORD]
_psapi.EnumProcessModulesEx.restype = wintypes.BOOL
_psapi.GetModuleBaseNameW.argtypes = [wintypes.HANDLE, wintypes.HMODULE, wintypes.LPWSTR,
                                      wintypes.DWORD]
_psapi.GetModuleBaseNameW.restype = wintypes.DWORD


def find_vcam_dll_names():
    """File names of the OBS Virtual Camera DLLs, read from the DirectShow registration."""
    import winreg
    names = set()
    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        access = winreg.KEY_READ | view
        try:
            inst = winreg.OpenKey(winreg.HKEY_CLASSES_ROOT,
                                  r"CLSID\%s\Instance" % VIDEO_INPUT_CATEGORY, 0, access)
        except OSError:
            continue
        with inst:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(inst, i)
                except OSError:
                    break
                i += 1
                try:
                    with winreg.OpenKey(inst, sub, 0, access) as k:
                        fname = str(winreg.QueryValueEx(k, "FriendlyName")[0])
                        if "obs virtual" not in fname.lower():
                            continue
                        try:
                            clsid = str(winreg.QueryValueEx(k, "CLSID")[0])
                        except OSError:
                            clsid = sub
                    with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT,
                                        r"CLSID\%s\InprocServer32" % clsid, 0, access) as ks:
                        path = str(winreg.QueryValueEx(ks, "")[0])
                    names.add(os.path.basename(os.path.expandvars(path)).lower())
                except OSError:
                    continue
    return names


def _pids():
    size = 1024
    while True:
        arr = (wintypes.DWORD * size)()
        needed = wintypes.DWORD()
        if not _psapi.EnumProcesses(arr, ctypes.sizeof(arr), ctypes.byref(needed)):
            return []
        n = needed.value // ctypes.sizeof(wintypes.DWORD)
        if n < size:
            return list(arr[:n])
        size *= 2


def _module_names(pid):
    h = _k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        return None
    try:
        count = 512
        while True:
            mods = (wintypes.HMODULE * count)()
            needed = wintypes.DWORD()
            if not _psapi.EnumProcessModulesEx(h, mods, ctypes.sizeof(mods),
                                               ctypes.byref(needed), LIST_MODULES_ALL):
                return None
            n = needed.value // ctypes.sizeof(wintypes.HMODULE)
            if n <= count:
                break
            count = n
        buf = ctypes.create_unicode_buffer(260)
        names = []
        for m in mods[:n]:
            if m and _psapi.GetModuleBaseNameW(h, m, buf, 260):
                names.append(buf.value.lower())
        return names
    finally:
        _k32.CloseHandle(h)


def find_users(dll_names):
    """Sorted list of program names (other than this one) that have the
    virtual camera loaded."""
    me = os.getpid()
    users = set()
    for pid in _pids():
        if pid in (0, 4, me):
            continue
        mods = _module_names(pid)
        if not mods:
            continue
        if dll_names:
            hit = any(m in dll_names for m in mods)
        else:
            hit = any(m.startswith(FALLBACK_PREFIX) for m in mods)
        if hit:
            users.add(mods[0])  # first module is the program itself
    return sorted(users)
