"""Update-Pruefung und -Installation ueber GitHub Releases.

Sicherheitsregeln
  * Pruefen laedt nie ein Programm herunter.
  * Installation nur nach ausdruecklicher Bestaetigung (Aufrufer uebergibt confirm).
  * Download-URLs muessen zum konfigurierten Repository gehoeren; Groesse und SHA-256 (SHA256SUMS.txt der Release) werden geprueft.
  * Ein heruntergeladenes Programm wird erst nach bestandener Pruefung ueberhaupt ausgefuehrt (nur `--version`).
  * Austausch mit Rollback: bei jedem Fehler bleibt die bisherige Installation unveraendert.
  * Die SHA-256-Summen schuetzen vor beschaedigten/unvollstaendigen Downloads. Sie ersetzen keine Codesignatur
    und schuetzen nicht vor einem kompromittierten Repository.
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

from . import __version__, config as cfgmod, fsutil
from .runlog import RunLog

ASSETS = ("SuperPyBackup.exe", "SuperPyBackupCmd.exe")
SUMS_NAME = "SHA256SUMS.txt"
MAX_JSON = 2 * 1024 * 1024
MAX_BINARY = 200 * 1024 * 1024
LOOPBACK = ("127.0.0.1", "localhost", "[::1]")


class UpdateError(Exception):
    pass


@dataclass
class UpdateInfo:
    version: str
    tag: str
    assets: dict[str, dict[str, object]]
    page: str = ""


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


def check_for_update(update_settings: dict, current: str = __version__, fetch: Callable[[str, int], bytes] = default_fetch) -> UpdateInfo | None:
    """Liefert die neuere Release oder None. Laedt nichts herunter."""
    info = latest_release(update_settings, fetch)
    return info if parse_version(info.version) > parse_version(current) else None


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


def parse_sums(text: str) -> dict[str, str]:
    sums: dict[str, str] = {}
    for line in text.splitlines():
        match = re.fullmatch(r"([A-Fa-f0-9]{64}) [ *]?(.+)", line.strip())
        if match:
            sums[match.group(2).strip()] = match.group(1).lower()
    return sums


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
    missing = [name for name in names + [SUMS_NAME] if name not in info.assets]
    if missing:
        raise UpdateError(f"Die Release enthaelt nicht alle benoetigten Dateien: {', '.join(missing)}")
    work = os.path.join(install_dir, f".update-{fsutil.new_id(12)}")
    os.mkdir(work)
    replaced: list[str] = []
    try:
        sums = parse_sums(fetch(str(info.assets[SUMS_NAME]["url"]), 64 * 1024).decode("utf-8", "replace"))
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
