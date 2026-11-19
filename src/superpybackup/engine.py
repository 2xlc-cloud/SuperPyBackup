"""Backup-Engine: transaktionssichere Sicherung (Vollsicherung oder Inkrement) mit Hardlinks.

Ablauf eines Laufs
  1. Vorabpruefungen (Ziel, Quellen, Lock, Schreibprobe)
  2. Quellen scannen (nur Metadaten), Aenderungen gegen den letzten vollstaendigen Stand bestimmen
  3. Speicherplatz pruefen
  4. In ein verstecktes Arbeitsverzeichnis (.incomplete-<id>) sichern; unveraenderte Dateien per Hardlink
  5. Manifest atomar schreiben und das Arbeitsverzeichnis mit einem einzigen Umbenennen freigeben
  6. Nur bei vollstaendigem Erfolg: Monats-/Jahresstand anlegen, danach Aufbewahrung bereinigen
Bereits freigegebene Staende werden von einem neuen Lauf nie veraendert.
"""

from __future__ import annotations

import errno
import fnmatch
import msvcrt
import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from . import netshare, __version__, config as cfgmod, fsutil, manifest as mf, retention, snapshots, staleness, vss, winapi
from .identity import Identity, get_identity
from .runlog import RunLog

COMPLETE = "Complete"
PARTIAL = "Partial"
ALREADY = "AlreadyComplete"
BUSY = "Busy"
FAILED = "Failed"
FULL = "Full"  # eigenstaendiger Stand: alle Dateien neu kopiert, keine Hardlinks zum Vorgaenger
INCREMENTAL = "Incremental"  # nur Geaendertes wird kopiert, Unveraendertes per Hardlink uebernommen
EXIT_CODES = {COMPLETE: 0, ALREADY: 0, BUSY: 0, PARTIAL: 2, FAILED: 1}


class BackupError(Exception):
    """Fehler, der den gesamten Lauf beendet (nicht nur eine Datei)."""


@dataclass
class Stats:
    scanned: int = 0
    new: int = 0
    modified: int = 0
    unchanged: int = 0
    deleted: int = 0  # seit dem letzten Stand verschwundene Quelldateien
    skipped: int = 0
    errors: int = 0
    linked: int = 0
    bytes_transferred: int = 0
    total_bytes: int = 0
    extra_storage_bytes: int = 0
    retention_removed: list[str] = field(default_factory=list)


@dataclass
class RunResult:
    status: str
    target: str = ""
    snapshot_path: str | None = None
    message: str = ""
    seconds: float = 0.0
    stats: Stats = field(default_factory=Stats)
    log_path: str = ""
    kind: str = ""  # FULL oder INCREMENTAL (leer, wenn kein Stand angelegt wurde)

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.status]


@dataclass
class FileInfo:
    key: str  # Kategorie/relativ, '/'-getrennt
    path: str
    size: int
    mtime_ns: int
    attrs: int


class TargetLock:
    """Exklusive Sperre ueber eine Lockdatei im Sicherungsziel (wirkt auch ueber Rechner/Sitzungen hinweg)."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._handle = None

    def acquire(self) -> bool:
        if os.path.lexists(fsutil.fs(self.path)) and (fsutil.is_link_or_junction(self.path) or fsutil.is_reparse(self.path)):
            raise BackupError("Lockdatei ist eine Verknuepfung.")
        handle = open(fsutil.fs(self.path), "a+b")
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is not None:
            try:
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            self._handle.close()
            self._handle = None


def build_excluder(patterns: list[str]):
    compiled = [pattern.replace("\\", "/").lower() for pattern in patterns]

    def excluded(name: str, relative: str) -> bool:
        lowered_name = name.lower()
        lowered_rel = relative.lower()
        return any(fnmatch.fnmatchcase(lowered_name, pattern) or fnmatch.fnmatchcase(lowered_rel, pattern) for pattern in compiled)

    return excluded


def scan_sources(sources: list[cfgmod.Source], excluded, log: RunLog, stats: Stats, cancel=None, translate=None) -> tuple[list[FileInfo], list[str], list[dict[str, str]], list[dict[str, str]]]:
    """Liest nur Metadaten. Verknuepfungen, Online-only-Platzhalter, System-/Temp-Dateien werden ausgelassen.

    translate: ersetzt einen Originalpfad durch den Pfad in der Schattenkopie (VSS); Kategorien und Manifest behalten die Originalpfade.
    """
    translate = translate or (lambda path: path)
    files: list[FileInfo] = []
    directories: list[str] = []
    categories: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    roots = [(source, os.path.abspath(source.path)) for source in sources]
    for source, root in roots:
        categories.append({"name": source.name, "source": root})
        # Ist eine andere Quelle in dieser enthalten (z. B. Bilder in Dokumenten), wird sie hier nicht doppelt gesichert.
        nested = {fsutil.norm_key(translate(other)) for _, other in roots if other != root and fsutil.norm_key(other).startswith(fsutil.norm_key(root) + os.sep)}
        stack: list[tuple[str, str]] = [(translate(root), "")]
        while stack:
            _check_cancel(cancel)
            folder, relative_dir = stack.pop()
            try:
                with os.scandir(fsutil.fs(folder)) as iterator:
                    entries = sorted(iterator, key=lambda item: item.name)
            except OSError as error:
                stats.errors += 1
                errors.append({"path": folder, "error": str(error)})
                log.error(f"Ordner nicht lesbar: {folder}: {error}")
                continue
            for entry in entries:
                name = entry.name
                relative = f"{relative_dir}/{name}" if relative_dir else name
                full = os.path.join(folder, name)
                try:
                    info = entry.stat(follow_symlinks=False)
                    is_dir = entry.is_dir(follow_symlinks=False)
                    is_link = entry.is_symlink() or (hasattr(entry, "is_junction") and entry.is_junction())
                except OSError as error:
                    stats.errors += 1
                    errors.append({"path": full, "error": str(error)})
                    log.error(f"Eintrag nicht lesbar: {full}: {error}")
                    continue
                attrs = getattr(info, "st_file_attributes", 0)
                if is_link:
                    stats.skipped += 1
                    log.info(f"Verknuepfung ausgelassen (Junction/Symlink): {full}")
                    continue
                if attrs & (winapi.ATTR_SYSTEM | winapi.ATTR_TEMPORARY):
                    stats.skipped += 1
                    continue
                if excluded(name, relative):
                    stats.skipped += 1
                    continue
                if attrs & winapi.ATTR_ONLINE_ONLY:
                    stats.skipped += 1
                    log.info(f"Nur online vorhanden (OneDrive), nicht heruntergeladen: {full}")
                    continue
                if is_dir:
                    if fsutil.norm_key(full) in nested:
                        continue
                    directories.append(f"{source.name}/{relative}")
                    stack.append((full, relative))
                else:
                    files.append(FileInfo(f"{source.name}/{relative}", full, info.st_size, info.st_mtime_ns, attrs))
    stats.scanned = len(files)
    return files, directories, categories, errors


def _create_target_folder(root: str, log: RunLog) -> None:
    """Legt den Zielordner an, aber nur eine Ebene tief: Existiert der uebergeordnete Ordner nicht (Laufwerk/Freigabe fehlt,
    Tippfehler), bleibt es beim Fehler, damit nie versehentlich ein Pfad auf dem falschen Datentraeger entsteht."""
    parent = os.path.dirname(root.rstrip("\\/"))
    if not parent or parent == root or not os.path.isdir(fsutil.fs(parent)):
        return
    try:
        os.mkdir(fsutil.fs(root))
        log.info(f"Sicherungsziel angelegt: {root}")
    except OSError as error:
        log.warn(f"Sicherungsziel konnte nicht angelegt werden: {root}: {error}")


def _stage_cleanup(target: str, log: RunLog) -> None:
    for path in snapshots.leftover_work_dirs(target):
        try:
            fsutil.remove_tree_safe(path)
            log.warn(f"Rest eines frueheren, abgebrochenen Laufs entfernt: {os.path.basename(path)}")
        except OSError as error:
            log.warn(f"Rest eines frueheren Laufs konnte nicht entfernt werden ({os.path.basename(path)}): {error}")


def _check_space(target: str, needed: int, config: cfgmod.Config, log: RunLog) -> None:
    margin = int(needed * config.backup["free_space_margin_percent"] / 100)
    required = needed + margin + config.backup["min_free_bytes"]
    free = fsutil.disk_free(target)
    log.info(f"Speicherplatz: benoetigt ca. {needed} Bytes (inkl. Reserve {required}), frei {free}")
    if free < required:
        raise BackupError(f"Nicht genug freier Speicherplatz im Sicherungsziel: frei {free} Bytes, benoetigt {required} Bytes.")


def ensure_archive(target: str, tier: str, name: str, source_dir: str, manifest: mf.Manifest, log: RunLog, stats: Stats) -> str | None:
    """Legt Monats-/Jahresstand aus einem vollstaendigen Tagesstand an (Hardlinks, sonst Kopien). Vorhandene bleiben unveraendert."""
    destination = os.path.join(target, tier, name)
    if os.path.lexists(fsutil.fs(destination)):
        existing = mf.load(destination, sid=manifest.user_sid)
        if existing is None or existing.date[: len(name)] != name:
            log.warn(f"Vorhandener {tier}-Stand {name} ist ungueltig und bleibt unveraendert; es wird kein neuer angelegt.")
        return None
    stage = os.path.join(target, snapshots.STAGE_PREFIX + fsutil.new_id(16))
    os.mkdir(fsutil.fs(stage))
    try:
        for directory in manifest.directories:
            os.makedirs(fsutil.fs(fsutil.safe_join(stage, directory)), exist_ok=True)
        for category in manifest.categories:
            os.makedirs(fsutil.fs(os.path.join(stage, category["name"])), exist_ok=True)
        for entry in manifest.files:
            source = fsutil.safe_join(source_dir, entry.path)
            destination_file = fsutil.safe_join(stage, entry.path)
            os.makedirs(fsutil.fs(os.path.dirname(destination_file)), exist_ok=True)
            try:
                os.link(fsutil.fs(source), fsutil.fs(destination_file))
            except OSError:
                if os.path.lexists(fsutil.fs(destination_file)):
                    raise
                size, digest, mtime_ns = fsutil.copy_file_hashed(source, destination_file)
                if digest != entry.sha256:
                    raise BackupError(f"Archivquelle wurde veraendert: {source}")
                fsutil.set_mtime(destination_file, entry.mtime_ns)
                fsutil.make_readonly(destination_file)
                stats.extra_storage_bytes += size
        mf.save(stage, manifest)
        fsutil.rename_with_retry(stage, destination)
        log.info(f"Archiv angelegt: {tier}/{name}")
        return destination
    finally:
        if os.path.lexists(fsutil.fs(stage)):
            try:
                fsutil.remove_tree_safe(stage)
            except OSError as error:
                log.warn(f"Arbeitsordner konnte nicht entfernt werden: {stage}: {error}")


def promote_archives(target: str, sid: str, today: date, log: RunLog, stats: Stats) -> list[str]:
    """Legt Monats- und Jahresstaende erst an, wenn der Monat bzw. das Jahr vorbei ist (nie schon beim ersten Stand).

    * Monatsstand: der letzte vollstaendige Tagesstand eines abgeschlossenen Monats (Zustand am Monatsende), falls noch vorhanden.
    * Jahresstand: der letzte Monatsstand eines abgeschlossenen Jahres.
    Vorhandene Staende bleiben unveraendert. Muss vor der Aufbewahrung laufen, solange die Tagesstaende noch da sind.
    """
    this_month, this_year = f"{today:%Y-%m}", f"{today:%Y}"
    created: list[str] = []
    latest: dict[str, snapshots.SnapshotRef] = {}
    for ref in snapshots.list_snapshots(target, sid, ("Daily",)):
        if ref.manifest is None or ref.manifest.status != mf.COMPLETE:
            continue
        month = ref.manifest.date[:7]
        if month < this_month and (month not in latest or (ref.manifest.date, ref.name) > (latest[month].manifest.date, latest[month].name)):
            latest[month] = ref
    for month, ref in sorted(latest.items()):
        if ensure_archive(target, "Monthly", month, ref.path, ref.manifest, log, stats):
            created.append(f"Monthly/{month}")
    newest: dict[str, snapshots.SnapshotRef] = {}
    for ref in snapshots.list_snapshots(target, sid, ("Monthly",)):
        if ref.manifest is None or ref.manifest.status != mf.COMPLETE:
            continue
        year = ref.name[:4]
        if year < this_year and (year not in newest or ref.name > newest[year].name):
            newest[year] = ref
    for year, ref in sorted(newest.items()):
        if ensure_archive(target, "Yearly", year, ref.path, ref.manifest, log, stats):
            created.append(f"Yearly/{year}")
    return created


def decide_kind(requested: str | None, base: snapshots.SnapshotRef | None, last_full: snapshots.SnapshotRef | None, day: str, full_every_days: int, use_links: bool) -> tuple[str, str]:
    """Bestimmt die Art des Laufs und nennt den Grund. requested: FULL, INCREMENTAL oder None (automatisch)."""
    if requested == FULL:
        return FULL, "Vollsicherung angefordert"
    if base is None:
        return FULL, "kein Vorgaengerstand vorhanden"
    if not use_links:
        return FULL, "ohne Hardlinks wird immer vollstaendig kopiert"
    if requested == INCREMENTAL:
        return INCREMENTAL, "Inkrement angefordert"
    if full_every_days > 0:
        if last_full is None:
            return FULL, "noch keine Vollsicherung vorhanden"
        age = (date.fromisoformat(day) - date.fromisoformat(last_full.name[:10])).days
        if age >= full_every_days:
            return FULL, f"letzte Vollsicherung vor {age} Tagen (Einstellung: alle {full_every_days} Tage)"
    return INCREMENTAL, "Vollsicherung noch nicht faellig" if full_every_days > 0 else "automatische Vollsicherung ist aus"


def _check_cancel(cancel) -> None:
    if cancel is not None and cancel.is_set():
        raise BackupError("Die Sicherung wurde abgebrochen; es wurde kein neuer Stand angelegt.")


def _test_delay() -> float:
    try:
        return int(os.environ.get("SUPERPYBACKUP_TEST_DELAY_MS", "0")) / 1000.0
    except ValueError:
        return 0.0


def run_backup(config: cfgmod.Config, *, as_of: datetime | None = None, new_snapshot: bool | None = None, mode: str | None = None, identity: Identity | None = None, echo=None, progress=None, cancel=None) -> RunResult:
    """Fuehrt einen Sicherungslauf aus. Wirft nie fuer erwartbare Fehler, sondern liefert Status Failed.

    mode: FULL oder INCREMENTAL erzwingt die Art des Stands, None entscheidet nach `backup.full_every_days`.
    progress(phase, erledigt, gesamt, text) meldet den Fortschritt (Phasen: scan, copy); cancel ist ein threading.Event,
    mit dem ein Lauf sauber abgebrochen werden kann (Arbeitsordner wird entfernt, es entsteht kein Stand).
    """
    started = time.perf_counter()
    as_of = as_of or datetime.now()
    identity = identity or get_identity()
    log = RunLog(config.log_dir, user=identity.sid, target=config.backup_root, max_bytes=config.log_max_bytes, echo=echo)
    result = RunResult(status=FAILED, log_path=log.path)
    stats = result.stats
    mutex = winapi.NamedMutex("Local\\SuperPyBackup-" + identity.sid)
    lock: TargetLock | None = None
    stage: str | None = None
    shadows: vss.ShadowSet | None = None
    try:
        log.info(f"Start {__version__} Benutzer={identity.name} Datum={as_of:%Y-%m-%d %H:%M:%S}")
        removed_logs = log.prune(config.log_keep_days, as_of.date())
        if removed_logs:
            log.info(f"Alte Protokolle entfernt: {len(removed_logs)}")
        if not mutex.try_acquire():
            result.status = BUSY
            result.message = "Ein Sicherungslauf ist bereits aktiv."
            log.info(result.message)
            return result
        log.start_live()  # erst nach dem Mutex: ein abgewiesener zweiter Lauf darf die Anzeige des laufenden nicht leeren
        log.info(f"Sicherungslauf beginnt ({__version__})")
        root = config.backup_root
        if not root or not os.path.isabs(root):
            raise BackupError("Sicherungsziel ist nicht als absoluter Pfad konfiguriert.")
        connection_error = netshare.ensure_connected(root, log)
        if connection_error:
            raise BackupError(connection_error)
        if not os.path.isdir(fsutil.fs(root)):
            _create_target_folder(root, log)
        if not os.path.isdir(fsutil.fs(root)):
            raise BackupError(f"Sicherungsziel nicht vorhanden oder nicht erreichbar: {root}")
        fsutil.assert_plain_directory(root)
        sources, notes = cfgmod.resolve_sources(config)
        for note in notes:
            log.warn(note)
        if not sources:
            raise BackupError("Keine Quellordner.")
        for source in sources:
            if not os.path.isdir(fsutil.fs(source.path)):
                raise BackupError(f"Quellordner fehlt oder ist nicht erreichbar: {source.name}: {source.path}")
            if fsutil.is_within(root, source.path) or fsutil.is_within(source.path, root):
                raise BackupError(f"Quelle und Sicherungsziel ueberlappen: {source.path}")
        log.info("Quellen: " + "; ".join(f"{s.name}={s.path}" for s in sources))
        target = config.target_dir(identity)
        result.target = target
        log.target = target
        os.makedirs(fsutil.fs(target), exist_ok=True)
        fsutil.assert_plain_directory(target)
        lock = TargetLock(os.path.join(target, ".backup.lock"))
        if not lock.acquire():
            result.status = BUSY
            result.message = "Das Sicherungsziel wird bereits von einem anderen Lauf verwendet."
            log.info(result.message)
            lock = None
            return result
        probe = os.path.join(target, f".write-test-{fsutil.new_id(12)}")
        try:
            with open(fsutil.fs(probe), "wb") as handle:
                handle.write(b"probe")
                handle.flush()
                os.fsync(handle.fileno())
            os.remove(fsutil.fs(probe))
        except OSError as error:
            raise BackupError(f"Keine Schreibrechte im Sicherungsziel: {target}: {error}") from error
        for tier in snapshots.TIERS:
            os.makedirs(fsutil.fs(os.path.join(target, tier)), exist_ok=True)
            fsutil.assert_plain_directory(os.path.join(target, tier))
        _stage_cleanup(target, log)

        day = as_of.strftime("%Y-%m-%d")
        if mode not in (None, FULL, INCREMENTAL):
            raise BackupError(f"Unbekannte Art der Sicherung: {mode!r}")
        make_new = (config.backup["existing_daily"] == "new_snapshot") if new_snapshot is None else new_snapshot
        if mode == FULL and new_snapshot is None:
            make_new = True  # eine ausdruecklich angeforderte Vollsicherung wird auch bei vorhandenem Tagesstand angelegt
        refs = snapshots.list_snapshots(target, identity.sid, ("Daily",))
        for ref in refs:
            if ref.manifest is None:
                log.warn(f"Defekter Tagesstand bleibt unveraendert: {ref.spec}")
        todays = [ref for ref in refs if ref.manifest is not None and ref.manifest.status == mf.COMPLETE and ref.name[:10] == day]
        if todays and not make_new:
            ref = todays[0]
            result.status = ALREADY
            result.snapshot_path = ref.path
            stats.unchanged = len(ref.manifest.files)
            stats.total_bytes = ref.manifest.total_bytes
            log.info("Heute bereits erfolgreich gesichert; Quellen werden nicht erneut gelesen.")
            ensure_archive(target, "Monthly", day[:7], ref.path, ref.manifest, log, stats)
            ensure_archive(target, "Yearly", day[:4], ref.path, ref.manifest, log, stats)
            stats.retention_removed = retention.apply_retention(target, identity.sid, as_of.date(), config.retention["daily"], config.retention["monthly"], config.retention["yearly"], log)
            return result

        base = snapshots.latest_complete(target, identity.sid, "Daily", day)
        previous_index = base.manifest.index() if base else {}
        log.info(f"Basis fuer unveraenderte Dateien: {base.spec if base else 'keine (vollstaendige Sicherung)'}")
        last_full = snapshots.latest_full(target, identity.sid, day)

        excluded = build_excluder(config.excludes)
        if progress:
            progress('scan', 0, 0, 'Quellen werden durchsucht ...')
        try:
            shadows = vss.open_for(config.backup["use_vss"], [source.path for source in sources], log)
        except vss.ShadowError as error:
            raise BackupError(str(error)) from error
        files, directories, categories, scan_errors = scan_sources(sources, excluded, log, stats, cancel, shadows.translate if shadows else None)
        errors = list(scan_errors)
        detection = config.backup["change_detection"]
        use_links = config.backup["use_hardlinks"] and winapi.volume_supports_hardlinks(target)
        if config.backup["use_hardlinks"] and not use_links:
            log.warn("Das Sicherungsziel unterstuetzt keine Hardlinks; unveraenderte Dateien werden kopiert.")
        kind, why = decide_kind(mode, base, last_full, day, config.backup["full_every_days"], use_links)
        result.kind = kind
        log.info(f"Art der Sicherung: {'Vollsicherung' if kind == FULL else 'Inkrement'} ({why})")
        plan: list[tuple[FileInfo, mf.FileEntry | None, bool]] = []
        needed = 0
        for info in files:
            old = previous_index.get(info.key)
            unchanged = kind == INCREMENTAL and old is not None and old.size == info.size and old.mtime_ns == info.mtime_ns
            plan.append((info, old, unchanged))
            if not unchanged or not use_links:
                needed += info.size
        stats.deleted = len(set(previous_index) - {info.key for info in files})
        log.info(f"Scan: {stats.scanned} Dateien, {len(directories)} Ordner, {stats.skipped} ausgelassen, {stats.deleted} seit dem letzten Stand entfernt")
        _check_space(target, needed, config, log)

        stage = os.path.join(target, snapshots.STAGE_PREFIX + fsutil.new_id(16))
        os.mkdir(fsutil.fs(stage))
        for category in categories:
            os.makedirs(fsutil.fs(os.path.join(stage, category["name"])), exist_ok=True)
        for directory in directories:
            os.makedirs(fsutil.fs(fsutil.safe_join(stage, directory)), exist_ok=True)
        entries: list[mf.FileEntry] = []
        delay = _test_delay()
        link_failed_reported = False
        for position, (info, old, unchanged) in enumerate(plan, 1):
            _check_cancel(cancel)
            if progress:
                progress('copy', position, len(plan), info.key)
            destination = fsutil.safe_join(stage, info.key)
            try:
                if delay:
                    time.sleep(delay)
                if unchanged and use_links and old is not None:
                    if detection == "hash" and fsutil.hash_file(info.path) != old.sha256:
                        unchanged = False
                    else:
                        previous_file = fsutil.safe_join(base.path, info.key)
                        try:
                            os.link(fsutil.fs(previous_file), fsutil.fs(destination))
                            stats.unchanged += 1
                            stats.linked += 1
                            entries.append(mf.FileEntry(info.key, info.size, info.mtime_ns, old.sha256, info.attrs))
                            stats.total_bytes += info.size
                            log.file("unveraendert (Hardlink)", info.key)
                            continue
                        except OSError as error:
                            if os.path.lexists(fsutil.fs(destination)):
                                os.remove(fsutil.fs(destination))
                            if not link_failed_reported:
                                log.warn(f"Hardlink nicht moeglich ({error}); es wird kopiert.")
                                link_failed_reported = True
                size, digest, mtime_ns = fsutil.copy_file_hashed(info.path, destination)
                fsutil.set_mtime(destination, mtime_ns)
                fsutil.make_readonly(destination)
                was_unchanged = old is not None and old.sha256 == digest
                log.file("neu" if old is None else "unveraendert (kopiert)" if was_unchanged else "geaendert", f"{info.key} ({size} Bytes)")
                if old is None:
                    stats.new += 1
                elif was_unchanged:
                    stats.unchanged += 1
                else:
                    stats.modified += 1
                stats.bytes_transferred += size
                stats.extra_storage_bytes += size
                stats.total_bytes += size
                entries.append(mf.FileEntry(info.key, size, mtime_ns, digest, info.attrs))
            except OSError as error:
                if os.path.lexists(fsutil.fs(destination)):
                    try:
                        fsutil.make_writable(destination)
                        os.remove(fsutil.fs(destination))
                    except OSError:
                        pass
                if error.errno == errno.ENOSPC:
                    raise BackupError("Sicherungsziel ist voll (kein Speicherplatz mehr).") from error
                stats.errors += 1
                errors.append({"path": info.path, "error": str(error)})
                log.error(f"Datei nicht gesichert: {info.path}: {error}")

        status = mf.COMPLETE if stats.errors == 0 else mf.PARTIAL
        manifest = mf.Manifest(
            status=status,
            user_sid=identity.sid,
            user_name=identity.name,
            date=day,
            created_utc=datetime.now(timezone.utc).isoformat(),
            finished_utc=datetime.now(timezone.utc).isoformat(),
            program_version=__version__,
            categories=categories,
            directories=directories,
            files=entries,
            errors=errors,
            stats={"type": kind, "scanned": stats.scanned, "new": stats.new, "modified": stats.modified, "unchanged": stats.unchanged, "skipped": stats.skipped, "errors": stats.errors, "bytes_transferred": stats.bytes_transferred, "total_bytes": stats.total_bytes},
        )
        mf.save(stage, manifest)
        daily_dir = os.path.join(target, "Daily")
        regular = os.path.join(daily_dir, day)
        if status == mf.COMPLETE and not os.path.lexists(fsutil.fs(regular)):
            final = regular
        else:
            final = os.path.join(daily_dir, f"{day}_{as_of:%H%M%S}_{fsutil.new_id(8)}")
        fsutil.rename_with_retry(stage, final)  # einzige Freigabe: ab hier ist der Stand sichtbar
        stage = None
        result.snapshot_path = final
        if status == mf.COMPLETE:
            result.status = COMPLETE
            log.info(f"Tagesstand freigegeben: {os.path.relpath(final, target)}")
            promote_archives(target, identity.sid, as_of.date(), log, stats)
            stats.retention_removed = retention.apply_retention(target, identity.sid, as_of.date(), config.retention["daily"], config.retention["monthly"], config.retention["yearly"], log)
        else:
            result.status = PARTIAL
            result.message = f"{stats.errors} Datei-/Ordnerfehler: Teil-Stand gesichert, aber nicht als vollstaendig gewertet; keine Rotation."
            log.warn(f"Teil-Stand gespeichert: {os.path.relpath(final, target)} - {result.message}")
        return result
    except BackupError as error:
        result.status = FAILED
        result.message = str(error)
        stats.errors += 1
        log.error(result.message)
        return result
    except OSError as error:
        result.status = FAILED
        result.message = f"{type(error).__name__}: {error}"
        stats.errors += 1
        log.error(result.message)
        return result
    finally:
        if stage is not None and os.path.lexists(fsutil.fs(stage)):
            try:
                fsutil.remove_tree_safe(stage)
            except OSError as error:
                log.warn(f"Unvollstaendigen Arbeitsordner manuell pruefen: {stage}: {error}")
        if shadows is not None:
            shadows.close()
        if lock is not None:
            lock.release()
        mutex.close()
        result.seconds = time.perf_counter() - started
        if result.status in (COMPLETE, ALREADY):
            staleness.record_success(config)
        try:
            log.info(
                f"Ende Status={result.status} Art={result.kind or '-'} Dauer={result.seconds:.2f}s Gescannt={stats.scanned} Neu={stats.new} Geaendert={stats.modified} "
                f"Unveraendert={stats.unchanged} Ausgelassen={stats.skipped} Fehler={stats.errors} Uebertragen={stats.bytes_transferred}B "
                f"Gesamt={stats.total_bytes}B ZusaetzlicherSpeicher={stats.extra_storage_bytes}B Entfernt={len(stats.retention_removed)} "
                f"Warnungen={log.warnings}"
            )
        except Exception:  # noqa: BLE001 - Protokollfehler duerfen das Ergebnis nicht verdecken
            pass
