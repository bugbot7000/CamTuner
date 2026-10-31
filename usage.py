"""
Finds which other programs are currently using the OBS Virtual Camera.

Step 1: the OBS virtual camera is a DirectShow filter DLL, so any app that has
ever opened it (Chrome, Zoom, ...) has that DLL loaded. Apps such as Chrome
keep the DLL loaded after they stop, so this alone is not enough.

Step 2: for those apps, look at their open handles for the shared-memory
video connection that the virtual camera uses to receive frames. An app is
counted as "active" only while it holds that connection.

The name of the shared memory is learned from CamTuner's own process, which
creates it when it starts sending to the virtual camera. Windows only.
"""
import ctypes
import os
from ctypes import wintypes

import numpy as np

# DirectShow "Video Capture Sources" category
VIDEO_INPUT_CATEGORY = "{860BB310-5D01-11D0-BD3B-00A0C911CE86}"
FALLBACK_PREFIX = "obs-virtualcam-module"
FALLBACK_SECTION_HINT = "obsvirtualcam"

PROCESS_DUP_HANDLE = 0x0040
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
LIST_MODULES_ALL = 0x03
DUPLICATE_SAME_ACCESS = 0x2
PAGE_READWRITE = 0x04
SYSTEM_EXTENDED_HANDLE_INFORMATION = 64
OBJECT_NAME_INFORMATION = 1
STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_psapi = ctypes.WinDLL("psapi", use_last_error=True)
_ntdll = ctypes.WinDLL("ntdll")

_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.CloseHandle.restype = wintypes.BOOL
_k32.GetCurrentProcess.argtypes = []
_k32.GetCurrentProcess.restype = wintypes.HANDLE
_k32.DuplicateHandle.argtypes = [wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
                                 ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD,
                                 wintypes.BOOL, wintypes.DWORD]
_k32.DuplicateHandle.restype = wintypes.BOOL
_k32.CreateFileMappingW.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.LPCWSTR]
_k32.CreateFileMappingW.restype = wintypes.HANDLE
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
_ntdll.NtQuerySystemInformation.argtypes = [wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG,
                                            ctypes.POINTER(wintypes.ULONG)]
_ntdll.NtQuerySystemInformation.restype = ctypes.c_long
_ntdll.NtQueryObject.argtypes = [wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p,
                                 wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
_ntdll.NtQueryObject.restype = ctypes.c_long

# SYSTEM_HANDLE_TABLE_ENTRY_INFO_EX (64-bit layout, 40 bytes)
_ENTRY = np.dtype([("obj", "<u8"), ("pid", "<u8"), ("handle", "<u8"), ("access", "<u4"),
                   ("backtrace", "<u2"), ("type", "<u2"), ("attr", "<u4"), ("reserved", "<u4")])


# ---------------------------------------------------------------- driver DLL

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


def _driver_holders(dll_names):
    """[(pid, program name)] of other processes that have the driver DLL loaded."""
    me = os.getpid()
    out = []
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
            out.append((pid, mods[0]))  # first module is the program itself
    return out


# ---------------------------------------------------------------- handles

_buf_size = 1 << 22
_section_type = None


def _system_handles():
    global _buf_size
    for _ in range(10):
        buf = ctypes.create_string_buffer(_buf_size)
        ret = wintypes.ULONG()
        st = _ntdll.NtQuerySystemInformation(SYSTEM_EXTENDED_HANDLE_INFORMATION, buf,
                                             _buf_size, ctypes.byref(ret)) & 0xFFFFFFFF
        if st == STATUS_INFO_LENGTH_MISMATCH:
            _buf_size = max(_buf_size * 2, ret.value + (1 << 20))
            continue
        if st != 0:
            raise OSError("NtQuerySystemInformation failed: 0x%08X" % st)
        n = ctypes.c_size_t.from_buffer(buf).value
        return np.frombuffer(buf, _ENTRY, count=n, offset=16).copy()
    raise OSError("System handle list too large")


def _get_section_type():
    """Object type number Windows uses for shared memory, found by creating
    our own small shared memory and looking it up."""
    global _section_type
    if _section_type is None:
        h = _k32.CreateFileMappingW(INVALID_HANDLE_VALUE, None, PAGE_READWRITE, 0, 4096, None)
        if not h:
            raise OSError("CreateFileMapping failed")
        try:
            t = _system_handles()
            rows = t[(t["pid"] == os.getpid()) & (t["handle"] == h)]
            if len(rows) == 0:
                raise OSError("Could not identify the shared memory object type")
            _section_type = int(rows["type"][0])
        finally:
            _k32.CloseHandle(h)
    return _section_type


def _object_name(proc_handle, value):
    """Name of a handle in another process, or None if it cannot be read."""
    dup = wintypes.HANDLE()
    if not _k32.DuplicateHandle(proc_handle, value, _k32.GetCurrentProcess(),
                                ctypes.byref(dup), 0, False, DUPLICATE_SAME_ACCESS):
        return None
    try:
        buf = ctypes.create_string_buffer(2048)
        ret = wintypes.ULONG()
        st = _ntdll.NtQueryObject(dup, OBJECT_NAME_INFORMATION, buf, 2048,
                                  ctypes.byref(ret)) & 0xFFFFFFFF
        if st != 0:
            return None
        length = ctypes.c_ushort.from_buffer(buf, 0).value
        ptr = ctypes.c_void_p.from_buffer(buf, 8).value
        if not ptr or not length:
            return ""
        return ctypes.wstring_at(ptr, length // 2)
    finally:
        _k32.CloseHandle(dup)


def _short(name):
    return name.rsplit("\\", 1)[-1].lower()


def _section_names(table, section_type, pid, proc_handle):
    """(names, unreadable_count) of shared memory handles held by pid."""
    rows = table[(table["pid"] == pid) & (table["type"] == section_type)]
    names, bad = [], 0
    for v in rows["handle"]:
        n = _object_name(proc_handle, int(v))
        if n is None:
            bad += 1
        elif n:
            names.append(n)
    return names, bad


class Detector:
    """Call scan() periodically. Returns a list of dicts:
    {"program", "pid", "state"} where state is
    "active"  - holds the video connection, so it is using the camera
    "idle"    - has the driver loaded but no video connection
    "unknown" - could not look inside this program (treated as active)"""

    def __init__(self):
        self.dll_names = find_vcam_dll_names()
        self.vcam_names = set()   # learned from our own process
        self.log = []             # messages for the log file

    def _learn_own_names(self, table, section_type):
        names, _ = _section_names(table, section_type, os.getpid(), _k32.GetCurrentProcess())
        found = {_short(n) for n in names if "obs" in n.lower() or "virtualcam" in n.lower()}
        if found and found != self.vcam_names:
            self.vcam_names = found
            self.log.append("Virtual camera connection names: %s" % sorted(found))

    def _is_vcam(self, name):
        s = _short(name)
        if self.vcam_names:
            return s in self.vcam_names
        return FALLBACK_SECTION_HINT in s

    def scan(self):
        holders = _driver_holders(self.dll_names)
        try:
            section_type = _get_section_type()
            table = _system_handles()
        except Exception as e:
            self.log.append("Handle check unavailable: %s" % e)
            return [{"program": exe, "pid": pid, "state": "unknown"} for pid, exe in holders]

        self._learn_own_names(table, section_type)

        out = []
        for pid, exe in holders:
            h = _k32.OpenProcess(PROCESS_DUP_HANDLE, False, pid)
            if not h:
                out.append({"program": exe, "pid": pid, "state": "unknown"})
                continue
            try:
                names, bad = _section_names(table, section_type, pid, h)
            finally:
                _k32.CloseHandle(h)
            if any(self._is_vcam(n) for n in names):
                state = "active"
            elif bad and not names:
                state = "unknown"
            else:
                state = "idle"
            out.append({"program": exe, "pid": pid, "state": state})
        return out
