"""Snapshot-Manifest (.snapshot.json): Format, Pruefung beim Laden, atomares Schreiben."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import fsutil

MANIFEST_NAME = ".snapshot.json"
FORMAT = 2
COMPLETE = "Complete"
PARTIAL = "Partial"
SHA256 = re.compile(r"^[a-f0-9]{64}$")
CATEGORY = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


@dataclass
class FileEntry:
    path: str  # relativ, immer mit '/' (erstes Segment = Kategorie)
    size: int
    mtime_ns: int
    sha256: str
    attrs: int = 0


@dataclass
class Manifest:
    status: str
    user_sid: str
    date: str  # JJJJ-MM-TT
    created_utc: str
    categories: list[dict[str, str]]
    directories: list[str]
    files: list[FileEntry]
    user_name: str = ""
    program_version: str = ""
    finished_utc: str = ""
    errors: list[dict[str, str]] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def total_bytes(self) -> int:
        return sum(entry.size for entry in self.files)

    def index(self) -> dict[str, FileEntry]:
        return {entry.path: entry for entry in self.files}

    def to_json(self) -> dict[str, Any]:
        return {
            "format": FORMAT,
            "status": self.status,
            "user_sid": self.user_sid,
            "user_name": self.user_name,
            "date": self.date,
            "created_utc": self.created_utc,
            "finished_utc": self.finished_utc,
            "program_version": self.program_version,
            "categories": self.categories,
            "directories": self.directories,
            "errors": self.errors,
            "stats": self.stats,
            "files": [[e.path, e.size, e.mtime_ns, e.sha256, e.attrs] for e in self.files],
        }


class ManifestError(ValueError):
    pass


def _parse(data: Any) -> Manifest:
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ManifestError("Unbekanntes Manifestformat")
    status = data.get("status")
    if status not in (COMPLETE, PARTIAL):
        raise ManifestError(f"Ungueltiger Status: {status!r}")
    datetime.strptime(data["date"], "%Y-%m-%d")
    sid = data["user_sid"]
    if not isinstance(sid, str) or not sid:
        raise ManifestError("SID fehlt")
    categories = data["categories"]
    if not isinstance(categories, list) or not categories:
        raise ManifestError("Keine Kategorien")
    names = set()
    for category in categories:
        if not isinstance(category, dict) or not CATEGORY.match(str(category.get("name", ""))) or not isinstance(category.get("source"), str):
            raise ManifestError("Ungueltige Kategorie")
        names.add(category["name"])
    directories = data["directories"]
    if not isinstance(directories, list):
        raise ManifestError("Ungueltige Ordnerliste")
    for directory in directories:
        fsutil.check_relative(directory)
        if directory.split("/")[0] not in names:
            raise ManifestError(f"Ordner ohne Kategorie: {directory}")
    files: list[FileEntry] = []
    seen: set[str] = set()
    for row in data["files"]:
        if not isinstance(row, list) or len(row) != 5:
            raise ManifestError("Ungueltiger Dateieintrag")
        path, size, mtime_ns, digest, attrs = row
        fsutil.check_relative(path)
        if path.split("/")[0] not in names or path in seen:
            raise ManifestError(f"Ungueltiger oder doppelter Pfad: {path}")
        if not (isinstance(size, int) and size >= 0 and isinstance(mtime_ns, int) and isinstance(attrs, int) and isinstance(digest, str) and SHA256.match(digest)):
            raise ManifestError(f"Ungueltige Werte fuer {path}")
        seen.add(path)
        files.append(FileEntry(path, size, mtime_ns, digest, attrs))
    errors = data.get("errors", [])
    if not isinstance(errors, list):
        raise ManifestError("Ungueltige Fehlerliste")
    return Manifest(
        status=status,
        user_sid=sid,
        date=data["date"],
        created_utc=str(data.get("created_utc", "")),
        categories=categories,
        directories=directories,
        files=files,
        user_name=str(data.get("user_name", "")),
        program_version=str(data.get("program_version", "")),
        finished_utc=str(data.get("finished_utc", "")),
        errors=errors,
        stats=data.get("stats", {}) if isinstance(data.get("stats", {}), dict) else {},
    )


def load(snapshot_dir: str, *, sid: str | None = None, require_complete: bool = False) -> Manifest | None:
    """Liest und prueft das Manifest eines Stands. None bei jedem Defekt (nie eine Ausnahme)."""
    try:
        fsutil.assert_plain_directory(snapshot_dir)
        path = os.path.join(snapshot_dir, MANIFEST_NAME)
        if fsutil.is_link_or_junction(path) or fsutil.is_reparse(path):
            return None
        manifest = _parse(fsutil.read_json(path))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if sid is not None and manifest.user_sid != sid:
        return None
    if require_complete and manifest.status != COMPLETE:
        return None
    return manifest


def save(snapshot_dir: str, manifest: Manifest) -> None:
    fsutil.atomic_write_json(os.path.join(snapshot_dir, MANIFEST_NAME), manifest.to_json())
