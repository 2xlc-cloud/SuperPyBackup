"""Reproduzierbarer Build der Windows-Programme.

    python build\\build.py

Erzeugt in dist\\:
  SuperPyBackup.exe     konsolenlos (fuer die geplante Aufgabe, kein sichtbares Fenster)
  SuperPyBackupCmd.exe  Konsolenversion (Wiederherstellung, Konfiguration, Aufgabe, Update)
  SHA256SUMS.txt        Pruefsummen (werden auch fuer die Update-Pruefung verwendet)

Voraussetzungen: Python 3.13 und die in requirements-build.txt festgelegte PyInstaller-Version.
Die Quellen liegen unveraendert in src\\; Build-Zwischenstaende landen in build\\work (nicht versioniert).
Hinweis: PyInstaller bettet Zeitstempel ein; die Dateien sind daher bei jedem Build funktional, aber nicht bytegleich.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
WORK = ROOT / "build" / "work"
EXCLUDES = ["unittest", "pydoc", "doctest", "test", "lib2to3", "distutils", "sqlite3", "asyncio", "multiprocessing", "pdb"]  # email/http/urllib werden vom Updater benoetigt
TARGETS = [("SuperPyBackup", "--noconsole"), ("SuperPyBackupCmd", "--console")]


def version() -> str:
    sys.path.insert(0, str(ROOT / "src"))
    from superpybackup import __version__

    return __version__


def main() -> int:
    DIST.mkdir(exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(ROOT / "src"))
    from superpybackup import icon

    icon_path = WORK / "SuperPyBackup.ico"
    icon_path.write_bytes(icon.ico_bytes())
    for name, mode in TARGETS:
        command = [
            sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", mode,
            "--name", name, "--paths", str(ROOT / "src"),
            "--distpath", str(DIST), "--workpath", str(WORK / name), "--specpath", str(WORK),
            "--log-level", "WARN", "--icon", str(icon_path),
        ]
        for module in EXCLUDES:
            command += ["--exclude-module", module]
        command.append(str(ROOT / "build" / "entry.py"))
        print("Baue", name, mode)
        subprocess.run(command, check=True, cwd=ROOT)
    lines = []
    for name, _ in TARGETS:
        path = DIST / f"{name}.exe"
        lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}")
    (DIST / "SHA256SUMS.txt").write_text("\n".join(lines) + "\n", encoding="ascii")
    shutil.rmtree(WORK, ignore_errors=True)
    print(f"\nFertig (Version {version()}):")
    for name, _ in TARGETS:
        path = DIST / f"{name}.exe"
        print(f"  {path}  {path.stat().st_size / 1024 / 1024:.1f} MB")
    print((DIST / "SHA256SUMS.txt").read_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
