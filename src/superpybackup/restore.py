"""Wiederherstellung und Integritaetspruefung von Sicherungsstaenden (unabhaengig von laufenden Sicherungen)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable

from . import fsutil, manifest as mf, snapshots, winapi
from .runlog import RunLog


class RestoreError(Exception):
    pass


@dataclass
class RestoreResult:
    copied: int = 0
    skipped: int = 0
    errors: int = 0
    bytes: int = 0
    directories: int = 0

    @property
    def exit_code(self) -> int:
        return 1 if self.errors else 0


def resolve_snapshot(target: str, spec: str, sid: str | None) -> snapshots.SnapshotRef:
    """spec: 'latest', 'daily/2026-11-17', 'monthly/2026-11', 'yearly/2026' oder ein Ordnerpfad."""
    if spec == "latest":
        ref = snapshots.latest_complete(target, sid)
        if ref is None:
            raise RestoreError("Es gibt noch keinen vollstaendigen Sicherungsstand.")
        return ref
    if "/" in spec and spec.split("/")[0].lower() in ("daily", "monthly", "yearly") and not os.path.isabs(spec):
        tier, name = spec.split("/", 1)
        tier = tier.capitalize()
        if not snapshots.NAME_PATTERNS[tier].match(name):
            raise RestoreError(f"Ungueltiger Stand: {spec}")
        path = os.path.join(target, tier, name)
    elif os.path.isabs(spec):
        path = spec
        tier, name = os.path.basename(os.path.dirname(path)), os.path.basename(path)
    else:
        raise RestoreError(f"Stand nicht erkannt: {spec!r} (Beispiele: latest, daily/2026-11-17, monthly/2026-11, yearly/2026)")
    manifest = mf.load(path, sid=sid)
    if manifest is None:
        raise RestoreError(f"Kein gueltiger Sicherungsstand: {path}")
    return snapshots.SnapshotRef(tier, name, path, manifest)


def _normalise_selection(manifest: mf.Manifest, categories: list[str] | None, paths: list[str] | None) -> tuple[set[str], list[str]]:
    names = {category["name"] for category in manifest.categories}
    selected_categories = set(categories) if categories else set(names)
    unknown = selected_categories - names
    if unknown:
        raise RestoreError(f"Unbekannte Kategorie: {', '.join(sorted(unknown))}. Vorhanden: {', '.join(sorted(names))}")
    prefixes: list[str] = []
    for path in paths or []:
        cleaned = path.replace("\\", "/").strip("/")
        fsutil.safe_join("X:\\root", cleaned)
        if cleaned.split("/")[0] not in selected_categories:
            raise RestoreError(f"Pfad liegt ausserhalb der gewaehlten Kategorien: {path}")
        known = cleaned in names or any(entry.path == cleaned or entry.path.startswith(cleaned + "/") for entry in manifest.files) or cleaned in manifest.directories
        if not known:
            raise RestoreError(f"Pfad nicht im Sicherungsstand enthalten: {path}")
        prefixes.append(cleaned)
    return selected_categories, prefixes


def _selected(path: str, categories: set[str], prefixes: list[str]) -> bool:
    if path.split("/")[0] not in categories:
        return False
    return not prefixes or any(path == prefix or path.startswith(prefix + "/") for prefix in prefixes)


def restore(
    snapshot_dir: str,
    destination: str | None,
    log: RunLog,
    *,
    categories: list[str] | None = None,
    paths: list[str] | None = None,
    original: bool = False,
    overwrite: bool = False,
    confirm_overwrite: Callable[[int], bool] | None = None,
    sid: str | None = None,
) -> RestoreResult:
    manifest = mf.load(snapshot_dir, sid=sid)
    if manifest is None:
        raise RestoreError(f"Kein gueltiger Sicherungsstand: {snapshot_dir}")
    if (destination is None) == (not original):
        raise RestoreError("Bitte entweder ein Zielverzeichnis (--dest) oder --original angeben.")
    snapshot_dir = os.path.abspath(snapshot_dir)
    fsutil.assert_plain_ancestors(snapshot_dir)
    selected_categories, prefixes = _normalise_selection(manifest, categories, paths)
    roots: dict[str, str] = {}
    for category in manifest.categories:
        if original:
            roots[category["name"]] = category["source"]
        else:
            roots[category["name"]] = os.path.join(os.path.abspath(destination), category["name"])
    for name, root in roots.items():
        if name in selected_categories and (fsutil.is_within(root, snapshot_dir) or fsutil.is_within(snapshot_dir, root)):
            raise RestoreError(f"Wiederherstellungsziel und Sicherungsstand duerfen nicht ueberlappen: {root}")
    if manifest.status != mf.COMPLETE:
        log.warn("Achtung: Dieser Stand ist nur ein Teil-Stand (Partial); nicht alle Dateien wurden gesichert.")
    wanted = [entry for entry in manifest.files if _selected(entry.path, selected_categories, prefixes)]

    def target_of(relative: str) -> str:
        category, _, rest = relative.partition("/")
        return os.path.join(roots[category], *rest.split("/")) if rest else roots[category]

    conflicts = [entry for entry in wanted if os.path.lexists(fsutil.fs(target_of(entry.path)))]
    allow_overwrite = overwrite
    if overwrite and conflicts:
        if confirm_overwrite is None or not confirm_overwrite(len(conflicts)):
            log.warn(f"Ueberschreiben nicht bestaetigt: {len(conflicts)} vorhandene Dateien werden ausgelassen.")
            allow_overwrite = False
    result = RestoreResult()
    log.info(f"Wiederherstellung Start: Stand={os.path.basename(os.path.dirname(snapshot_dir))}/{os.path.basename(snapshot_dir)} Dateien={len(wanted)} Ziel={'ursprueglicher Ort' if original else destination} Ueberschreiben={allow_overwrite}")
    for name in sorted(selected_categories):
        directory = roots[name]
        if not prefixes or any(prefix == name for prefix in prefixes):
            _ensure_dir(directory, result, log)
    for directory in manifest.directories:
        if _selected(directory, selected_categories, prefixes) or any(prefix.startswith(directory + "/") for prefix in prefixes if directory.split("/")[0] in selected_categories):
            _ensure_dir(target_of(directory), result, log)
    for entry in wanted:
        temporary: str | None = None
        try:
            source = fsutil.safe_join(snapshot_dir, entry.path)
            destination_file = target_of(entry.path)
            fsutil.assert_plain_ancestors(source)
            fsutil.assert_plain_ancestors(destination_file)
            exists = os.path.lexists(fsutil.fs(destination_file))
            if exists and not allow_overwrite:
                result.skipped += 1
                log.warn(f"Vorhandene Datei ausgelassen: {destination_file}")
                continue
            if exists and os.path.isdir(fsutil.fs(destination_file)):
                raise RestoreError(f"Am Zielort existiert ein Ordner mit demselben Namen: {destination_file}")
            parent = os.path.dirname(destination_file)
            os.makedirs(fsutil.fs(parent), exist_ok=True)
            fsutil.assert_plain_ancestors(parent)
            temporary = os.path.join(parent, f".restore-{fsutil.new_id(12)}")
            size, digest, _ = fsutil.copy_file_hashed(source, temporary)
            if digest != entry.sha256 or size != entry.size:
                raise RestoreError(f"Integritaetspruefung fehlgeschlagen: {source}")
            fsutil.set_mtime(temporary, entry.mtime_ns)
            if winapi.IS_WINDOWS:
                winapi.set_attributes(fsutil.fs(temporary), (entry.attrs & winapi.ATTR_RESTORABLE) or 0x80)
            if exists:
                fsutil.make_writable(destination_file)
            os.replace(fsutil.fs(temporary), fsutil.fs(destination_file))
            temporary = None
            result.copied += 1
            result.bytes += size
            log.info(f"Wiederhergestellt: {destination_file}")
        except (OSError, RestoreError, ValueError) as error:
            result.errors += 1
            log.error(f"Wiederherstellung fehlgeschlagen: {entry.path}: {error}")
        finally:
            if temporary is not None and os.path.lexists(fsutil.fs(temporary)):
                try:
                    os.remove(fsutil.fs(temporary))
                except OSError:
                    pass
    log.info(f"Wiederherstellung Ende: Kopiert={result.copied} Ausgelassen={result.skipped} Fehler={result.errors} Bytes={result.bytes}")
    return result


def _ensure_dir(path: str, result: RestoreResult, log: RunLog) -> None:
    try:
        fsutil.assert_plain_ancestors(path)
        os.makedirs(fsutil.fs(path), exist_ok=True)
        result.directories += 1
    except OSError as error:
        result.errors += 1
        log.error(f"Ordner konnte nicht angelegt werden: {path}: {error}")


def verify_snapshot(snapshot_dir: str, log: RunLog, sid: str | None = None) -> tuple[int, list[str]]:
    """Prueft jede gesicherte Datei gegen Groesse und SHA-256 im Manifest. Liefert (geprueft, fehlerhafte Pfade)."""
    manifest = mf.load(snapshot_dir, sid=sid)
    if manifest is None:
        raise RestoreError(f"Kein gueltiger Sicherungsstand: {snapshot_dir}")
    bad: list[str] = []
    for entry in manifest.files:
        path = fsutil.safe_join(snapshot_dir, entry.path)
        try:
            if os.path.getsize(fsutil.fs(path)) != entry.size or fsutil.hash_file(path) != entry.sha256:
                bad.append(entry.path)
                log.error(f"Pruefsumme stimmt nicht: {entry.path}")
        except OSError as error:
            bad.append(entry.path)
            log.error(f"Datei fehlt oder ist nicht lesbar: {entry.path}: {error}")
    log.info(f"Pruefung {snapshot_dir}: {len(manifest.files)} Dateien, {len(bad)} fehlerhaft")
    return len(manifest.files), bad
