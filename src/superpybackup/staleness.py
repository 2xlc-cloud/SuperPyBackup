"""Warnung bei ausbleibender Sicherung.

Je Sicherung (Job) wird lokal im Programmordner (nicht im Sicherungsziel, das ja gerade fehlen kann) festgehalten, wann zuletzt eine
Sicherung erfolgreich war (vollstaendig oder "heute bereits gesichert"). Liegt das laenger als backup.warn_after_days Tage zurueck
(Standard 3, 0 = aus), erscheint eine Desktop-Meldung - hoechstens einmal pro Tag und Sicherung. Ohne bisherigen Erfolg zaehlt der
Zeitpunkt der ersten Pruefung. Alles hier ist fehlertolerant: nie eine Ausnahme nach aussen.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from . import config as cfgmod
from . import fsutil
from . import notify as notifymod

DEFAULT_WARN_DAYS = 3


def _path(config: cfgmod.Config) -> Path:
    job = re.sub(r"[^A-Za-z0-9_.-]", "_", config.job or cfgmod.STANDARD_JOB)
    return cfgmod.app_home() / f"last-success-{job}.json"


def _load(config: cfgmod.Config) -> dict:
    try:
        data = json.loads(_path(config).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(config: cfgmod.Config, data: dict) -> None:
    try:
        path = _path(config)
        path.parent.mkdir(parents=True, exist_ok=True)
        fsutil.atomic_write_json(str(path), data)
    except OSError:
        pass


def record_success(config: cfgmod.Config, when: datetime | None = None) -> None:
    data = _load(config)
    data["last_success"] = (when or datetime.now()).isoformat(timespec="seconds")
    data.pop("warned_on", None)
    _save(config, data)


def last_success(config: cfgmod.Config) -> datetime | None:
    try:
        return datetime.fromisoformat(_load(config)["last_success"])
    except (KeyError, ValueError, TypeError):
        return None


def warn_days(config: cfgmod.Config) -> int:
    try:
        return int(config.backup.get("warn_after_days", DEFAULT_WARN_DAYS))
    except (TypeError, ValueError):
        return DEFAULT_WARN_DAYS


def overdue_days(config: cfgmod.Config, now: datetime | None = None) -> int | None:
    """Anzahl voller Tage seit dem letzten Erfolg, wenn die Grenze ueberschritten ist; sonst None (auch wenn die Warnung aus ist)."""
    limit = warn_days(config)
    if limit <= 0:
        return None
    now = now or datetime.now()
    data = _load(config)
    reference = last_success(config)
    if reference is None:
        try:
            reference = datetime.fromisoformat(data["first_seen"])
        except (KeyError, ValueError, TypeError):
            data["first_seen"] = now.isoformat(timespec="seconds")
            _save(config, data)
            return None
    days = (now - reference).days
    return days if days >= limit else None


def check(config: cfgmod.Config, log=None, now: datetime | None = None) -> bool:
    """Meldet bei ueberschrittener Grenze (einmal pro Tag und Sicherung). True, wenn gewarnt wurde (auch wenn Meldungen aus sind)."""
    try:
        now = now or datetime.now()
        days = overdue_days(config, now)
        if days is None:
            return False
        data = _load(config)
        today = now.strftime("%Y-%m-%d")
        if data.get("warned_on") == today:
            return False
        data["warned_on"] = today
        _save(config, data)
        job = "" if not config.job or config.job.lower() == cfgmod.STANDARD_JOB.lower() else f" ({config.job})"
        last = last_success(config)
        since = f"seit {last:%d.%m.%Y}" if last else f"seit mindestens {days} Tagen"
        text = f"Keine erfolgreiche Sicherung {since} ({days} Tage). Ist der Rechner oder das Sicherungsziel erreichbar? Details im Protokoll."
        if log is not None:
            try:
                log.warn(f"Ausbleibende Sicherung{job}: {text}")
            except Exception:  # noqa: BLE001
                pass
        notifymod.notify(config.notifications, notifymod.ERROR, "Sicherung überfällig" + job, text, log)
        return True
    except Exception:  # noqa: BLE001 - eine Warnung darf nie einen Lauf oder die Oberflaeche stoeren
        return False
