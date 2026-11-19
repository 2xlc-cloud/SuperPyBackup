"""Gesperrte Dateien sichern: Schattenkopie (Volume Shadow Copy, VSS) der Quelllaufwerke.

Dateien, die ein Programm exklusiv geoeffnet hat (Outlook-PSTs, Datenbanken, ...), lassen sich sonst nicht lesen. Eine Schattenkopie
ist ein eingefrorener, konsistenter Stand des Laufwerks; die Sicherung liest dann daraus statt vom Laufwerk.

* Das Anlegen einer Schattenkopie erfordert Administratorrechte (Prozess "als Administrator", z. B. Aufgabe mit hoechsten Rechten).
  backup.use_vss: "off" = nie | "auto" (Standard) = nutzen, wenn moeglich; sonst normal weitersichern (Protokollhinweis) |
  "on" = Schattenkopie ist Pflicht, sonst schlaegt die Sicherung fehl.
* Die Schattenkopie wird nach jedem Lauf wieder geloescht. Wird der Lauf hart beendet, merkt sich vss-pending.json die Kennung und
  der naechste Lauf raeumt sie auf (nur Kopien, die dieses Programm angelegt hat).
* Gesichert wird unter dem urspruenglichen Pfad: Manifest und Wiederherstellung kennen nur die echten Pfade.
* Ausgefuehrt wird PowerShell (CIM, Win32_ShadowCopy), versteckt; Werte gehen ueber Umgebungsvariablen, nie in die Befehlszeile.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Protocol

from . import config as cfgmod
from . import fsutil

CREATE_NO_WINDOW = 0x08000000
MODES = ("off", "auto", "on")


class ShadowError(Exception):
    pass


class Provider(Protocol):
    def create(self, volume: str) -> tuple[str, str]:
        """Legt eine Schattenkopie von `volume` (z. B. 'C:\\\\') an; liefert (Kennung, Geraetepfad ohne abschliessenden Trenner)."""

    def delete(self, shadow_id: str) -> None: ...


def is_admin() -> bool:
    """Laeuft der Prozess mit Administratorrechten (erhoeht)?"""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


_CREATE = r"""
$ErrorActionPreference = 'Stop'
$r = Invoke-CimMethod -ClassName Win32_ShadowCopy -MethodName Create -Arguments @{ Volume = $env:SPB_VOLUME; Context = 'ClientAccessible' }
if ($r.ReturnValue -ne 0) { throw "Win32_ShadowCopy.Create Rueckgabewert $($r.ReturnValue)" }
$id = $r.ShadowID
$s = Get-CimInstance -ClassName Win32_ShadowCopy | Where-Object { $_.ID -eq $id }
if (-not $s) { throw 'Schattenkopie nicht gefunden' }
Write-Output ("SPB|" + $id + "|" + $s.DeviceObject)
"""
_DELETE = r"""
$ErrorActionPreference = 'Stop'
Get-CimInstance -ClassName Win32_ShadowCopy | Where-Object { $_.ID -eq $env:SPB_ID } | Remove-CimInstance
"""


def _powershell(script: str, **env: str) -> str:
    import base64

    root = os.environ.get("SystemRoot", r"C:\Windows")
    exe = os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        result = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
                                capture_output=True, timeout=180, env=dict(os.environ, **env), creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as error:
        raise ShadowError(f"PowerShell nicht ausfuehrbar: {error}") from error
    out = (result.stdout or b"").decode("utf-8", "replace")
    if result.returncode != 0:
        detail = (result.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise ShadowError(detail[0][:300] if detail else f"PowerShell Rueckgabewert {result.returncode}")
    return out


class WindowsProvider:
    def create(self, volume: str) -> tuple[str, str]:
        out = _powershell(_CREATE, SPB_VOLUME=volume)
        for line in out.splitlines():
            if line.startswith("SPB|"):
                _, shadow_id, device = line.strip().split("|", 2)
                return shadow_id, device.rstrip("\\")
        raise ShadowError("Unerwartete Antwort beim Anlegen der Schattenkopie.")

    def delete(self, shadow_id: str) -> None:
        _powershell(_DELETE, SPB_ID=shadow_id)


def new_provider() -> Provider:
    return WindowsProvider()


def _pending_path() -> Path:
    return cfgmod.app_home() / "vss-pending.json"


def _pending() -> list[str]:
    try:
        data = json.loads(_pending_path().read_text(encoding="utf-8"))
        return [item for item in data if isinstance(item, str)] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save_pending(ids: list[str]) -> None:
    try:
        path = _pending_path()
        if ids:
            path.parent.mkdir(parents=True, exist_ok=True)
            fsutil.atomic_write_json(str(path), ids)
        elif path.exists():
            path.unlink()
    except OSError:
        pass


def volume_of(path: str) -> str | None:
    """'C:\\\\' fuer 'C:\\\\Users\\\\x'; None fuer Netzwerkpfade und Pfade ohne Laufwerksbuchstaben (dafuer gibt es keine Schattenkopie)."""
    drive, _ = os.path.splitdrive(os.path.abspath(path))
    if len(drive) == 2 and drive[1] == ":":
        return drive.upper() + "\\"
    return None


class ShadowSet:
    """Schattenkopien der Laufwerke einer Sicherung; `translate` ersetzt ein Original- durch den Pfad in der Schattenkopie."""

    def __init__(self, provider: Provider, log) -> None:
        self.provider = provider
        self.log = log
        self.devices: dict[str, str] = {}  # Laufwerk ('C:\\\\') -> Geraetepfad
        self.ids: dict[str, str] = {}

    def prepare(self, paths: list[str]) -> None:
        """Legt je betroffenem Laufwerk eine Schattenkopie an. ShadowError, wenn das fuer ein Laufwerk nicht gelingt."""
        for volume in sorted({v for v in (volume_of(p) for p in paths) if v}):
            if volume in self.devices:
                continue
            shadow_id, device = self.provider.create(volume)
            self.devices[volume] = device
            self.ids[volume] = shadow_id
            _save_pending(list(self.ids.values()) + [i for i in _pending() if i not in self.ids.values()])
            self.log.info(f"Schattenkopie fuer {volume} angelegt: gesperrte Dateien werden aus dem eingefrorenen Stand gelesen.")

    def translate(self, path: str) -> str:
        volume = volume_of(path)
        device = self.devices.get(volume) if volume else None
        if device is None:
            return path
        absolute = os.path.abspath(path)
        return device + absolute[2:]

    def close(self) -> None:
        remaining = _pending()
        for volume, shadow_id in list(self.ids.items()):
            try:
                self.provider.delete(shadow_id)
                remaining = [i for i in remaining if i != shadow_id]
                self.log.info(f"Schattenkopie fuer {volume} geloescht.")
            except ShadowError as error:
                self.log.warn(f"Schattenkopie fuer {volume} konnte nicht geloescht werden (wird beim naechsten Lauf erneut versucht): {error}")
        self.ids.clear()
        self.devices.clear()
        _save_pending(remaining)


def cleanup_pending(provider: Provider, log) -> None:
    """Loescht Schattenkopien, die ein frueherer, hart beendeter Lauf angelegt und nicht aufgeraeumt hat."""
    remaining = []
    for shadow_id in _pending():
        try:
            provider.delete(shadow_id)
            log.info("Uebriggebliebene Schattenkopie eines frueheren Laufs geloescht.")
        except ShadowError as error:
            log.warn(f"Uebriggebliebene Schattenkopie konnte nicht geloescht werden: {error}")
            remaining.append(shadow_id)
    _save_pending(remaining)


def open_for(mode: str, paths: list[str], log) -> ShadowSet | None:
    """Schattenkopien fuer die Quellpfade, soweit gewuenscht und moeglich. None = normal von den Laufwerken lesen.

    "auto": ohne Administratorrechte oder bei einem Fehler wird normal gesichert (mit Hinweis im Protokoll).
    "on": jeder Mangel ist ein Fehler (ShadowError).
    """
    if mode == "off":
        return None
    if not any(volume_of(p) for p in paths):
        return None
    if not is_admin():
        message = "Schattenkopie (VSS) nicht moeglich: der Lauf hat keine Administratorrechte; gesperrte Dateien koennen nicht gesichert werden."
        if mode == "on":
            raise ShadowError(message)
        log.info(message)
        return None
    provider = new_provider()
    cleanup_pending(provider, log)
    shadows = ShadowSet(provider, log)
    try:
        shadows.prepare(paths)
    except ShadowError as error:
        shadows.close()
        if mode == "on":
            raise ShadowError(f"Schattenkopie konnte nicht angelegt werden: {error}") from error
        log.warn(f"Schattenkopie nicht moeglich ({error}); es wird normal von den Laufwerken gesichert.")
        return None
    return shadows
