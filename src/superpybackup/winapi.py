"""Duenne Windows-Schnittstellen ueber ctypes (keine externen Pakete).

Alle Funktionen sind klein und einzeln ersetzbar, damit Tests ohne Systemveraenderung moeglich sind.
"""

from __future__ import annotations

import ctypes
import os
import re
import sys
from ctypes import wintypes

IS_WINDOWS = sys.platform == "win32"

# Dateiattribute
ATTR_READONLY = 0x1
ATTR_HIDDEN = 0x2
ATTR_SYSTEM = 0x4
ATTR_DIRECTORY = 0x10
ATTR_ARCHIVE = 0x20
ATTR_TEMPORARY = 0x100
ATTR_REPARSE_POINT = 0x400
ATTR_OFFLINE = 0x1000
ATTR_RECALL_ON_OPEN = 0x40000
ATTR_RECALL_ON_DATA_ACCESS = 0x400000
# Dateien/Ordner, deren Inhalt erst aus der Cloud geladen werden muesste (OneDrive "nur online").
ATTR_ONLINE_ONLY = ATTR_OFFLINE | ATTR_RECALL_ON_OPEN | ATTR_RECALL_ON_DATA_ACCESS
# Attribute, die bei der Wiederherstellung gesetzt werden duerfen.
ATTR_RESTORABLE = ATTR_READONLY | ATTR_HIDDEN | ATTR_ARCHIVE

KNOWN_FOLDER_IDS = {
    "Desktop": "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}",
    "Documents": "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}",
    "Pictures": "{33E28130-4E1E-4676-835A-98395C3BC3BB}",
    "Music": "{4BD8D571-6D19-48D3-BE97-422220080E43}",
    "Videos": "{18989B1D-99B5-455B-841C-AB7C74E4DDFC}",
    "Downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
    "Favorites": "{1777F761-68AD-4D8A-87BD-30B759FA33DD}",
}


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]


def known_folder_path(name: str) -> str:
    """Tatsaechlicher Pfad eines Windows-Benutzerordners (beruecksichtigt Umleitung und OneDrive)."""
    if not IS_WINDOWS:
        raise OSError("Known Folders sind nur unter Windows verfuegbar.")
    if name not in KNOWN_FOLDER_IDS:
        raise KeyError(f"Unbekannter Windows-Ordner: {name}")
    guid = _GUID()
    ole32 = ctypes.windll.ole32
    ole32.CLSIDFromString.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(_GUID)]
    if ole32.CLSIDFromString(KNOWN_FOLDER_IDS[name], ctypes.byref(guid)) != 0:
        raise OSError(f"Ungueltige Ordner-ID: {name}")
    shell32 = ctypes.windll.shell32
    shell32.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(_GUID), wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
    shell32.SHGetKnownFolderPath.restype = ctypes.c_long
    pointer = ctypes.c_void_p()
    result = shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(pointer))
    try:
        if result != 0 or not pointer.value:
            raise OSError(f"Windows kennt keinen Pfad fuer {name} (HRESULT {result & 0xFFFFFFFF:#x}).")
        return ctypes.wstring_at(pointer.value)
    finally:
        if pointer.value:
            ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
            ole32.CoTaskMemFree(pointer)


def current_user_sid() -> str:
    """SID des aktuell angemeldeten Windows-Benutzers als Text (S-1-5-21-...)."""
    if not IS_WINDOWS:
        raise OSError("SID nur unter Windows verfuegbar.")
    advapi32 = ctypes.windll.advapi32
    kernel32 = ctypes.windll.kernel32
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise ctypes.WinError()
    try:
        needed = wintypes.DWORD()
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetTokenInformation(token, 1, buffer, needed, ctypes.byref(needed)):
            raise ctypes.WinError()
        sid_pointer = ctypes.c_void_p.from_buffer(buffer).value
        text = ctypes.c_void_p()
        if not advapi32.ConvertSidToStringSidW(sid_pointer, ctypes.byref(text)):
            raise ctypes.WinError()
        try:
            return ctypes.wstring_at(text.value)
        finally:
            kernel32.LocalFree(text)
    finally:
        kernel32.CloseHandle(token)


class NamedMutex:
    """Benannter Windows-Mutex (Local\\...) gegen parallele Laeufe desselben Benutzers auf diesem Rechner."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._handle = None
        self.owned = False

    def try_acquire(self) -> bool:
        if not IS_WINDOWS:
            self.owned = True
            return True
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        self._handle = kernel32.CreateMutexW(None, False, self.name)
        if not self._handle:
            raise ctypes.WinError()
        result = kernel32.WaitForSingleObject(self._handle, 0)
        self.owned = result in (0x0, 0x80)  # WAIT_OBJECT_0 / WAIT_ABANDONED
        if not self.owned:
            self.close()
        return self.owned

    def close(self) -> None:
        if self._handle and IS_WINDOWS:
            kernel32 = ctypes.windll.kernel32
            kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            if self.owned:
                kernel32.ReleaseMutex(self._handle)
            kernel32.CloseHandle(self._handle)
        self._handle = None
        self.owned = False

    def __enter__(self) -> "NamedMutex":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def volume_supports_hardlinks(path: str) -> bool:
    """True, wenn das Dateisystem des Pfads NTFS-Hardlinks unterstuetzt (FILE_SUPPORTS_HARD_LINKS)."""
    if not IS_WINDOWS:
        return hasattr(os, "link")
    root = os.path.splitdrive(os.path.abspath(path))[0]
    root = root + "\\"
    flags = wintypes.DWORD()
    kernel32 = ctypes.windll.kernel32
    kernel32.GetVolumeInformationW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD]
    if not kernel32.GetVolumeInformationW(root, None, 0, None, None, ctypes.byref(flags), None, 0):
        return False
    return bool(flags.value & 0x00400000)


def set_attributes(path: str, attributes: int) -> None:
    if not IS_WINDOWS:
        return
    kernel32 = ctypes.windll.kernel32
    kernel32.SetFileAttributesW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    if not kernel32.SetFileAttributesW(path, attributes):
        raise ctypes.WinError()


SAFE_NAME = re.compile(r"[^a-zA-Z0-9_.-]")


def sanitize_name(value: str) -> str:
    return SAFE_NAME.sub("_", value)
