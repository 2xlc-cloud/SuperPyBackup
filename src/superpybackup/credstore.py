"""Anmeldedaten fuer Netzwerkfreigaben (UNC-Sicherungsziele), sicher im Programmordner abgelegt.

* Das Kennwort wird mit der Windows-Datenschutzschnittstelle (DPAPI) fuer den angemeldeten Benutzer verschluesselt
  (CryptProtectData); nur dieser Benutzer auf diesem Rechner kann es wieder entschluesseln, auch ein Kopieren der Datei hilft nicht.
* Die Datei credentials.json liegt im Programmordner (%LOCALAPPDATA%\\SuperPyBackup) und enthaelt je Freigabe nur Benutzername
  und das verschluesselte Kennwort. Sie gehoert nie ins Repository und nie in die config.json.
* Kennwoerter erscheinen weder in Protokollen noch in Kommandozeilen oder Aufgaben.
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import time
from ctypes import wintypes
from pathlib import Path

from . import config as cfgmod, winapi

FILE_NAME = "credentials.json"
ENTROPY = b"SuperPyBackup.credentials.v1"
FORMAT = 1


class CredentialError(Exception):
    pass


def store_path() -> Path:
    return cfgmod.app_home() / FILE_NAME


def share_key(path: str | None) -> str | None:
    r"""Schluessel einer Freigabe (\\server\freigabe, kleingeschrieben) aus einem UNC-Pfad; None bei lokalen Pfaden."""
    if not path:
        return None
    drive = os.path.splitdrive(os.path.normpath(path))[0]
    if not drive.startswith("\\\\"):
        return None
    parts = [part for part in drive[2:].split("\\") if part]
    if len(parts) < 2 or parts[0] in (".", "?"):
        return None
    return "\\\\" + parts[0].lower() + "\\" + parts[1].lower()


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> tuple[_Blob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def _crypt(data: bytes, protect: bool) -> bytes:
    if not winapi.IS_WINDOWS:
        raise CredentialError("Die verschluesselte Ablage der Anmeldedaten gibt es nur unter Windows.")
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    source, keep_source = _blob(data)
    entropy, keep_entropy = _blob(ENTROPY)
    result = _Blob()
    flags = 0x1  # CRYPTPROTECT_UI_FORBIDDEN: nie ein Dialogfenster
    if protect:
        ok = crypt32.CryptProtectData(ctypes.byref(source), "SuperPyBackup", ctypes.byref(entropy), None, None, flags, ctypes.byref(result))
    else:
        ok = crypt32.CryptUnprotectData(ctypes.byref(source), None, ctypes.byref(entropy), None, None, flags, ctypes.byref(result))
    if not ok:
        raise CredentialError("Die Anmeldedaten konnten nicht " + ("verschluesselt" if protect else "entschluesselt") + f" werden (Windows-Fehler {ctypes.GetLastError()}). Sie sind nur fuer den Benutzer lesbar, der sie gespeichert hat.")
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(result.pbData, ctypes.c_void_p))
        del keep_source, keep_entropy


def encrypt(secret: str) -> str:
    return base64.b64encode(_crypt(secret.encode("utf-8"), True)).decode("ascii")


def decrypt(token: str) -> str:
    try:
        return _crypt(base64.b64decode(token.encode("ascii"), validate=True), False).decode("utf-8")
    except (ValueError, UnicodeError) as error:
        raise CredentialError("Der gespeicherte Kennwortwert ist beschaedigt.") from error


def _read() -> dict:
    path = store_path()
    if not path.is_file():
        return {"format": FORMAT, "entries": {}}
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        raise CredentialError(f"Die Datei mit den Anmeldedaten ist nicht lesbar ({path}): {error}") from error
    if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
        raise CredentialError(f"Die Datei mit den Anmeldedaten hat ein unbekanntes Format: {path}")
    return data


def _write(data: dict) -> None:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(temp, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(1, 11):
            try:
                os.replace(temp, path)
                break
            except PermissionError:
                if attempt == 10:
                    raise
                time.sleep(0.05 * attempt)
    finally:
        if temp.exists():
            temp.unlink()


def _need_key(share: str) -> str:
    key = share_key(share)
    if key is None:
        raise CredentialError(f"{share!r} ist keine Netzwerkfreigabe (erwartet: \\\\Server\\Freigabe oder ein Pfad darauf).")
    return key


def save(share: str, user: str, password: str) -> str:
    """Speichert Benutzer und (verschluesseltes) Kennwort fuer eine Freigabe. Liefert den Schluessel der Freigabe."""
    key = _need_key(share)
    user = (user or "").strip()
    if not user:
        raise CredentialError("Der Benutzername darf nicht leer sein.")
    if password == "":
        raise CredentialError("Das Kennwort darf nicht leer sein.")
    data = _read()
    data["entries"][key] = {"user": user, "password": encrypt(password)}
    data["format"] = FORMAT
    _write(data)
    return key


def load(share: str | None) -> tuple[str, str] | None:
    """(Benutzer, Kennwort) fuer die Freigabe des Pfads, None wenn keine hinterlegt sind (oder der Pfad lokal ist)."""
    key = share_key(share)
    if key is None:
        return None
    entry = _read()["entries"].get(key)
    if not isinstance(entry, dict) or not entry.get("user") or not entry.get("password"):
        return None
    return str(entry["user"]), decrypt(str(entry["password"]))


def user_of(share: str | None) -> str | None:
    """Nur der Benutzername (ohne das Kennwort zu entschluesseln), fuer Anzeigen."""
    key = share_key(share)
    if key is None:
        return None
    entry = _read()["entries"].get(key)
    return str(entry["user"]) if isinstance(entry, dict) and entry.get("user") else None


def remove(share: str) -> bool:
    key = _need_key(share)
    data = _read()
    if key not in data["entries"]:
        return False
    del data["entries"][key]
    _write(data)
    return True


def list_entries() -> list[tuple[str, str]]:
    """[(Freigabe, Benutzer)] ohne Kennwoerter."""
    return sorted((key, str(entry.get("user", ""))) for key, entry in _read()["entries"].items() if isinstance(entry, dict))
