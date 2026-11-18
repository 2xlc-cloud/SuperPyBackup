"""Auflisten und Benennen von Sicherungsstaenden (Daily / Monthly / Yearly)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from . import fsutil, manifest as mf, winapi

TIERS = ("Daily", "Monthly", "Yearly")
NAME_PATTERNS = {
    "Daily": re.compile(r"^\d{4}-\d{2}-\d{2}(?:_\d{6}_[a-f0-9]{8})?$"),
    "Monthly": re.compile(r"^\d{4}-\d{2}$"),
    "Yearly": re.compile(r"^\d{4}$"),
}
PERIOD_LENGTH = {"Daily": 10, "Monthly": 7, "Yearly": 4}
STAGE_PREFIX = ".incomplete-"
DELETE_PREFIX = ".deleting-"


@dataclass
class SnapshotRef:
    tier: str
    name: str
    path: str
    manifest: mf.Manifest | None  # None = ungueltig/defekt

    @property
    def period(self) -> str:
        return self.name[: PERIOD_LENGTH[self.tier]]

    @property
    def spec(self) -> str:
        return f"{self.tier.lower()}/{self.name}"


def list_snapshots(target: str, sid: str | None = None, tiers: tuple[str, ...] = TIERS) -> list[SnapshotRef]:
    """Alle Staende mit gueltigem Namen; manifest=None markiert defekte Staende. Neueste zuerst."""
    found: list[SnapshotRef] = []
    for tier in tiers:
        parent = os.path.join(target, tier)
        try:
            names = os.listdir(fsutil.fs(parent))
        except OSError:
            continue
        for name in names:
            if not NAME_PATTERNS[tier].match(name):
                continue
            path = os.path.join(parent, name)
            if not os.path.isdir(fsutil.fs(path)):
                continue
            found.append(SnapshotRef(tier, name, path, mf.load(path, sid=sid)))
    found.sort(key=lambda ref: (ref.tier != "Daily", ref.tier, ref.name), reverse=False)
    found.sort(key=lambda ref: ref.name, reverse=True)
    return found


def latest_complete(target: str, sid: str | None, tier: str = "Daily", on_or_before: str | None = None) -> SnapshotRef | None:
    for ref in list_snapshots(target, sid, (tier,)):
        if ref.manifest is None or ref.manifest.status != mf.COMPLETE:
            continue
        if on_or_before is not None and ref.name[:10] > on_or_before:
            continue
        return ref
    return None


def leftover_work_dirs(target: str) -> list[str]:
    result = []
    try:
        for name in os.listdir(fsutil.fs(target)):
            if name.startswith((STAGE_PREFIX, DELETE_PREFIX)):
                path = os.path.join(target, name)
                if os.path.isdir(fsutil.fs(path)):
                    result.append(path)
    except OSError:
        pass
    return result


def target_folder_name(user_name: str, sid: str) -> str:
    return f"{winapi.sanitize_name(user_name)}-{sid}"
