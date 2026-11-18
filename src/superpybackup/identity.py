"""Identitaet des angemeldeten Windows-Benutzers."""

from __future__ import annotations

import getpass
import os
from dataclasses import dataclass

from . import snapshots, winapi


@dataclass(frozen=True)
class Identity:
    sid: str
    name: str

    @property
    def folder(self) -> str:
        return snapshots.target_folder_name(self.name, self.sid)


def get_identity() -> Identity:
    name = os.environ.get("USERNAME") or getpass.getuser()
    return Identity(sid=winapi.current_user_sid(), name=name)
