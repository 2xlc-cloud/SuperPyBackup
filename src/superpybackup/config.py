"""Zentrale JSON-Konfiguration: Laden, Validieren (mit verstaendlichen Fehlern), Standardwerte."""

from __future__ import annotations

import copy
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import REPOSITORY, winapi

ENV_CONFIG = "SUPERPYBACKUP_CONFIG"
ENV_HOME = "SUPERPYBACKUP_HOME"
NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
EXISTING_DAILY = ("skip", "new_snapshot")
CHANGE_DETECTION = ("metadata", "hash")
THEMES = ("dark", "light")
DEFAULT_KNOWN_FOLDERS = ["Desktop", "Documents", "Pictures", "Music", "Videos", "Downloads", "Favorites"]
# Standardmaessig nie gesichert: Temporaeres, Systemdateien, Papierkorb, Programm-Caches.
DEFAULT_EXCLUDES = ["*.tmp", "*.temp", "desktop.ini", "Thumbs.db", "~$*", "$RECYCLE.BIN", "System Volume Information"]


class ConfigError(ValueError):
    """Konfiguration ist ungueltig; die Meldung enthaelt alle gefundenen Probleme."""

    def __init__(self, message: str, problems: list[str] | None = None) -> None:
        super().__init__(message)
        self.problems = problems or []


def app_home() -> Path:
    """Verzeichnis fuer Konfiguration, Protokolle und Zustand - ausserhalb des Programmverzeichnisses."""
    override = os.environ.get(ENV_HOME)
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "SuperPyBackup"


JOBS_FOLDER = "jobs"  # weitere Sicherungen: <Ordner der Hauptkonfiguration>/jobs/<Name>.json
STANDARD_JOB = "Standard"  # Anzeigename der Hauptkonfiguration (config.json)
JOB_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")


def job_of(path: str | os.PathLike[str] | None) -> str:
    """Name der weiteren Sicherung zu einem Konfigurationspfad; leer fuer die Hauptkonfiguration (Standard)."""
    if not path:
        return ""
    p = Path(path)
    return p.stem if p.parent.name.lower() == JOBS_FOLDER and p.suffix.lower() == ".json" else ""


def default_config_path() -> Path:
    override = os.environ.get(ENV_CONFIG)
    return Path(override) if override else app_home() / "config.json"


DEFAULTS: dict[str, Any] = {
    "format": 1,
    "backup_root": "",
    "use_known_folders": True,
    "known_folders": list(DEFAULT_KNOWN_FOLDERS),
    "extra_sources": [],
    "exclude": [],
    "use_default_excludes": True,
    "retention": {"daily": 7, "monthly": 12, "yearly": 5},
    "backup": {
        "existing_daily": "skip",
        "change_detection": "metadata",
        "use_hardlinks": True,
        "full_every_days": 7,
        "free_space_margin_percent": 5,
        "min_free_bytes": 0,
        "warn_after_days": 3,
    },
    "logging": {"directory": "", "keep_days": 30, "max_file_mb": 20},
    "gui": {"theme": "dark", "close_to_tray": True},
    "notifications": {"enabled": False, "on_success": True, "on_update": True, "on_error": True},
    "update": {
        "check_for_updates": True,
        "check_interval_hours": 24,
        "force_after_versions": 5,
        "require_signature": True,
        "repository": REPOSITORY,
        "api_url": "https://api.github.com",
    },
}


@dataclass
class Source:
    name: str
    path: str
    origin: str = "extra"  # "known" oder "extra"


@dataclass
class Config:
    data: dict[str, Any]
    path: Path | None = None
    extras: list[Source] = field(default_factory=list)

    # --- bequeme Zugriffe
    @property
    def backup_root(self) -> str:
        return self.data["backup_root"]

    @property
    def job(self) -> str:
        """Name der weiteren Sicherung (aus dem Dateipfad), leer bei der Hauptkonfiguration."""
        return job_of(self.path)

    def target_dir(self, identity: Any) -> str:
        """Sicherungsordner dieses Benutzers im Ziel; weitere Sicherungen haben einen eigenen Ordner (<Benutzerordner>__<Name>)."""
        suffix = f"__{self.job}" if self.job else ""
        from . import netshare  # erst hier: netshare nutzt selbst die Konfiguration

        netshare.ensure_connected(self.backup_root)  # bei einer Netzwerkfreigabe mit hinterlegten Anmeldedaten zuerst anmelden
        return os.path.join(self.backup_root, identity.folder + suffix)

    @property
    def retention(self) -> dict[str, int]:
        return self.data["retention"]

    @property
    def backup(self) -> dict[str, Any]:
        return self.data["backup"]

    @property
    def notifications(self) -> dict[str, Any]:
        return self.data["notifications"]

    @property
    def update(self) -> dict[str, Any]:
        return self.data["update"]

    @property
    def log_dir(self) -> str:
        configured = self.data["logging"]["directory"]
        return configured or str(app_home() / "Logs")

    @property
    def log_keep_days(self) -> int:
        return self.data["logging"]["keep_days"]

    @property
    def log_max_bytes(self) -> int:
        return self.data["logging"]["max_file_mb"] * 1024 * 1024

    @property
    def excludes(self) -> list[str]:
        patterns = list(DEFAULT_EXCLUDES) if self.data["use_default_excludes"] else []
        return patterns + list(self.data["exclude"])


def _merge(defaults: dict[str, Any], given: dict[str, Any], where: str, problems: list[str]) -> dict[str, Any]:
    result = copy.deepcopy(defaults)
    for key, value in given.items():
        if key not in defaults:
            problems.append(f"Unbekannte Einstellung '{where}{key}' (Tippfehler?). Erlaubt: {', '.join(sorted(defaults))}")
            continue
        if isinstance(defaults[key], dict):
            if not isinstance(value, dict):
                problems.append(f"'{where}{key}' muss ein Objekt sein.")
                continue
            result[key] = _merge(defaults[key], value, f"{where}{key}.", problems)
        else:
            result[key] = value
    return result


def _int(value: Any, where: str, minimum: int, maximum: int, problems: list[str]) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        problems.append(f"'{where}' muss eine ganze Zahl von {minimum} bis {maximum} sein (gefunden: {value!r}).")


def validate(data: dict[str, Any], *, require_target: bool = True) -> list[Source]:
    """Prueft alle Werte; wirft ConfigError mit allen Problemen. Liefert die benutzerdefinierten Quellen."""
    problems: list[str] = []
    if data.get("format") != 1:
        problems.append(f"'format' muss 1 sein (gefunden: {data.get('format')!r}).")
    root = data["backup_root"]
    if not isinstance(root, str):
        problems.append("'backup_root' muss ein Text sein.")
    elif root.strip() == "":
        if require_target:
            problems.append("'backup_root' fehlt: Bitte das Sicherungsziel (z. B. \"E:\\\\Backups\" oder \"\\\\\\\\NAS\\\\Backup\") eintragen.")
    elif not os.path.isabs(root) or (os.path.splitdrive(root)[0] == "" ):
        problems.append(f"'backup_root' muss ein absoluter Pfad mit Laufwerk oder UNC-Freigabe sein (gefunden: {root!r}).")
    if not isinstance(data["use_known_folders"], bool):
        problems.append("'use_known_folders' muss true oder false sein.")
    known = data["known_folders"]
    if not isinstance(known, list) or not all(isinstance(item, str) for item in known):
        problems.append("'known_folders' muss eine Liste von Namen sein.")
    else:
        for item in known:
            if item not in winapi.KNOWN_FOLDER_IDS:
                problems.append(f"'known_folders': unbekannter Ordner {item!r}. Erlaubt: {', '.join(winapi.KNOWN_FOLDER_IDS)}")
        if len(set(known)) != len(known):
            problems.append("'known_folders' enthaelt Doppelte.")
    extras: list[Source] = []
    names: set[str] = set(known) if isinstance(known, list) and data["use_known_folders"] else set()
    if not isinstance(data["extra_sources"], list):
        problems.append("'extra_sources' muss eine Liste sein.")
    else:
        for index, item in enumerate(data["extra_sources"]):
            where = f"extra_sources[{index}]"
            if isinstance(item, str):
                item = {"path": item}
            if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not item["path"].strip():
                problems.append(f"'{where}' braucht einen Pfad (\"path\") und optional einen Namen (\"name\").")
                continue
            path = item["path"]
            if not os.path.isabs(path) or os.path.splitdrive(path)[0] == "":
                problems.append(f"'{where}.path' muss ein absoluter Pfad sein (gefunden: {path!r}).")
                continue
            name = item.get("name") or re.sub(r"[^A-Za-z0-9_-]", "_", os.path.basename(path.rstrip("\\/")) or "Quelle")
            if not name[0].isalpha():
                name = "Q_" + name
            if not NAME_PATTERN.match(name):
                problems.append(f"'{where}.name' darf nur Buchstaben, Ziffern, '_' und '-' enthalten und muss mit einem Buchstaben beginnen (gefunden: {name!r}).")
                continue
            if name in names:
                problems.append(f"'{where}': Der Name {name!r} wird bereits verwendet. Bitte einen eindeutigen \"name\" angeben.")
                continue
            names.add(name)
            extras.append(Source(name=name, path=path, origin="extra"))
    if not data["use_known_folders"] and not extras:
        problems.append("Es sind keine Quellen konfiguriert (use_known_folders=false und keine extra_sources).")
    exclude = data["exclude"]
    if not isinstance(exclude, list) or not all(isinstance(item, str) and item.strip() for item in exclude):
        problems.append("'exclude' muss eine Liste nicht leerer Muster (z. B. \"*.iso\", \"node_modules\") sein.")
    if not isinstance(data["use_default_excludes"], bool):
        problems.append("'use_default_excludes' muss true oder false sein.")
    retention = data["retention"]
    _int(retention["daily"], "retention.daily", 1, 36500, problems)
    _int(retention["monthly"], "retention.monthly", 1, 1200, problems)
    _int(retention["yearly"], "retention.yearly", 1, 100, problems)
    backup = data["backup"]
    if backup["existing_daily"] not in EXISTING_DAILY:
        problems.append(f"'backup.existing_daily' muss {' oder '.join(EXISTING_DAILY)} sein (gefunden: {backup['existing_daily']!r}).")
    if backup["change_detection"] not in CHANGE_DETECTION:
        problems.append(f"'backup.change_detection' muss {' oder '.join(CHANGE_DETECTION)} sein (gefunden: {backup['change_detection']!r}).")
    if not isinstance(backup["use_hardlinks"], bool):
        problems.append("'backup.use_hardlinks' muss true oder false sein.")
    _int(backup["full_every_days"], "backup.full_every_days", 0, 3650, problems)
    _int(backup["free_space_margin_percent"], "backup.free_space_margin_percent", 0, 100, problems)
    _int(backup["min_free_bytes"], "backup.min_free_bytes", 0, 1 << 50, problems)
    _int(backup["warn_after_days"], "backup.warn_after_days", 0, 3650, problems)
    logging_cfg = data["logging"]
    if not isinstance(logging_cfg["directory"], str) or (logging_cfg["directory"] and not os.path.isabs(logging_cfg["directory"])):
        problems.append("'logging.directory' muss leer (Standard) oder ein absoluter Pfad sein.")
    _int(logging_cfg["keep_days"], "logging.keep_days", 1, 3650, problems)
    _int(logging_cfg["max_file_mb"], "logging.max_file_mb", 1, 1024, problems)
    if not isinstance(data["gui"]["close_to_tray"], bool):
        problems.append("'gui.close_to_tray' muss true oder false sein.")
    if data["gui"]["theme"] not in THEMES:
        problems.append(f"'gui.theme' muss {' oder '.join(THEMES)} sein (gefunden: {data['gui']['theme']!r}).")
    for key, value in data["notifications"].items():
        if not isinstance(value, bool):
            problems.append(f"'notifications.{key}' muss true oder false sein.")
    update = data["update"]
    if not isinstance(update["check_for_updates"], bool):
        problems.append("'update.check_for_updates' muss true oder false sein.")
    _int(update["check_interval_hours"], "update.check_interval_hours", 1, 24 * 365, problems)
    _int(update["force_after_versions"], "update.force_after_versions", 0, 1000, problems)
    if not isinstance(update["require_signature"], bool):
        problems.append("'update.require_signature' muss true oder false sein.")
    if not isinstance(update["repository"], str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", update["repository"]):
        problems.append("'update.repository' muss die Form Benutzer/Repository haben.")
    if not isinstance(update["api_url"], str) or not re.fullmatch(r"https?://[A-Za-z0-9_.:-]+", update["api_url"]):
        problems.append("'update.api_url' muss eine URL wie https://api.github.com sein.")
    if problems:
        raise ConfigError("Ungueltige Konfiguration:\n  - " + "\n  - ".join(problems), problems)
    return extras


def from_dict(given: dict[str, Any], *, path: Path | None = None, require_target: bool = True) -> Config:
    problems: list[str] = []
    if not isinstance(given, dict):
        raise ConfigError("Ungueltige Konfiguration: Die Datei muss ein JSON-Objekt enthalten.")
    data = _merge(DEFAULTS, given, "", problems)
    extras: list[Source] = []
    try:
        extras = validate(data, require_target=require_target)
    except ConfigError as error:
        problems.extend(error.problems)
    if problems:  # unbekannte Schluessel und Wertfehler gemeinsam melden
        raise ConfigError("Ungueltige Konfiguration:\n  - " + "\n  - ".join(problems), problems)
    return Config(data=data, path=path, extras=extras)


def load(path: str | os.PathLike[str] | None = None, *, require_target: bool = True) -> Config:
    config_path = Path(path) if path else default_config_path()
    if not config_path.is_file():
        raise ConfigError(f"Konfigurationsdatei nicht gefunden: {config_path}\nMit 'SuperPyBackupCmd config init' eine Vorlage anlegen.")
    try:
        with open(config_path, "r", encoding="utf-8-sig") as handle:
            given = json.load(handle)
    except json.JSONDecodeError as error:
        raise ConfigError(f"Konfigurationsdatei ist kein gueltiges JSON ({config_path}, Zeile {error.lineno}, Spalte {error.colno}): {error.msg}") from error
    except OSError as error:
        raise ConfigError(f"Konfigurationsdatei nicht lesbar ({config_path}): {error}") from error
    job = job_of(config_path)
    if job and (not JOB_NAME.match(job) or job.lower() == STANDARD_JOB.lower()):
        raise ConfigError(f"Ungueltiger Name der Sicherung {job!r}: erlaubt sind Buchstaben, Ziffern, '.', '_' und '-' (hoechstens 40 Zeichen), und nicht {STANDARD_JOB!r}.")
    return from_dict(given, path=config_path, require_target=require_target)


def template(backup_root: str = "") -> dict[str, Any]:
    data = copy.deepcopy(DEFAULTS)
    data["backup_root"] = backup_root
    return data


def write_template(path: str | os.PathLike[str], backup_root: str = "") -> Path:
    """Legt eine Vorlage an. Eine vorhandene Datei wird nie ueberschrieben."""
    target = Path(path)
    if target.exists():
        raise ConfigError(f"Konfiguration existiert bereits und wird nicht ueberschrieben: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "x", encoding="utf-8", newline="\n") as handle:
        json.dump(template(backup_root), handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    return target


def save(path: str | os.PathLike[str], data: dict[str, Any]) -> Config:
    """Prueft und speichert die Konfiguration atomar; die bisherige Datei bleibt als config.json.bak erhalten."""
    config = from_dict(data, path=Path(path), require_target=False)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        import shutil

        shutil.copy2(target, target.with_name(target.name + ".bak"))
    temp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with open(temp, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(1, 16):  # kurze Sperren durch Virenscanner/Indexdienst abwarten
            try:
                os.replace(temp, target)
                break
            except PermissionError:
                if attempt == 15:
                    raise
                time.sleep(min(0.05 * attempt, 0.5))
    finally:
        if temp.exists():
            temp.unlink()
    return config


def resolve_sources(config: Config) -> tuple[list[Source], list[str]]:
    """Alle Quellen (Windows-Benutzerordner + eigene), Duplikate entfernt. Liefert (Quellen, Hinweise)."""
    sources: list[Source] = []
    notes: list[str] = []
    seen: dict[str, str] = {}

    def add(source: Source) -> None:
        key = os.path.normcase(os.path.abspath(source.path)).rstrip("\\/")
        if key in seen:
            notes.append(f"Quelle {source.name} ({source.path}) ist identisch mit {seen[key]} und wird nicht doppelt gesichert.")
            return
        seen[key] = source.name
        sources.append(source)

    if config.data["use_known_folders"]:
        for name in config.data["known_folders"]:
            add(Source(name=name, path=winapi.known_folder_path(name), origin="known"))
    for extra in config.extras:
        add(extra)
    return sources, notes
