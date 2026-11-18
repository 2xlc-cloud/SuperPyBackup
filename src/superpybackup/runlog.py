"""Zentrale Protokollierung: ein Protokoll pro Kalendertag, begrenzte Dateigroesse, automatische Bereinigung."""

from __future__ import annotations

import os
import re
import sys
import threading
from datetime import date, datetime, timedelta
from typing import TextIO

from . import fsutil

LOG_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:\.\d+)?\.log$")


class RunLog:
    """Schreibt Zeilen `Zeitstempel [LEVEL] User=<SID> Target=<Ziel> Text` in <Protokollordner>/<Datum>.log."""

    def __init__(self, directory: str, *, user: str = "", target: str = "", max_bytes: int = 20 * 1024 * 1024, echo: TextIO | None = None, prefix: str = "") -> None:
        self.directory = directory
        self.user = user
        self.target = target
        self.max_bytes = max_bytes
        self.echo = echo
        self.prefix = prefix  # z. B. "restore-"
        self.warnings = 0
        self.errors = 0
        self._lock = threading.Lock()
        self._usable = True
        try:
            os.makedirs(fsutil.fs(directory), exist_ok=True)
        except OSError as error:
            self._usable = False
            self._emit_stderr(f"Protokollordner nicht nutzbar ({directory}): {error}")

    @staticmethod
    def _emit_stderr(message: str) -> None:
        stream = sys.stderr
        if stream is not None:
            try:
                stream.write(message + "\n")
            except (OSError, ValueError):
                pass

    @property
    def path(self) -> str:
        return os.path.join(self.directory, f"{self.prefix}{datetime.now():%Y-%m-%d}.log")

    def _roll_if_needed(self, path: str) -> None:
        try:
            if os.path.getsize(fsutil.fs(path)) < self.max_bytes:
                return
        except OSError:
            return
        rolled = path[:-4] + ".1.log"
        try:
            os.replace(fsutil.fs(path), fsutil.fs(rolled))  # nur eine Vorgaengerdatei: Obergrenze 2 x max_bytes
        except OSError:
            pass

    def write(self, level: str, message: str) -> None:
        line = f"{datetime.now():%Y-%m-%d %H:%M:%S.%f}"[:-3] + f" [{level}] User={self.user} Target={self.target} {message}"
        if level == "WARN":
            self.warnings += 1
        elif level == "ERROR":
            self.errors += 1
        with self._lock:
            if self.echo is not None:
                try:
                    # Die Anzeige ist kuerzer als die Datei (ohne SID und Zielpfad, die in jeder Zeile gleich waeren).
                    self.echo.write(f"{line[:23]} [{level}] {message}\n")
                    self.echo.flush()
                except (OSError, ValueError):
                    pass
            if not self._usable:
                return
            try:
                path = self.path
                self._roll_if_needed(path)
                with open(fsutil.fs(path), "a", encoding="utf-8", newline="\n") as handle:
                    handle.write(line + "\n")
            except OSError as error:
                self._usable = False
                self._emit_stderr(f"Protokoll nicht beschreibbar: {error}")

    def info(self, message: str) -> None:
        self.write("INFO", message)

    def warn(self, message: str) -> None:
        self.write("WARN", message)

    def error(self, message: str) -> None:
        self.write("ERROR", message)

    def prune(self, keep_days: int, today: date | None = None, *, dry_run: bool = False) -> list[str]:
        """Entfernt Protokolle, die aelter als keep_days Kalendertage sind. Liefert die entfernten Dateien."""
        today = today or date.today()
        cutoff = today - timedelta(days=keep_days - 1)
        removed: list[str] = []
        if not self._usable:
            return removed
        try:
            names = os.listdir(fsutil.fs(self.directory))
        except OSError:
            return removed
        for name in names:
            stem = name[len(self.prefix):] if self.prefix and name.startswith(self.prefix) else name
            if self.prefix and not name.startswith(self.prefix):
                continue
            match = LOG_NAME.match(stem)
            if not match:
                continue
            try:
                if datetime.strptime(match.group(1), "%Y-%m-%d").date() < cutoff:
                    if not dry_run:
                        os.remove(fsutil.fs(os.path.join(self.directory, name)))
                    removed.append(name)
            except (OSError, ValueError):
                continue
        return removed
