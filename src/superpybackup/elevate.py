"""Auf Anfrage einen Befehl mit Administratorrechten ausfuehren (Windows-Abfrage "Benutzerkontensteuerung").

Die Oberflaeche selbst laeuft nie als Administrator. Fuer die wenigen Einstellungen, die Administratorrechte brauchen (Ereignisprotokoll
einschalten, Aufgabe mit hoechsten Rechten einrichten), startet ein Knopf einen kurzen Hilfsprozess mit der Konsolenversion, den Windows
vorher mit seiner Abfrage bestaetigen laesst. Lehnt der Benutzer ab, passiert nichts. Die Ausgabe des Hilfsprozesses kommt ueber eine
Temp-Datei zurueck und wird in der Oberflaeche angezeigt.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
from ctypes import wintypes
from pathlib import Path

ERROR_CANCELLED = 1223
SEE_MASK_NOCLOSEPROCESS = 0x40
SEE_MASK_FLAG_NO_UI = 0x400  # keine Fehlerfenster von Windows (die Abfrage zur Administrator-Bestaetigung bleibt)
SW_HIDE = 0
INFINITE = 0xFFFFFFFF


class ElevationDeclined(Exception):
    """Die Administrator-Abfrage von Windows wurde abgelehnt oder abgebrochen."""


class ElevationError(Exception):
    pass


class _ShellExecuteInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG), ("hwnd", wintypes.HWND), ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR), ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE),
    ]


def shell_execute(file: str, parameters: str, *, verb: str = "runas", timeout_ms: int = INFINITE) -> int:
    """Startet `file` versteckt mit dem Verb (Standard "runas" = als Administrator, mit Windows-Abfrage) und wartet auf das Ende.

    Liefert den Rueckgabewert des Prozesses. ElevationDeclined, wenn die Abfrage abgelehnt wurde; ElevationError sonst.
    """
    if sys.platform != "win32":
        raise ElevationError("Nur unter Windows verfuegbar.")
    kernel = ctypes.windll.kernel32
    shell = ctypes.windll.shell32
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    shell.ShellExecuteExW.argtypes = [ctypes.POINTER(_ShellExecuteInfo)]
    info = _ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_FLAG_NO_UI
    info.lpVerb = verb
    info.lpFile = file
    info.lpParameters = parameters
    info.nShow = SW_HIDE
    if not shell.ShellExecuteExW(ctypes.byref(info)):
        code = ctypes.get_last_error() or kernel.GetLastError()
        if code == ERROR_CANCELLED:
            raise ElevationDeclined("Die Administrator-Abfrage wurde abgelehnt.")
        raise ElevationError(f"Der Befehl konnte nicht gestartet werden (Windows-Fehler {code}).")
    handle = info.hProcess
    if not handle:
        raise ElevationError("Der gestartete Prozess ist nicht erreichbar.")
    try:
        if kernel.WaitForSingleObject(handle, timeout_ms) != 0:
            raise ElevationError("Zeitueberschreitung beim Warten auf den Befehl.")
        exit_code = wintypes.DWORD()
        kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        return int(exit_code.value)
    finally:
        kernel.CloseHandle(handle)


def console_command() -> list[str]:
    """Programm und feste Argumente der Konsolenversion (SuperPyBackupCmd.exe bzw. beim Start aus dem Quellcode python -m superpybackup)."""
    if getattr(sys, "frozen", False):
        executable = Path(sys.executable)
        console = executable.with_name("SuperPyBackupCmd.exe")
        return [str(console if console.is_file() else executable)]
    return [sys.executable, "-m", "superpybackup"]


def cmd_line(arguments: list[str], output_file: str) -> tuple[str, str]:
    """("cmd.exe", Parameter): fuehrt den Befehl aus und schreibt seine Ausgabe in output_file. /s entfernt nur die aeusseren Anfuehrungszeichen."""
    command = subprocess.list2cmdline(console_command() + arguments)
    comspec = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe")
    return comspec, f'/s /c "{command} > "{output_file}" 2>&1"'


def run_cli(arguments: list[str], *, verb: str = "runas", timeout_ms: int = 5 * 60 * 1000) -> tuple[int, str]:
    """Fuehrt `SuperPyBackupCmd <arguments>` als Administrator aus. Liefert (Rueckgabewert, Ausgabe)."""
    handle, output_file = tempfile.mkstemp(prefix="superpybackup-admin-", suffix=".txt")
    os.close(handle)
    try:
        program, parameters = cmd_line(arguments, output_file)
        code = shell_execute(program, parameters, verb=verb, timeout_ms=timeout_ms)
        try:
            raw = Path(output_file).read_bytes()
        except OSError:
            raw = b""
        for encoding in ("utf-8", "cp1252"):
            try:
                return code, raw.decode(encoding).strip()
            except UnicodeDecodeError:
                continue
        return code, raw.decode("latin-1").strip()
    finally:
        try:
            os.remove(output_file)
        except OSError:
            pass
