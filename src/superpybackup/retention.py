"""Aufbewahrung: 7 Kalendertage / 12 Kalendermonate / 5 Kalenderjahre (konfigurierbar).

Es wird erst nach einem erfolgreichen neuen Stand bereinigt. Ein Ordner wird nur entfernt, wenn
sein Name einen gueltigen Stand ausweist; Defektes wird gemeldet, aber nie geloescht.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime

from . import fsutil, manifest as mf, snapshots
from .runlog import RunLog


@dataclass
class Windows:
    daily: date
    monthly: date
    yearly: date


def _add_months(value: date, months: int) -> date:
    index = value.year * 12 + (value.month - 1) + months
    return date(index // 12, index % 12 + 1, 1)


def cutoffs(as_of: date, daily: int, monthly: int, yearly: int) -> Windows:
    """Fruehestes noch aufzubewahrendes Datum je Stufe (Kalenderfenster inkl. heutigem Tag/Monat/Jahr)."""
    return Windows(
        daily=date.fromordinal(as_of.toordinal() - (daily - 1)),
        monthly=_add_months(date(as_of.year, as_of.month, 1), -(monthly - 1)),
        yearly=date(as_of.year - (yearly - 1), 1, 1),
    )


def _period_date(tier: str, period: str) -> date:
    if tier == "Daily":
        return datetime.strptime(period, "%Y-%m-%d").date()
    if tier == "Monthly":
        return datetime.strptime(period + "-01", "%Y-%m-%d").date()
    return date(int(period), 1, 1)


def expired_snapshots(target: str, sid: str, as_of: date, daily: int, monthly: int, yearly: int, log: RunLog | None = None) -> list[snapshots.SnapshotRef]:
    window = cutoffs(as_of, daily, monthly, yearly)
    limits = {"Daily": window.daily, "Monthly": window.monthly, "Yearly": window.yearly}
    refs = snapshots.list_snapshots(target, sid)
    # Der neueste vollstaendige Stand darf nie entfernt werden, auch nicht bei verstelltem Systemdatum.
    newest = snapshots.latest_complete(target, sid)
    expired: list[snapshots.SnapshotRef] = []
    for ref in refs:
        if ref.manifest is None:
            if log:
                log.warn(f"Aufbewahrung: Stand {ref.spec} hat kein gueltiges Manifest und bleibt unangetastet.")
            continue
        if ref.manifest.date[: len(ref.period)] != ref.period:
            if log:
                log.warn(f"Aufbewahrung: Ordnerdatum und Manifest von {ref.spec} stimmen nicht ueberein; Stand bleibt erhalten.")
            continue
        if newest is not None and ref.path == newest.path:
            continue
        if _period_date(ref.tier, ref.period) < limits[ref.tier]:
            expired.append(ref)
    return expired


def apply_retention(target: str, sid: str, as_of: date, daily: int, monthly: int, yearly: int, log: RunLog) -> list[str]:
    """Entfernt abgelaufene Staende. Reihenfolge: erst alles pruefen, dann loeschen. Liefert die entfernten Staende."""
    expired = expired_snapshots(target, sid, as_of, daily, monthly, yearly, log)
    for ref in expired:  # Vorab pruefen: kein Loeschen, wenn Verknuepfungen enthalten sind
        try:
            fsutil.assert_plain_directory(ref.path)
        except OSError as error:
            log.warn(f"Aufbewahrung uebersprungen fuer {ref.spec}: {error}")
            expired = [item for item in expired if item is not ref]
    removed: list[str] = []
    for ref in expired:
        hidden = os.path.join(target, snapshots.DELETE_PREFIX + fsutil.new_id(16))
        try:
            fsutil.rename_with_retry(ref.path, hidden)  # sofort kein gueltiger Stand mehr
        except OSError as error:
            log.warn(f"Aufbewahrung: {ref.spec} konnte nicht entfernt werden: {error}")
            continue
        try:
            fsutil.remove_tree_safe(hidden)
            removed.append(ref.spec)
            log.info(f"Abgelaufenen Stand entfernt: {ref.spec}")
        except OSError as error:
            log.warn(f"Aufbewahrung: Reste von {ref.spec} bleiben in {os.path.basename(hidden)} und werden beim naechsten Lauf bereinigt: {error}")
    return removed
