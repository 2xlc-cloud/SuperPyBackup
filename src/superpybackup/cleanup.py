"""Bereinigung des Sicherungsziels: Reste abgebrochener Laeufe, abgelaufene Staende, alte Protokolle, optional Teil-Staende.

Grundsaetze:
* Es wird nie ohne Vorschau und Bestaetigung geloescht (Bestaetigung liegt bei CLI/GUI; hier gibt es plan() und execute()).
* Der neueste vollstaendige Stand wird nie entfernt, defekte Staende (ohne gueltiges Manifest) nie automatisch; sie werden nur gemeldet.
* Geloescht wird nur unter derselben Sperre wie bei einer Sicherung (Mutex + Lockdatei); laeuft gerade eine Sicherung, passiert nichts.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime

from . import config as cfgmod, fsutil, manifest as mf, retention, snapshots, winapi
from .identity import Identity, get_identity
from .runlog import RunLog

STALE = "stale"
EXPIRED = "expired"
PARTIAL = "partial"
LOGS = "logs"
KIND_LABELS = {STALE: "Rest eines abgebrochenen Laufs", EXPIRED: "Abgelaufener Stand", PARTIAL: "Teil-Stand", LOGS: "Altes Protokoll"}


class CleanupError(Exception):
    pass


@dataclass
class Options:
    stale: bool = True
    expired: bool = True
    logs: bool = True
    partial: bool = False  # nur auf ausdruecklichen Wunsch


@dataclass
class Item:
    kind: str
    label: str
    path: str
    bytes: int | None = None
    files: int | None = None


@dataclass
class Plan:
    target: str
    items: list[Item] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(item.bytes or 0 for item in self.items)


@dataclass
class Result:
    removed: list[Item] = field(default_factory=list)
    failed: list[tuple[Item, str]] = field(default_factory=list)
    plan: Plan | None = None


def _log(config: cfgmod.Config, identity: Identity, target: str, echo=None, prefix: str = "") -> RunLog:
    return RunLog(config.log_dir, user=identity.sid, target=target, max_bytes=config.log_max_bytes, echo=echo, prefix=prefix)


def _target(config: cfgmod.Config, identity: Identity) -> str:
    root = config.backup_root
    if not root or not os.path.isabs(root):
        raise CleanupError("Sicherungsziel ist nicht als absoluter Pfad konfiguriert.")
    target = os.path.join(root, identity.folder)
    if not os.path.isdir(fsutil.fs(target)):
        raise CleanupError(f"Im Sicherungsziel gibt es keinen Ordner fuer diesen Benutzer: {target}")
    try:
        fsutil.assert_plain_directory(target)
    except OSError as error:
        raise CleanupError(str(error)) from error
    return target


def build_plan(config: cfgmod.Config, options: Options | None = None, *, identity: Identity | None = None, as_of: datetime | None = None) -> Plan:
    """Ermittelt, was entfernt wuerde. Veraendert nichts."""
    options = options or Options()
    identity = identity or get_identity()
    as_of = as_of or datetime.now()
    target = _target(config, identity)
    plan = Plan(target=target)
    if options.stale:
        for path in snapshots.leftover_work_dirs(target):
            plan.items.append(Item(STALE, os.path.basename(path), path))
    marked: set[str] = set()
    if options.expired:
        for ref in retention.expired_snapshots(target, identity.sid, as_of.date(), config.retention["daily"], config.retention["monthly"], config.retention["yearly"]):
            plan.items.append(Item(EXPIRED, ref.spec, ref.path, ref.manifest.total_bytes if ref.manifest else None, len(ref.manifest.files) if ref.manifest else None))
            marked.add(ref.path)
    if options.partial:
        for ref in snapshots.list_snapshots(target, identity.sid):
            if ref.manifest is not None and ref.manifest.status == mf.PARTIAL and ref.path not in marked:
                plan.items.append(Item(PARTIAL, ref.spec, ref.path, ref.manifest.total_bytes, len(ref.manifest.files)))
    for ref in snapshots.list_snapshots(target, identity.sid):
        if ref.manifest is None:
            plan.notes.append(f"{ref.spec} hat kein gueltiges Manifest (defekt) und wird nicht automatisch geloescht; bitte manuell pruefen.")
    if options.logs:
        for prefix in ("", "restore-"):
            log = RunLog(config.log_dir, prefix=prefix)
            for name in log.prune(config.log_keep_days, as_of.date(), dry_run=True):
                plan.items.append(Item(LOGS, name, os.path.join(config.log_dir, name)))
    return plan


def execute(config: cfgmod.Config, options: Options | None = None, *, identity: Identity | None = None, as_of: datetime | None = None, echo=None, cancel=None) -> Result:
    """Loescht, was build_plan ermittelt. Die Pruefung wird unter der Sicherungssperre frisch wiederholt."""
    options = options or Options()
    identity = identity or get_identity()
    as_of = as_of or datetime.now()
    target = _target(config, identity)
    mutex = winapi.NamedMutex("Local\\SuperPyBackup-" + identity.sid)
    if not mutex.try_acquire():
        raise CleanupError("Eine Sicherung laeuft gerade; Bereinigung abgebrochen.")
    lock = None
    try:
        from .engine import TargetLock  # spaeter Import: engine importiert die Bereinigung nicht

        lock = TargetLock(os.path.join(target, ".backup.lock"))
        if not lock.acquire():
            lock = None
            raise CleanupError("Das Sicherungsziel wird gerade von einem anderen Lauf verwendet; Bereinigung abgebrochen.")
        plan = build_plan(config, options, identity=identity, as_of=as_of)
        log = _log(config, identity, target, echo)
        log.info(f"Bereinigung gestartet: {len(plan.items)} Eintraege")
        result = Result(plan=plan)
        newest = snapshots.latest_complete(target, identity.sid)
        for item in plan.items:
            if cancel is not None and cancel.is_set():
                log.warn("Bereinigung abgebrochen.")
                break
            try:
                if item.kind in (EXPIRED, PARTIAL):
                    if newest is not None and os.path.normcase(item.path) == os.path.normcase(newest.path):
                        raise CleanupError("neuester vollstaendiger Stand wird nie entfernt")
                    ref = next((r for r in snapshots.list_snapshots(target, identity.sid) if r.path == item.path), None)
                    if ref is None:
                        raise CleanupError("Stand nicht mehr vorhanden")
                    fsutil.assert_plain_directory(ref.path)
                    if not retention.remove_snapshot(target, ref, log, "Stand" if item.kind == PARTIAL else "Abgelaufenen Stand"):
                        raise CleanupError("konnte nicht vollstaendig entfernt werden (siehe Protokoll)")
                elif item.kind == STALE:
                    fsutil.remove_tree_safe(item.path)
                    log.info(f"Rest entfernt: {item.label}")
                else:
                    os.remove(fsutil.fs(item.path))
                    log.info(f"Protokoll entfernt: {item.label}")
                result.removed.append(item)
            except (OSError, CleanupError) as error:
                result.failed.append((item, str(error)))
                log.warn(f"Bereinigung: {item.label} nicht entfernt: {error}")
        log.info(f"Bereinigung beendet: entfernt {len(result.removed)}, fehlgeschlagen {len(result.failed)}")
        return result
    finally:
        if lock is not None:
            lock.release()
        mutex.close()
