"""Zentrale Protokollierung: ein Protokoll pro Kalendertag, begrenzte Dateigroesse, automatische Bereinigung."""

from __future__ import annotations

import os
import re
import sys
import threading
from datetime import date, datetime, timedelta
from typing import TextIO

from . import fsutil

LIVE_NAME = "aktuell.log"
LOG_NAME =re.compile(r"^(\d{4}-\d{2}-\d{2})(?:\.\d+)?\.log$")


class RunLog:
    """Schreibt Zeilen `Zeitstempel [LEVEL] User=<SID> Target=<Ziel> Text` in <Protokollordner>/<Datum>.log."""

    def __init__(self, directory: str, *, user: str = "", target: str = "", max_bytes: int = 20 * 1024 * 1024, echo: TextIO | None = None, prefix: str = "") -> None:
        self.directory = directory
        self.user = user
        self.target = target
        self.max_bytes = max_bytes
        self.echo = echo
        self.prefix = prefix  # z. B. "restore-"
        self.live_path: str | None = None
        self._live = None  # offene Live-Datei (gepuffert); ein Hilfsthread schreibt sie spaetestens alle 0,5 s heraus
        self._live_stop = threading.Event()
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
            self._write_live(f"{line[:23]} [{level}] {message}")
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

    def start_live(self) -> None:
        """Beginnt die Live-Datei `aktuell.log` (alle Ereignisse und jede einzelne Datei des laufenden Laufs; wird geleert).

        Die Datei bleibt offen und wird gepuffert geschrieben (eine Datei je Zeile zu oeffnen kostete bei 20 000 Dateien Sekunden);
        Ereignisse werden sofort herausgeschrieben, Zeilen einzelner Dateien spaetestens nach 0,5 s (die Oberflaeche liest alle 5 s).
        """
        self.close_live()
        self.live_path = os.path.join(self.directory, LIVE_NAME)
        try:
            self._live = open(fsutil.fs(self.live_path), "w", encoding="utf-8", newline="\n", buffering=64 * 1024)
        except OSError:
            self.live_path = None
            return
        self._live_stop = threading.Event()
        threading.Thread(target=self._flush_live_regularly, args=(self._live_stop,), daemon=True).start()

    def _flush_live_regularly(self, stop: threading.Event) -> None:
        while not stop.wait(0.5):
            with self._lock:
                self._flush_live()

    def _flush_live(self) -> None:
        handle = self._live
        if handle is None:
            return
        try:
            handle.flush()
        except (OSError, ValueError):
            self._live = None

    def close_live(self) -> None:
        """Schreibt die Live-Datei fertig heraus und schliesst sie (am Ende des Laufs)."""
        self._live_stop.set()
        with self._lock:
            handle, self._live = self._live, None
        if handle is not None:
            try:
                handle.close()
            except (OSError, ValueError):
                pass

    def _write_live(self, text: str, *, flush: bool = True) -> None:
        handle = self._live
        if not self.live_path or handle is None:
            return
        try:
            handle.write(text + "\n")
            if flush:
                handle.flush()
        except (OSError, ValueError):
            self.live_path = None
            self._live = None

    def file(self, action: str, name: str) -> None:
        """Eine Datei des laufenden Laufs nur in die Live-Datei (nicht ins Tagesprotokoll, das waere zu gross)."""
        with self._lock:
            self._write_live(f"{datetime.now():%Y-%m-%d %H:%M:%S.%f}"[:-3] + f" [DATEI] {action}: {name}", flush=False)

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
