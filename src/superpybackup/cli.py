"""Kommandozeile von SuperPyBackup (auch in der konsolenlosen SuperPyBackup.exe nutzbar)."""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

from . import __version__, cleanup as cleanupmod, config as cfgmod, engine, fsutil, restore as restoremod, scheduler, snapshots, updater
from .identity import get_identity
from .runlog import RunLog

EXIT_USAGE = 3


def _human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def _load_config(args: argparse.Namespace, *, require_target: bool = True) -> cfgmod.Config:
    return cfgmod.load(args.config, require_target=require_target)


def _target_dir(config: cfgmod.Config) -> tuple[str, str]:
    identity = get_identity()
    return os.path.join(config.backup_root, identity.folder), identity.sid


def _log(config: cfgmod.Config, *, quiet: bool = False) -> RunLog:
    identity = get_identity()
    return RunLog(config.log_dir, user=identity.sid, target=config.backup_root, max_bytes=config.log_max_bytes, echo=None if quiet else sys.stdout)


# --- Befehle -----------------------------------------------------------------------------------

def cmd_backup(args: argparse.Namespace) -> int:
    config = _load_config(args)
    result = engine.run_backup(config, new_snapshot=True if args.new_snapshot else None, echo=None if args.quiet else sys.stdout)
    stats = result.stats
    if not args.quiet:
        print(f"\nStatus: {result.status}" + (f" - {result.message}" if result.message else ""))
        if result.snapshot_path:
            print(f"Stand: {result.snapshot_path}")
        print(f"Gescannt {stats.scanned} | neu {stats.new} | geaendert {stats.modified} | unveraendert {stats.unchanged} | ausgelassen {stats.skipped} | Fehler {stats.errors}")
        print(f"Uebertragen {_human(stats.bytes_transferred)} | Gesamt {_human(stats.total_bytes)} | zusaetzlicher Speicher {_human(stats.extra_storage_bytes)} | {result.seconds:.1f} s")
    if result.status != engine.BUSY:
        try:
            log = _log(config, quiet=True)
            info = updater.periodic_check(config, log)
            if info and not args.quiet:
                print(f"Neue Version verfuegbar: {info.version}. Installation mit 'update install'.")
        except Exception:  # noqa: BLE001 - die Update-Pruefung darf einen Lauf nie beeintraechtigen
            pass
    return result.exit_code


def cmd_list(args: argparse.Namespace) -> int:
    config = _load_config(args)
    target, sid = _target_dir(config)
    refs = snapshots.list_snapshots(target, sid)
    order = {"Daily": 0, "Monthly": 1, "Yearly": 2}
    refs.sort(key=lambda ref: (order[ref.tier], ref.name), reverse=False)
    rows = []
    for tier in ("Daily", "Monthly", "Yearly"):
        for ref in sorted((r for r in refs if r.tier == tier), key=lambda r: r.name, reverse=True):
            manifest = ref.manifest
            rows.append({
                "spec": ref.spec,
                "tier": tier,
                "date": manifest.date if manifest else None,
                "status": manifest.status if manifest else "Defekt",
                "files": len(manifest.files) if manifest else None,
                "bytes": manifest.total_bytes if manifest else None,
                "created_utc": manifest.created_utc if manifest else None,
                "path": ref.path,
            })
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    if not rows:
        print(f"Keine Sicherungsstaende in {target}")
        return 0
    print(f"Sicherungsstaende in {target}\n")
    print(f"{'Stand':<32}{'Datum':<12}{'Status':<10}{'Dateien':>9}{'Groesse':>12}")
    for row in rows:
        print(f"{row['spec']:<32}{row['date'] or '-':<12}{row['status']:<10}{row['files'] if row['files'] is not None else '-':>9}{_human(row['bytes']) if row['bytes'] is not None else '-':>12}")
    return 0


def _ask(prompt: str, accepted: tuple[str, ...]) -> bool:
    """Fragt interaktiv nach. Ohne Terminal, bei Dateiende (z. B. NUL) oder Abbruch gilt immer: Nein."""
    if not sys.stdin:
        return False
    try:
        if not sys.stdin.isatty():
            return False
        return input(prompt).strip().lower() in accepted
    except (EOFError, OSError, ValueError):
        return False


def _confirm_overwrite_factory(assume_yes: bool):
    def confirm(count: int) -> bool:
        if assume_yes:
            return True
        return _ask(f"{count} vorhandene Datei(en) ueberschreiben? Tippen Sie 'ja' zur Bestaetigung: ", ("ja", "j", "yes", "y"))

    return confirm


def cmd_restore(args: argparse.Namespace) -> int:
    config = _load_config(args)
    target, sid = _target_dir(config)
    try:
        ref = restoremod.resolve_snapshot(target, args.snapshot, sid)
        log = _log(config)
        result = restoremod.restore(
            ref.path, args.dest, log,
            categories=args.category, paths=args.path, original=args.original, overwrite=args.overwrite,
            confirm_overwrite=_confirm_overwrite_factory(args.yes), sid=sid,
        )
    except restoremod.RestoreError as error:
        print(f"Fehler: {error}", file=sys.stderr)
        return 1
    print(f"Wiederhergestellt: {result.copied} Dateien ({_human(result.bytes)}), ausgelassen: {result.skipped}, Fehler: {result.errors}")
    return result.exit_code


def cmd_verify(args: argparse.Namespace) -> int:
    config = _load_config(args)
    target, sid = _target_dir(config)
    try:
        ref = restoremod.resolve_snapshot(target, args.snapshot, sid)
        checked, bad = restoremod.verify_snapshot(ref.path, _log(config), sid)
    except restoremod.RestoreError as error:
        print(f"Fehler: {error}", file=sys.stderr)
        return 1
    print(f"{ref.spec}: {checked} Dateien geprueft, {len(bad)} fehlerhaft")
    for path in bad:
        print(f"  FEHLERHAFT: {path}")
    return 1 if bad else 0


def cmd_config(args: argparse.Namespace) -> int:
    path = Path(args.config) if args.config else cfgmod.default_config_path()
    if args.action == "init":
        created = cfgmod.write_template(path, args.backup_root or "")
        print(f"Vorlage angelegt: {created}\nBitte 'backup_root' eintragen und mit 'config validate' pruefen.")
        return 0
    config = cfgmod.load(path, require_target=args.action != "show")
    if args.action == "show":
        print(json.dumps(config.data, indent=2, ensure_ascii=False))
        return 0
    sources, notes = cfgmod.resolve_sources(config)
    print(f"Konfiguration in Ordnung: {path}")
    for source in sources:
        print(f"  Quelle {source.name}: {source.path}")
    for note in notes:
        print(f"  Hinweis: {note}")
    return 0


def cmd_task(args: argparse.Namespace) -> int:
    name = scheduler.validate_name(args.name or scheduler.default_task_name())
    if args.action in ("install", "set"):
        config = _load_config(args)  # nur installieren, wenn die Konfiguration gueltig ist
        program, arguments = scheduler.program_command(str(config.path) if config.path else None)
        scheduler.install(name, program, arguments, delay_minutes=args.delay, daily_at=args.daily_at)
        print(f"Aufgabe eingerichtet: {name}\n  Programm: {program} {arguments}\n  Start: {args.delay} Minute(n) nach Anmeldung" + (f" und taeglich {args.daily_at}" if args.daily_at else ""))
        return 0
    if args.action == "status":
        print(json.dumps(scheduler.status(name), indent=2, ensure_ascii=False))
        return 0
    if args.action == "check":
        problems = scheduler.check(name)
        print("Aufgabe in Ordnung." if not problems else "\n".join(problems))
        return 1 if problems else 0
    removed = scheduler.uninstall(name)
    print(f"Aufgabe entfernt: {name}" if removed else f"Aufgabe nicht vorhanden: {name}")
    return 0


def _install_dir() -> str:
    if not getattr(sys, "frozen", False):
        raise updater.UpdateError("Die Programmaktualisierung ist nur in der installierten EXE-Version verfuegbar (nicht beim Start aus dem Quellcode).")
    return str(Path(sys.executable).parent)


def cmd_cleanup(args: argparse.Namespace) -> int:
    config = _load_config(args)
    options = cleanupmod.Options(partial=args.partial, logs=not args.skip_logs)
    try:
        plan = cleanupmod.build_plan(config, options)
    except cleanupmod.CleanupError as error:
        print(f"Fehler: {error}", file=sys.stderr)
        return 1
    print(f"Bereinigung, Ziel: {plan.target}")
    for item in plan.items:
        detail = f"  {item.files} Dateien, {_human(item.bytes)}" if item.bytes is not None else ""
        print(f"  [{cleanupmod.KIND_LABELS[item.kind]}] {item.label}{detail}")
    for note in plan.notes:
        print(f"  Hinweis: {note}")
    if not plan.items:
        print("Nichts zu bereinigen.")
        return 0
    print(f"{len(plan.items)} Eintraege, Stand-Groesse (logisch) {_human(plan.total_bytes)}; der tatsaechlich frei werdende Platz ist wegen gemeinsamer Dateien (Hardlinks) meist kleiner.")
    if args.dry_run:
        print("Nur Vorschau (--dry-run): nichts wurde geloescht.")
        return 0
    if not args.yes and not _ask("Jetzt endgueltig loeschen? (j/N): ", ("j", "ja", "y", "yes")):
        print("Nicht bestaetigt; nichts wurde geloescht. (Mit --yes ohne Rueckfrage.)", file=sys.stderr)
        return 1
    try:
        result = cleanupmod.execute(config, options, echo=None)
    except cleanupmod.CleanupError as error:
        print(f"Fehler: {error}", file=sys.stderr)
        return 1
    print(f"Entfernt: {len(result.removed)}, fehlgeschlagen: {len(result.failed)}")
    for item, reason in result.failed:
        print(f"  NICHT ENTFERNT: {item.label}: {reason}", file=sys.stderr)
    return 1 if result.failed else 0


def cmd_gui(args: argparse.Namespace) -> int:
    from .gui import main_gui

    return main_gui(args.config)


def cmd_update(args: argparse.Namespace) -> int:
    config = cfgmod.load(args.config, require_target=False)
    try:
        if args.action == "rollback":
            print("Wiederhergestellt: " + ", ".join(updater.rollback(_install_dir())))
            return 0
        info = updater.check_for_update(config.update)
        if info is None:
            print(f"SuperPyBackup {__version__} ist aktuell.")
            return 0
        print(f"Neue Version verfuegbar: {info.version} (installiert: {__version__}) {info.page}")
        if args.action == "check":
            return 0

        def confirm(update: updater.UpdateInfo) -> bool:
            if args.yes:
                return True
            return _ask(f"Version {update.version} jetzt herunterladen und installieren? (j/N): ", ("j", "ja", "y", "yes"))

        names = updater.install_update(info, _install_dir(), config.update, confirm=confirm, log=_log(config, quiet=True))
        print(f"Aktualisiert auf {info.version}: {', '.join(names)}")
        return 0
    except updater.UpdateError as error:
        print(f"Update: {error}", file=sys.stderr)
        return 1


# --- Aufbau ------------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="SuperPyBackup", description="Inkrementelle, versionierte Sicherung der Windows-Benutzerordner.")
    parser.add_argument("--config", help="Pfad zur config.json (Standard: %%LOCALAPPDATA%%\\SuperPyBackup\\config.json)")
    parser.add_argument("--version", action="store_true", help="Version anzeigen")
    sub = parser.add_subparsers(dest="command")

    backup = sub.add_parser("backup", help="Sicherung ausfuehren")
    backup.add_argument("--new-snapshot", action="store_true", help="auch bei vorhandenem Tagesstand einen weiteren Stand anlegen")
    backup.add_argument("--quiet", action="store_true", help="keine Ausgabe (fuer die geplante Aufgabe)")
    backup.set_defaults(func=cmd_backup)

    lst = sub.add_parser("list", help="Sicherungsstaende auflisten")
    lst.add_argument("--json", action="store_true")
    lst.set_defaults(func=cmd_list)

    rest = sub.add_parser("restore", help="Dateien oder Ordner wiederherstellen")
    rest.add_argument("--snapshot", default="latest", help="latest, daily/JJJJ-MM-TT, monthly/JJJJ-MM, yearly/JJJJ oder Pfad")
    rest.add_argument("--category", action="append", help="nur diese Kategorie (z. B. Documents); mehrfach moeglich")
    rest.add_argument("--path", action="append", help="nur diese Datei/diesen Ordner, z. B. Documents/Projekt/a.txt; mehrfach moeglich")
    where = rest.add_mutually_exclusive_group(required=True)
    where.add_argument("--dest", help="Zielverzeichnis (Unterordner je Kategorie)")
    where.add_argument("--original", action="store_true", help="am urspruenglichen Speicherort wiederherstellen")
    rest.add_argument("--overwrite", action="store_true", help="vorhandene Dateien ersetzen (nur nach Bestaetigung)")
    rest.add_argument("--yes", action="store_true", help="Ueberschreiben ohne Rueckfrage bestaetigen")
    rest.set_defaults(func=cmd_restore)

    verify = sub.add_parser("verify", help="Integritaet eines Standes pruefen")
    verify.add_argument("--snapshot", default="latest")
    verify.set_defaults(func=cmd_verify)

    config = sub.add_parser("config", help="Konfiguration anlegen/anzeigen/pruefen")
    config.add_argument("action", choices=["init", "show", "validate"])
    config.add_argument("--backup-root", help="bei init: Sicherungsziel eintragen")
    config.set_defaults(func=cmd_config)

    task = sub.add_parser("task", help="Windows-Aufgabe verwalten")
    task.add_argument("action", choices=["install", "set", "status", "check", "uninstall"])
    task.add_argument("--name", help="Aufgabenname (muss mit SuperPyBackup beginnen)")
    task.add_argument("--delay", type=int, default=2, help="Minuten nach der Anmeldung (Standard 2)")
    task.add_argument("--daily-at", help="zusaetzlich taeglich um HH:MM")
    task.set_defaults(func=cmd_task)

    update = sub.add_parser("update", help="Updates pruefen/installieren")
    update.add_argument("action", choices=["check", "install", "rollback"])
    update.add_argument("--yes", action="store_true", help="Installation ohne Rueckfrage bestaetigen")
    update.set_defaults(func=cmd_update)

    cleanup = sub.add_parser("cleanup", help="Sicherungsziel bereinigen (Reste, abgelaufene Staende, alte Protokolle)")
    cleanup.add_argument("--dry-run", action="store_true", help="nur anzeigen, was entfernt wuerde")
    cleanup.add_argument("--yes", action="store_true", help="ohne Rueckfrage loeschen")
    cleanup.add_argument("--partial", action="store_true", help="auch Teil-Staende (Status Partial) entfernen")
    cleanup.add_argument("--skip-logs", action="store_true", help="alte Protokolle nicht entfernen")
    cleanup.set_defaults(func=cmd_cleanup)

    gui = sub.add_parser("gui", help="Grafische Oberflaeche oeffnen (ohne Argumente startet SuperPyBackup.exe ebenfalls die Oberflaeche)")
    gui.set_defaults(func=cmd_gui)
    return parser


def _ensure_streams() -> None:
    """In der konsolenlosen EXE sind stdout/stderr None; durch Null-Streams ersetzen."""
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, io.StringIO())


def _crash_log(error: BaseException) -> None:
    try:
        folder = Path(cfgmod.app_home()) / "Logs"
        folder.mkdir(parents=True, exist_ok=True)
        with open(folder / "crash.log", "a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {''.join(traceback.format_exception(error))}\n")
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    # Konsolenlose EXE (Doppelklick) oder ohne Konsole gestartet: ohne Befehl die Oberflaeche zeigen.
    windowed = sys.stdout is None or (getattr(sys, "frozen", False) and Path(sys.executable).name.lower() == "superpybackup.exe")
    _ensure_streams()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.version:
        print(f"SuperPyBackup {__version__}")
        return 0
    if not getattr(args, "func", None):
        if windowed:
            args.func = cmd_gui
        else:
            parser.print_help()
            return EXIT_USAGE
    try:
        return args.func(args)
    except (cfgmod.ConfigError, scheduler.SchedulerError) as error:
        print(str(error), file=sys.stderr)
        try:
            folder = Path(cfgmod.app_home()) / "Logs"
            folder.mkdir(parents=True, exist_ok=True)
            with open(folder / "errors.log", "a", encoding="utf-8") as handle:
                handle.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {args.command}: {error}\n")
        except OSError:
            pass
        return 1
    except KeyboardInterrupt:
        print("Abgebrochen.", file=sys.stderr)
        return 130
    except Exception as error:  # noqa: BLE001 - letzte Sicherung: auch ohne Konsole nachvollziehbar protokollieren
        _crash_log(error)
        print(f"Unerwarteter Fehler: {error} (Details in {cfgmod.app_home() / 'Logs' / 'crash.log'})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
