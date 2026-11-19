"""Mehrere Sicherungen (Profile) verwalten.

* "Standard" ist die Hauptkonfiguration (config.json) und bleibt unveraendert.
* Jede weitere Sicherung ist eine eigene Konfigurationsdatei <Ordner der Hauptkonfiguration>/jobs/<Name>.json
  mit eigenen Quellen, eigenem Ziel, eigener Aufbewahrung und eigenen Einstellungen.
* Der Sicherungsordner im Ziel traegt bei weiteren Sicherungen den Zusatz __<Name> (siehe Config.target_dir),
  so mischen sich die Staende verschiedener Sicherungen nie.
* Jede Sicherung kann eine eigene Windows-Aufgabe haben; zusaetzlich gibt es eine Aufgabe fuer alle Sicherungen.
* Entfernt wird nur die Konfiguration (und auf Wunsch die Aufgabe), nie bereits gesicherte Daten.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from . import config as cfgmod


class JobError(cfgmod.ConfigError):
    pass


@dataclass(frozen=True)
class Job:
    name: str  # Anzeigename ("Standard" oder der Name der weiteren Sicherung)
    path: Path
    standard: bool = False


def jobs_dir(base: str | Path) -> Path:
    return Path(base).parent / cfgmod.JOBS_FOLDER


def validate_name(name: str) -> str:
    name = (name or "").strip()
    if not cfgmod.JOB_NAME.match(name):
        raise JobError(f"Ungueltiger Name {name!r}: erlaubt sind Buchstaben, Ziffern, '.', '_' und '-' (hoechstens 40 Zeichen, nicht mit Sonderzeichen beginnend).")
    if name.lower() == cfgmod.STANDARD_JOB.lower():
        raise JobError(f"Der Name {cfgmod.STANDARD_JOB!r} ist fuer die Hauptsicherung reserviert.")
    return name


def is_standard(name: str | None) -> bool:
    return not name or name.lower() == cfgmod.STANDARD_JOB.lower()


def path_for(base: str | Path, name: str | None) -> Path:
    """Konfigurationspfad einer Sicherung (Standard = Hauptkonfiguration)."""
    if is_standard(name):
        return Path(base)
    return jobs_dir(base) / (validate_name(name) + ".json")


def list_jobs(base: str | Path) -> list[Job]:
    """Standard zuerst, danach alle Dateien im Ordner jobs (alphabetisch). Ungueltig benannte Dateien werden uebergangen."""
    found = [Job(cfgmod.STANDARD_JOB, Path(base), True)]
    folder = jobs_dir(base)
    try:
        files = sorted(folder.glob("*.json"), key=lambda p: p.name.lower())
    except OSError:
        files = []
    for file in files:
        if cfgmod.JOB_NAME.match(file.stem) and file.stem.lower() != cfgmod.STANDARD_JOB.lower():
            found.append(Job(file.stem, file))
    return found


def find(base: str | Path, name: str | None) -> Job:
    """Sicherung nach Namen (ohne Gross-/Kleinschreibung); JobError, wenn es sie nicht gibt."""
    for job in list_jobs(base):
        if job.name.lower() == (name or cfgmod.STANDARD_JOB).lower():
            return job
    raise JobError(f"Es gibt keine Sicherung mit dem Namen {name!r}. Vorhanden: " + ", ".join(job.name for job in list_jobs(base)))


def create(base: str | Path, name: str, copy_from: str | Path | None = None) -> Job:
    """Legt eine weitere Sicherung an: Kopie der Einstellungen von copy_from (z. B. der aktuellen Sicherung) oder Vorlage."""
    name = validate_name(name)
    if any(job.name.lower() == name.lower() for job in list_jobs(base)):
        raise JobError(f"Es gibt bereits eine Sicherung mit dem Namen {name!r}.")
    data = cfgmod.template("")
    if copy_from and Path(copy_from).is_file():
        try:
            with open(copy_from, "r", encoding="utf-8-sig") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError):
            pass
    path = path_for(base, name)
    try:
        cfgmod.save(path, data)
    except cfgmod.ConfigError:
        cfgmod.save(path, cfgmod.template(""))  # kopierte Datei war ungueltig: mit Vorlage beginnen
    except OSError as error:
        raise JobError(f"Die Sicherung konnte nicht angelegt werden: {error}") from error
    return Job(name, path)


def remove(base: str | Path, name: str) -> bool:
    """Entfernt nur die Konfigurationsdatei (und ihre .bak); gesicherte Daten im Ziel bleiben unberuehrt. Standard ist nicht entfernbar."""
    if is_standard(name):
        raise JobError("Die Standard-Sicherung kann nicht entfernt werden.")
    path = path_for(base, name)
    removed = False
    for file in (path, path.with_name(path.name + ".bak")):
        try:
            file.unlink()
            removed = removed or file == path
        except FileNotFoundError:
            pass
        except OSError as error:
            raise JobError(f"Die Sicherung konnte nicht entfernt werden: {error}") from error
    return removed


def task_name(base_task: str, name: str | None) -> str:
    """Aufgabenname einer Sicherung: Standard behaelt den bisherigen Namen, weitere heissen <Basis>-Job-<Name>."""
    return base_task if is_standard(name) else f"{base_task}-Job-{validate_name(name)}"


def all_task_name(base_task: str) -> str:
    """Aufgabenname fuer 'alle Sicherungen nacheinander'."""
    return base_task + "-Alle"
