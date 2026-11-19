"""Sichere Dateisystem-Helfer: Pfade, Hashes, atomares Schreiben, sicheres Loeschen."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import stat
import time
import uuid
from pathlib import PurePosixPath
from typing import Any, Callable, Iterator

from . import winapi

CHUNK = 1024 * 1024
LONG_PATH_THRESHOLD = 240


def fs(path: str) -> str:
    """Pfad fuer Dateioperationen; ab 240 Zeichen mit \\\\?\\-Praefix (Pfade ueber MAX_PATH)."""
    if not winapi.IS_WINDOWS or len(path) < LONG_PATH_THRESHOLD or path.startswith("\\\\?\\"):
        return path
    absolute = os.path.abspath(path)
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute


def rename_with_retry(source: str, destination: str, *, attempts: int = 15) -> None:
    """Benennt um und wiederholt kurz bei vorubergehenden Sperren (Virenscanner/Indexdienst halten oft kurz Handles).

    Ein vorhandenes Ziel wird nie ueberschrieben: os.rename schlaegt dann fehl.
    """
    delay = 0.05
    for attempt in range(1, attempts + 1):
        try:
            os.rename(fs(source), fs(destination))
            return
        except PermissionError:
            if attempt == attempts or os.path.lexists(fs(destination)):
                raise
            time.sleep(delay)
            delay = min(delay * 1.6, 1.0)

def disk_free(path: str) -> int:
    """Freier Speicher des Datentraegers (eigene Funktion, damit Tests Speichermangel simulieren koennen)."""
    return shutil.disk_usage(path).free


def new_id(length: int = 8) -> str:
    return uuid.uuid4().hex[:length]


def norm_key(path: str) -> str:
    """Vergleichsschluessel fuer Pfade (Windows ist nicht gross/klein-sensitiv)."""
    return os.path.normcase(os.path.abspath(path)).rstrip("\\/")


def is_within(child: str, parent: str) -> bool:
    c = norm_key(os.path.realpath(child))
    p = norm_key(os.path.realpath(parent))
    return c == p or c.startswith(p + os.sep)


def is_reparse(path: str) -> bool:
    try:
        return bool(os.lstat(fs(path)).st_file_attributes & winapi.ATTR_REPARSE_POINT)
    except AttributeError:  # keine Windows-Attribute
        return os.path.islink(path)
    except OSError:
        return False


def is_link_or_junction(path: str) -> bool:
    """Symbolischer Link oder Junction (Cloud-Platzhalter zaehlen nicht)."""
    p = fs(path)
    return os.path.islink(p) or (hasattr(os.path, "isjunction") and os.path.isjunction(p))


def assert_plain_directory(path: str) -> None:
    info = os.lstat(fs(path))
    if not stat.S_ISDIR(info.st_mode) or is_link_or_junction(path) or is_reparse(path):
        raise OSError(f"Verzeichnis darf keine Verknuepfung sein: {path}")


def assert_plain_ancestors(path: str) -> None:
    """Kein Bestandteil des (vorhandenen) Pfades darf eine Verknuepfung sein."""
    cursor = os.path.abspath(path)
    while True:
        if os.path.lexists(fs(cursor)) and (is_link_or_junction(cursor) or is_reparse(cursor)):
            raise OSError(f"Verknuepfung im Pfad: {cursor}")
        parent = os.path.dirname(cursor)
        if parent == cursor:
            return
        cursor = parent


def check_relative(relative: str) -> list[str]:
    """Prueft einen relativen Manifest-Pfad ('a/b/c') und liefert seine Teile; lehnt Absolutes, Laufwerke, '\\' und '.'/'..' ab."""
    if not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative or relative.startswith("/"):
        raise ValueError(f"Unsicherer relativer Pfad: {relative!r}")
    parts = relative.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            raise ValueError(f"Unsicherer relativer Pfad: {relative!r}")
    return parts


def safe_join(root: str, relative: str) -> str:
    """Relativen Manifest-Pfad ('a/b/c') sicher unter root aufloesen; lehnt Absolutes und '..' ab."""
    return os.path.join(root, *check_relative(relative))


def hash_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(fs(path), "rb") as handle:
        while block := handle.read(CHUNK):
            digest.update(block)
    return digest.hexdigest()


def copy_file_hashed(source: str, destination: str, *, on_chunk: Callable[[], None] | None = None) -> tuple[int, str, int]:
    """Kopiert source nach destination (neue Datei, nie ueberschreibend) und liefert (Groesse, SHA-256, mtime_ns).

    Die Quelle wird nur einmal gelesen. Aendert sie sich waehrend des Kopierens, wird abgebrochen.
    """
    digest = hashlib.sha256()
    total = 0
    with open(fs(source), "rb") as reader:
        before = os.fstat(reader.fileno())
        with open(fs(destination), "xb") as writer:
            while block := reader.read(CHUNK):
                digest.update(block)
                writer.write(block)
                total += len(block)
                if on_chunk:
                    on_chunk()
            writer.flush()
            os.fsync(writer.fileno())
        after = os.fstat(reader.fileno())
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or total != before.st_size:
        raise OSError(errno.EAGAIN, "Datei wurde waehrend der Sicherung veraendert")
    return total, digest.hexdigest(), before.st_mtime_ns


def set_mtime(path: str, mtime_ns: int) -> None:
    os.utime(fs(path), ns=(mtime_ns, mtime_ns))


def make_readonly(path: str) -> None:
    os.chmod(fs(path), stat.S_IREAD)


def make_writable(path: str) -> None:
    try:
        os.chmod(fs(path), stat.S_IWRITE | stat.S_IREAD)
    except FileNotFoundError:
        pass


def atomic_write_json(path: str, data: Any) -> None:
    """JSON atomar schreiben: temporaere Datei im selben Ordner, fsync, os.replace."""
    temp = os.path.join(os.path.dirname(path), f".tmp-{new_id(12)}")
    try:
        with open(fs(temp), "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(fs(temp), fs(path))
    finally:
        if os.path.exists(fs(temp)):
            os.remove(fs(temp))


def read_json(path: str) -> Any:
    with open(fs(path), "r", encoding="utf-8") as handle:
        return json.load(handle)


def walk_files_and_dirs(root: str) -> Iterator[tuple[str, bool]]:
    """Alle Eintraege unterhalb root (ohne root); Verknuepfungen werden gemeldet, aber nicht betreten."""
    stack = [root]
    while stack:
        current = stack.pop()
        with os.scandir(fs(current)) as entries:
            for entry in entries:
                path = os.path.join(current, entry.name)
                is_dir = entry.is_dir(follow_symlinks=False)
                yield path, is_dir
                if is_dir and not entry.is_symlink() and not (hasattr(entry, "is_junction") and entry.is_junction()):
                    stack.append(path)


def remove_tree_safe(path: str) -> None:
    """Loescht einen Sicherungsordner. Verweigert bei Verknuepfungen im Baum; entfernt Schreibschutz."""
    assert_plain_directory(path)
    for entry_path, _ in walk_files_and_dirs(path):
        if is_link_or_junction(entry_path):
            raise OSError(f"Loeschen verweigert: Verknuepfung in {path}: {entry_path}")

    def on_error(function, failed_path, exc_info):  # noqa: ANN001 - Signatur von shutil
        make_writable(failed_path)
        function(failed_path)

    shutil.rmtree(fs(path), onexc=lambda f, p, e: on_error(f, p, e))


def unique_sibling(path: str, prefix: str) -> str:
    return os.path.join(os.path.dirname(path), f"{prefix}{new_id(16)}")
