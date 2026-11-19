"""Update-Pruefung und -Installation ueber GitHub Releases.

Sicherheitsregeln
  * Pruefen laedt nie ein Programm herunter.
  * Installation nur nach ausdruecklicher Bestaetigung (Aufrufer uebergibt confirm) - mit einer Ausnahme, dem Pflichtupdate:
    Liegen mehr als `update.force_after_versions` (Standard 5) neuere Versionen vor, installieren die automatischen Pruefungen
    (nach einer Sicherung, geplante Updatepruefung, Oberflaeche) ohne Rueckfrage. Alle uebrigen Pruefungen unten gelten dafuer
    unveraendert; 0 schaltet das Pflichtupdate aus, ebenso `update.check_for_updates: false`.
  * Download-URLs muessen zum konfigurierten Repository gehoeren; Groesse und SHA-256 (SHA256SUMS.txt der Release) werden geprueft.
  * Ein heruntergeladenes Programm wird erst nach bestandener Pruefung ueberhaupt ausgefuehrt (nur `--version`).
  * Austausch mit Rollback: bei jedem Fehler bleibt die bisherige Installation unveraendert.
  * Signatur: Jede Release enthaelt SHA256SUMS.txt.sig, eine Ed25519-Signatur ueber SHA256SUMS.txt. Der oeffentliche Schluessel steht
    fest im Programm (RELEASE_PUBLIC_KEYS); der geheime Schluessel liegt nur beim Herausgeber, nie auf GitHub. Ohne gueltige
    Signatur wird nichts installiert (update.require_signature, Standard true). So genuegt ein manipuliertes Repository oder ein
    uebernommenes GitHub-Konto nicht, um Schadsoftware einzuspielen: die SHA-256-Summen allein koennten mit ausgetauscht werden.
  * Die SHA-256-Summen selbst schuetzen vor beschaedigten/unvollstaendigen Downloads.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import base64
import binascii

from . import __version__, config as cfgmod, ed25519, fsutil
from .runlog import RunLog

ASSETS = ("SuperPyBackup.exe", "SuperPyBackupCmd.exe")
SUMS_NAME = "SHA256SUMS.txt"
SIG_NAME = "SHA256SUMS.txt.sig"
# Oeffentliche Schluessel (hex) der Herausgeber-Signatur; mehrere erlauben einen Schluesselwechsel (alt und neu gleichzeitig).
RELEASE_PUBLIC_KEYS = ("252696795972f58b1a44187b3e01ebd6f6ae62157d52d04d68fa1fba062e1169",)
MAX_JSON = 2 * 1024 * 1024
MAX_BINARY = 200 * 1024 * 1024
LOOPBACK = ("127.0.0.1", "localhost", "[::1]")


class UpdateError(Exception):
    pass


DEFAULT_FORCE_AFTER = 5


@dataclass
class UpdateInfo:
    version: str
    tag: str
    assets: dict[str, dict[str, object]]
    page: str = ""
    behind: int = 1  # Anzahl veroeffentlichter Versionen, die neuer sind als die installierte
    forced: bool = False  # Pflichtupdate: mehr als force_after_versions Versionen zurueck


def parse_version(text: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", text.strip())
    if not match:
        raise UpdateError(f"Keine gueltige Versionsnummer (X.Y.Z): {text!r}")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def _check_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme == "https":
        return
    if parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1"):
        return  # nur fuer lokale Tests
    raise UpdateError(f"Nur HTTPS-Adressen sind erlaubt: {url}")


def default_fetch(url: str, limit: int) -> bytes:
    _check_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": "SuperPyBackup-Updater", "Accept": "application/vnd.github+json, application/octet-stream"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - Schema oben geprueft
            data = response.read(limit + 1)
    except OSError as error:
        raise UpdateError(f"Netzwerkfehler bei {url}: {error}") from error
    if len(data) > limit:
        raise UpdateError(f"Antwort zu gross: {url}")
    return data


def web_base(update_settings: dict) -> str:
    api = update_settings["api_url"].rstrip("/")
    return "https://github.com" if api == "https://api.github.com" else api


def latest_release(update_settings: dict, fetch: Callable[[str, int], bytes] = default_fetch) -> UpdateInfo:
    repo = update_settings["repository"]
    api = update_settings["api_url"].rstrip("/")
    raw = fetch(f"{api}/repos/{repo}/releases/latest", MAX_JSON)
    try:
        data = json.loads(raw.decode("utf-8"))
        tag = data["tag_name"]
        parse_version(tag)
        if data.get("draft") or data.get("prerelease"):
            raise UpdateError("Neueste Release ist ein Entwurf oder eine Vorabversion.")
        assets: dict[str, dict[str, object]] = {}
        for asset in data["assets"]:
            name, url, size = asset["name"], asset["browser_download_url"], asset["size"]
            if not isinstance(name, str) or not isinstance(url, str) or not isinstance(size, int):
                raise UpdateError("Ungueltige Asset-Angaben.")
            assets[name] = {"url": url, "size": size}
    except (ValueError, KeyError, TypeError) as error:
        raise UpdateError(f"Unerwartete Antwort der Update-Quelle: {error}") from error
    prefix = f"{web_base(update_settings)}/{repo}/releases/download/"
    for name, asset in assets.items():
        if not str(asset["url"]).startswith(prefix):
            raise UpdateError(f"Download-Adresse gehoert nicht zum Repository {repo}: {asset['url']}")
    return UpdateInfo(version=tag.lstrip("v"), tag=tag, assets=assets, page=str(data.get("html_url", "")))


def count_newer(update_settings: dict, current: str, fetch: Callable[[str, int], bytes] = default_fetch) -> int:
    """Anzahl der veroeffentlichten Versionen (ohne Entwuerfe/Vorabversionen), die neuer sind als `current`; mindestens 1."""
    repo = update_settings["repository"]
    api = update_settings["api_url"].rstrip("/")
    try:
        data = json.loads(fetch(f"{api}/repos/{repo}/releases?per_page=100", 8 * 1024 * 1024).decode("utf-8"))
        have = parse_version(current)
        versions = set()
        for release in data:
            if release.get("draft") or release.get("prerelease"):
                continue
            version = parse_version(str(release["tag_name"]))
            if version > have:
                versions.add(version)
        return max(1, len(versions))
    except (UpdateError, ValueError, KeyError, TypeError, AttributeError):
        return 1  # Liste nicht lesbar: die neueste Version ist jedenfalls eine neuere


def check_for_update(update_settings: dict, current: str = __version__, fetch: Callable[[str, int], bytes] = default_fetch) -> UpdateInfo | None:
    """Liefert die neuere Release oder None. Laedt nichts herunter. Setzt `behind` und `forced` (Pflichtupdate)."""
    info = latest_release(update_settings, fetch)
    if parse_version(info.version) <= parse_version(current):
        return None
    info.behind = count_newer(update_settings, current, fetch)
    limit = int(update_settings.get("force_after_versions", DEFAULT_FORCE_AFTER) or 0)
    info.forced = limit > 0 and info.behind > limit
    return info


def _cache_path() -> Path:
    return cfgmod.app_home() / "update-check.json"


def periodic_check(config: cfgmod.Config, log: RunLog, fetch: Callable[[str, int], bytes] = default_fetch) -> UpdateInfo | None:
    """Hoechstens alle check_interval_hours pruefen; Fehler werden nur protokolliert (nie den Backup-Lauf stoeren)."""
    settings = config.update
    if not settings["check_for_updates"]:
        return None
    cache = _cache_path()
    try:
        last = json.loads(cache.read_text(encoding="utf-8"))["checked_utc"]
        if datetime.now(timezone.utc) - datetime.fromisoformat(last) < timedelta(hours=settings["check_interval_hours"]):
            return None
    except (OSError, ValueError, KeyError):
        pass
    try:
        info = check_for_update(settings, fetch=fetch)
    except UpdateError as error:
        log.warn(f"Update-Pruefung nicht moeglich: {error}")
        return None
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        fsutil.atomic_write_json(str(cache), {"checked_utc": datetime.now(timezone.utc).isoformat(), "latest": info.version if info else __version__})
    except OSError:
        pass
    if info:
        log.info(f"Neue Version verfuegbar: {info.version} (installiert {__version__}). Installation mit 'SuperPyBackupCmd update install'.")
    return info


def _notified_path() -> Path:
    return cfgmod.app_home() / "update-notified.json"


def already_notified(version: str) -> bool:
    """Wurde fuer diese Version schon eine Desktop-Meldung gezeigt? (Damit die geplante Pruefung nicht taeglich dasselbe meldet.)"""
    try:
        return json.loads(_notified_path().read_text(encoding="utf-8")).get("version") == version
    except (OSError, ValueError):
        return False


def remember_notified(version: str) -> None:
    try:
        _notified_path().parent.mkdir(parents=True, exist_ok=True)
        fsutil.atomic_write_json(str(_notified_path()), {"version": version})
    except OSError:
        pass


def parse_sums(text: str) -> dict[str, str]:
    sums: dict[str, str] = {}
    for line in text.splitlines():
        match = re.fullmatch(r"([A-Fa-f0-9]{64}) [ *]?(.+)", line.strip())
        if match:
            sums[match.group(2).strip()] = match.group(1).lower()
    return sums


def verify_signature(sums: bytes, signature_text: str, keys: tuple[str, ...] | None = None) -> None:
    """Prueft die Ed25519-Signatur (Base64) ueber den exakten Inhalt von SHA256SUMS.txt; UpdateError, wenn kein Schluessel passt."""
    try:
        signature = base64.b64decode(signature_text.strip(), validate=True)
    except (binascii.Error, ValueError) as error:
        raise UpdateError("Die Signatur der Release ist nicht lesbar.") from error
    for key in (RELEASE_PUBLIC_KEYS if keys is None else keys):
        try:
            if ed25519.verify(bytes.fromhex(key), sums, signature):
                return
        except ValueError:
            continue
    raise UpdateError("Die Signatur der Release ist ungueltig: die Pruefsummen stammen nicht vom Herausgeber. Es wird nichts installiert.")


def _default_smoke_test(path: str, version: str) -> None:
    result = subprocess.run([path, "--version"], capture_output=True, timeout=60, creationflags=0x08000000)
    output = (result.stdout or b"").decode("utf-8", "replace")
    if result.returncode != 0 or version not in output:
        raise UpdateError(f"Das heruntergeladene Programm meldet nicht die erwartete Version {version}: {output.strip()!r}")


def install_update(
    info: UpdateInfo,
    install_dir: str,
    update_settings: dict,
    *,
    confirm: Callable[[UpdateInfo], bool],
    fetch: Callable[[str, int], bytes] = default_fetch,
    smoke_test: Callable[[str, str], None] = _default_smoke_test,
    log: RunLog | None = None,
) -> list[str]:
    """Laedt die Release nach Bestaetigung herunter, prueft sie und tauscht die Programme aus. Liefert die ersetzten Dateien."""
    if not confirm(info):
        raise UpdateError("Update nicht bestaetigt; es wurde nichts heruntergeladen.")
    names = [name for name in ASSETS if os.path.isfile(os.path.join(install_dir, name))]
    if not names:
        raise UpdateError(f"Im Installationsordner wurde kein Programm gefunden: {install_dir}")
    signed = bool(update_settings.get("require_signature", True))
    missing = [name for name in names + [SUMS_NAME] + ([SIG_NAME] if signed else []) if name not in info.assets]
    if missing:
        raise UpdateError(f"Die Release enthaelt nicht alle benoetigten Dateien: {', '.join(missing)}")
    work = os.path.join(install_dir, f".update-{fsutil.new_id(12)}")
    os.mkdir(work)
    replaced: list[str] = []
    try:
        sums_bytes = fetch(str(info.assets[SUMS_NAME]["url"]), 64 * 1024)
        if signed:
            verify_signature(sums_bytes, fetch(str(info.assets[SIG_NAME]["url"]), 4096).decode("ascii", "replace"))
        sums = parse_sums(sums_bytes.decode("utf-8", "replace"))
        for name in names:
            if name not in sums:
                raise UpdateError(f"Fuer {name} fehlt die SHA-256-Summe in {SUMS_NAME}.")
            data = fetch(str(info.assets[name]["url"]), MAX_BINARY)
            if len(data) != info.assets[name]["size"]:
                raise UpdateError(f"{name}: Groesse stimmt nicht (erwartet {info.assets[name]['size']}, erhalten {len(data)}); Download unvollstaendig?")
            if hashlib.sha256(data).hexdigest() != sums[name]:
                raise UpdateError(f"{name}: SHA-256 stimmt nicht; Datei ist beschaedigt oder manipuliert.")
            with open(os.path.join(work, name), "xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        for name in names:
            smoke_test(os.path.join(work, name), info.version)
        for name in names:
            current = os.path.join(install_dir, name)
            backup = current + ".old"
            if os.path.exists(backup):
                os.remove(backup)
            os.replace(current, backup)
            replaced.append(name)
            os.replace(os.path.join(work, name), current)
        if log:
            log.info(f"Update auf {info.version} installiert: {', '.join(names)}")
        return names
    except BaseException:
        for name in replaced:  # Rollback
            current = os.path.join(install_dir, name)
            backup = current + ".old"
            try:
                if os.path.exists(backup):
                    if os.path.exists(current):
                        os.remove(current)
                    os.replace(backup, current)
            except OSError:
                pass
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)


def installed_dir() -> str | None:
    """Ordner der installierten EXE; None beim Start aus dem Quellcode (dort gibt es nichts zu ersetzen)."""
    import sys

    return str(Path(sys.executable).parent) if getattr(sys, "frozen", False) else None


def run_forced_update(config: cfgmod.Config, info: UpdateInfo | None, log: RunLog | None, *, install_dir: str | None = None, fetch: Callable[[str, int], bytes] = default_fetch, smoke_test: Callable[[str, str], None] | None = None) -> list[str] | None:
    """Pflichtupdate: installiert ohne Rueckfrage, wenn `info.forced`. Liefert die ersetzten Dateien, None wenn nichts zu tun ist.

    Es gelten alle Sicherungen der normalen Installation (HTTPS/Repository, Groesse, SHA-256, Probelauf, Rollback).
    Bei einem Fehler bleibt die bisherige Installation unveraendert (UpdateError); der naechste Lauf versucht es erneut.
    """
    if info is None or not info.forced or not config.update["check_for_updates"]:
        return None
    folder = install_dir or installed_dir()
    if folder is None:
        if log:
            log.warn(f"Pflichtupdate auf {info.version} faellig ({info.behind} Versionen zurueck), aber nur in der installierten EXE-Version moeglich.")
        return None
    if log:
        log.warn(f"Pflichtupdate: Version {__version__} ist {info.behind} Versionen hinter {info.version} (Grenze: mehr als {config.update['force_after_versions']}); es wird ohne Rueckfrage installiert.")
    try:
        return install_update(info, folder, config.update, confirm=lambda update: True, fetch=fetch, smoke_test=smoke_test or _default_smoke_test, log=log)
    except UpdateError as error:
        if log:
            log.error(f"Pflichtupdate auf {info.version} fehlgeschlagen (bisherige Installation unveraendert): {error}")
        raise


def rollback(install_dir: str) -> list[str]:
    """Stellt die vor dem letzten Update gesicherten Programme (.old) wieder her."""
    restored = []
    for name in ASSETS:
        current = os.path.join(install_dir, name)
        backup = current + ".old"
        if os.path.isfile(backup):
            if os.path.exists(current):
                os.replace(current, current + ".bad")
            os.replace(backup, current)
            restored.append(name)
            try:
                os.remove(current + ".bad")
            except OSError:
                pass
    if not restored:
        raise UpdateError("Es gibt keine gesicherte Vorversion (.old).")
    return restored
