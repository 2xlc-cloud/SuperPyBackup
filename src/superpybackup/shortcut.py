"""Verknuepfung auf dem Desktop (.lnk) anlegen und entfernen - nur auf ausdruecklichen Wunsch.

Die Verknuepfung wird ueber das Windows-Skripting-Objekt WScript.Shell in einem versteckten Windows PowerShell erzeugt
(Pfade werden per Umgebungsvariablen uebergeben, nichts wird in Befehlstext eingesetzt). Das Programmsymbol liegt als
SuperPyBackup.ico im Datenordner und bleibt damit auch nach einem Programmupdate gueltig.
Fuer Tests ersetzt SUPERPYBACKUP_DESKTOP den Desktopordner.
"""

from __future__ import annotations

import base64
import ctypes
import os
import subprocess
from pathlib import Path

from . import config as cfgmod, icon, scheduler

LINK_NAME = "SuperPyBackup.lnk"
CREATE_NO_WINDOW = 0x08000000
CSIDL_DESKTOPDIRECTORY = 0x10

_CREATE = r"""
$ErrorActionPreference = 'Stop'
$link = (New-Object -ComObject WScript.Shell).CreateShortcut($env:SPB_LNK)
$link.TargetPath = $env:SPB_TARGET
$link.Arguments = $env:SPB_ARGS
$link.WorkingDirectory = $env:SPB_WORKDIR
$link.IconLocation = $env:SPB_ICON
$link.Description = 'SuperPyBackup - versionierte Sicherung der Benutzerordner'
$link.Save()
"""
_READ = r"""
$ErrorActionPreference = 'Stop'
$link = (New-Object -ComObject WScript.Shell).CreateShortcut($env:SPB_LNK)
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$link.TargetPath
$link.Arguments
$link.IconLocation
"""


class ShortcutError(Exception):
    pass


def desktop_dir() -> Path:
    override = os.environ.get("SUPERPYBACKUP_DESKTOP")
    if override:
        return Path(override)
    buffer = ctypes.create_unicode_buffer(520)
    if ctypes.windll.shell32.SHGetFolderPathW(None, CSIDL_DESKTOPDIRECTORY, None, 0, buffer) != 0:
        return Path(os.path.expanduser("~")) / "Desktop"
    return Path(buffer.value)


def link_path() -> Path:
    return desktop_dir() / LINK_NAME


def exists() -> bool:
    return link_path().is_file()


def _powershell(script: str, variables: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    executable = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    try:
        return subprocess.run([executable, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
                              capture_output=True, timeout=60, env=dict(os.environ, **variables), creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as error:
        raise ShortcutError(f"PowerShell konnte nicht gestartet werden: {error}") from error


def create(config_path: str | None) -> Path:
    """Legt die Verknuepfung an oder aktualisiert sie (idempotent). Liefert ihren Pfad."""
    program, arguments = scheduler.program_command(config_path, "gui")
    folder = desktop_dir()
    if not folder.is_dir():
        raise ShortcutError(f"Desktopordner nicht gefunden: {folder}")
    icon_file = cfgmod.app_home() / "SuperPyBackup.ico"
    try:
        icon_file.parent.mkdir(parents=True, exist_ok=True)
        icon_file.write_bytes(icon.ico_bytes())
    except OSError as error:
        raise ShortcutError(f"Programmsymbol konnte nicht geschrieben werden: {error}") from error
    target = link_path()
    result = _powershell(_CREATE, {"SPB_LNK": str(target), "SPB_TARGET": program, "SPB_ARGS": arguments, "SPB_WORKDIR": str(Path(program).parent), "SPB_ICON": f"{icon_file},0"})
    if result.returncode != 0 or not target.is_file():
        raise ShortcutError("Die Verknüpfung konnte nicht angelegt werden: " + (result.stderr or result.stdout).decode("utf-8", "replace").strip()[:300])
    return target


def remove() -> bool:
    """Entfernt die Verknuepfung. True, wenn sie vorhanden war. Andere Dateien auf dem Desktop bleiben unberuehrt."""
    target = link_path()
    if not target.is_file():
        return False
    try:
        target.unlink()
    except OSError as error:
        raise ShortcutError(f"Die Verknüpfung konnte nicht entfernt werden: {error}") from error
    return True


def read() -> tuple[str, str, str] | None:
    """(Ziel, Argumente, Symbol) der vorhandenen Verknuepfung, sonst None."""
    if not exists():
        return None
    result = _powershell(_READ, {"SPB_LNK": str(link_path())})
    if result.returncode != 0:
        return None
    lines = result.stdout.decode("utf-8", "replace").splitlines()
    lines += [""] * (3 - len(lines))
    return lines[0].strip(), lines[1].strip(), lines[2].strip()
