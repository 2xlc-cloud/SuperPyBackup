"""Kommandozeile von SuperPyBackup (auch in der konsolenlosen SuperPyBackup.exe nutzbar)."""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

from . import __version__, cleanup as cleanupmod, config as cfgmod, credstore, engine, fsutil, jobs as jobsmod, netshare, notify as notifymod, restore as restoremod, scheduler, shortcut as shortcutmod, snapshots, staleness, updater, vss
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
    return config.target_dir(identity), identity.sid


def _log(config: cfgmod.Config, *, quiet: bool = False) -> RunLog:
    identity = get_identity()
    return RunLog(config.log_dir, user=identity.sid, target=config.backup_root, max_bytes=config.log_max_bytes, echo=None if quiet else sys.stdout)


# --- Befehle -----------------------------------------------------------------------------------

def _base_config(args: argparse.Namespace) -> str:
    """Pfad der Hauptkonfiguration (auch wenn --job eine weitere Sicherung gewaehlt hat)."""
    return getattr(args, "base_config", None) or args.config or str(cfgmod.default_config_path())


def cmd_backup(args: argparse.Namespace) -> int:
    if not getattr(args, "all", False):
        return _backup_one(args)
    if getattr(args, "job", None):
        print("--all und --job schliessen sich aus.", file=sys.stderr)
        return EXIT_USAGE
    worst = 0
    base = _base_config(args)
    for job in jobsmod.list_jobs(base):
        if job.standard and not job.path.is_file():
            continue  # keine Hauptkonfiguration: nur die weiteren Sicherungen
        if not args.quiet:
            print(f"\n=== Sicherung {job.name} ===")
        args.config = str(job.path)
        try:
            worst = max(worst, _backup_one(args))
        except cfgmod.ConfigError as error:  # eine fehlerhafte Konfiguration haelt die uebrigen Sicherungen nicht auf
            print(f"Sicherung {job.name}: {str(error).splitlines()[0]}", file=sys.stderr)
            notifymod.notify_from_file(str(job.path), notifymod.ERROR, f"Sicherung nicht gestartet ({job.name})", str(error).splitlines()[0][:300])
            worst = max(worst, 1)
    return worst


def _backup_one(args: argparse.Namespace) -> int:
    config = _load_config(args)
    mode = engine.FULL if args.full else engine.INCREMENTAL if args.incremental else None
    result = engine.run_backup(config, new_snapshot=True if args.new_snapshot else None, mode=mode, echo=None if args.quiet else sys.stdout)
    stats = result.stats
    if not args.quiet:
        print(f"\nStatus: {result.status}" + (f" - {result.message}" if result.message else ""))
        if result.snapshot_path:
            print(f"Stand: {result.snapshot_path}")
        if result.kind:
            print("Art: " + ("Vollsicherung" if result.kind == engine.FULL else "Inkrement"))
        print(f"Gescannt {stats.scanned} | neu {stats.new} | geaendert {stats.modified} | unveraendert {stats.unchanged} | ausgelassen {stats.skipped} | Fehler {stats.errors}")
        print(f"Uebertragen {_human(stats.bytes_transferred)} | Gesamt {_human(stats.total_bytes)} | zusaetzlicher Speicher {_human(stats.extra_storage_bytes)} | {result.seconds:.1f} s")
    message = notifymod.backup_message(result, {engine.FULL: "Vollsicherung", engine.INCREMENTAL: "Inkrement"}.get(result.kind, ""), config.job)
    if message:
        notifymod.notify(config.notifications, *message, log=_safe_log(config))
    if result.status not in (engine.COMPLETE, engine.ALREADY):
        staleness.check(config, _safe_log(config))  # Lauf nicht erfolgreich: lange ausbleibende Sicherung melden
    if result.status != engine.BUSY and not getattr(args, "all", False):
        try:
            log = _log(config, quiet=True)
            info = updater.periodic_check(config, log)
            if info and _apply_forced_update(config, info, log, args.quiet):
                pass
            elif info:
                _notify_new_version(config, info, log)
            if info and not info.forced and not args.quiet:
                print(f"Neue Version verfuegbar: {info.version}. Installation mit 'update install'.")
        except Exception:  # noqa: BLE001 - die Update-Pruefung darf einen Lauf nie beeintraechtigen
            pass
    return result.exit_code


def _apply_forced_update(config: cfgmod.Config, info: updater.UpdateInfo | None, log: RunLog | None, quiet: bool) -> bool:
    """Pflichtupdate (mehr als update.force_after_versions Versionen zurueck): installiert ohne Rueckfrage. True = installiert."""
    if info is None or not info.forced:
        return False
    try:
        names = updater.run_forced_update(config, info, log)
    except updater.UpdateError as error:
        notifymod.notify(config.notifications, notifymod.ERROR, "Pflichtupdate fehlgeschlagen", f"Update auf {info.version} nicht installiert: {error}"[:300], log)
        if not quiet:
            print(f"Pflichtupdate auf {info.version} fehlgeschlagen (bisherige Installation unveraendert): {error}", file=sys.stderr)
        return False
    if not names:
        return False
    notifymod.notify(config.notifications, notifymod.UPDATE, "SuperPyBackup aktualisiert (Pflichtupdate)", f"Version {info.version} installiert (vorher {__version__}, {info.behind} Versionen zurueck). Bitte die Oberflaeche neu starten.", log)
    if not quiet:
        print(f"Pflichtupdate installiert: {', '.join(names)} -> Version {info.version}")
    return True


def _notify_new_version(config: cfgmod.Config, info: updater.UpdateInfo, log: RunLog | None) -> None:
    """Desktop-Meldung zu einer neuen Version, hoechstens einmal je Version."""
    if updater.already_notified(info.version):
        return
    if notifymod.notify(config.notifications, notifymod.UPDATE, "Neue Version von SuperPyBackup", f"Version {info.version} ist verfuegbar (installiert: {__version__}). Installation: SuperPyBackupCmd update install", log):
        updater.remember_notified(info.version)


def _safe_log(config: cfgmod.Config) -> RunLog | None:
    try:
        return _log(config, quiet=True)
    except Exception:  # noqa: BLE001
        return None


def _warn_overdue_jobs(args: argparse.Namespace) -> None:
    """Warnt fuer jede Sicherung (Standard und weitere), deren letzter Erfolg zu lange zurueckliegt. Nie eine Ausnahme."""
    try:
        for job in jobsmod.list_jobs(_base_config(args)):
            if job.standard and not job.path.is_file():
                continue
            try:
                config = cfgmod.load(str(job.path), require_target=False)
            except cfgmod.ConfigError:
                continue
            staleness.check(config, _safe_log(config))
    except Exception:  # noqa: BLE001
        pass


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


def cmd_find(args: argparse.Namespace) -> int:
    config = _load_config(args)
    target, sid = _target_dir(config)
    try:
        versions = restoremod.find_versions(snapshots.list_snapshots(target, sid), args.pattern, limit=args.limit)
    except restoremod.RestoreError as error:
        print(f"Fehler: {error}", file=sys.stderr)
        return 1
    if not versions:
        print("Keine passende Datei in den Sicherungsstaenden gefunden.")
        return 1
    for version in versions:
        also = f" (+{len(version.also_in)} weitere Staende mit derselben Fassung)" if version.also_in else ""
        print(f"{version.path} | {version.ref.spec} | {_human(version.entry.size)} | geaendert {time.strftime('%d.%m.%Y %H:%M', time.localtime(version.entry.mtime_ns / 1e9))}{also}")
    print(f"\n{len(versions)} Fassung(en). Wiederherstellen: restore --snapshot <Stand> --path <Pfad> --dest <Ordner>")
    return 0


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
    run_all = getattr(args, "all", False)
    if run_all and getattr(args, "job", None):
        print("--all und --job schliessen sich aus.", file=sys.stderr)
        return EXIT_USAGE
    if run_all:
        name = scheduler.validate_name(args.name or jobsmod.all_task_name(scheduler.default_task_name()))
    else:
        job = cfgmod.job_of(args.config)
        name = scheduler.validate_name(args.name or jobsmod.task_name(scheduler.default_task_name(), job))
    if args.action in ("install", "set"):
        if run_all:
            base = _base_config(args)
            valid = [job for job in jobsmod.list_jobs(base) if job.path.is_file()]
            if not valid:
                raise cfgmod.ConfigError("Es gibt noch keine Sicherung mit Konfiguration.")
            for job in valid:  # nur installieren, wenn alle Konfigurationen gueltig sind
                cfgmod.load(job.path)
            program, arguments = scheduler.program_command(base, "backup --all --quiet")
        else:
            config = _load_config(args)  # nur installieren, wenn die Konfiguration gueltig ist
            program, arguments = scheduler.program_command(str(config.path) if config.path else None)
        elevated = bool(getattr(args, "elevated", False))
        if elevated and not vss.is_admin():
            print("Eine Aufgabe mit hoechsten Rechten (fuer Schattenkopien/VSS) kann nur aus einer als Administrator gestarteten Eingabeaufforderung eingerichtet werden.", file=sys.stderr)
            return 1
        scheduler.install(name, program, arguments, delay_minutes=args.delay, daily_at=args.daily_at, elevated=elevated)
        print(f"Aufgabe eingerichtet: {name}" + (" (alle Sicherungen nacheinander)" if run_all else "") + f"\n  Programm: {program} {arguments}\n  Start: {args.delay} Minute(n) nach Anmeldung" + (f" und taeglich {args.daily_at}" if args.daily_at else "") + ("\n  Rechte: hoechste verfuegbare (Schattenkopien fuer gesperrte Dateien moeglich)" if elevated else ""))
        return 0
    if args.action == "status":
        print(json.dumps(scheduler.status(name), indent=2, ensure_ascii=False))
        return 0
    if args.action == "check":
        problems = scheduler.check(name, allow_elevated=bool(getattr(args, "elevated", False)))
        print("Aufgabe in Ordnung." if not problems else "\n".join(problems))
        return 1 if problems else 0
    removed = scheduler.uninstall(name)
    print(f"Aufgabe entfernt: {name}" if removed else f"Aufgabe nicht vorhanden: {name}")
    return 0


def cmd_job(args: argparse.Namespace) -> int:
    base = _base_config(args)
    try:
        if args.action == "list":
            for job in jobsmod.list_jobs(base):
                state = "" if job.path.is_file() else "  (noch keine Konfiguration)"
                print(f"{job.name:<24}{job.path}{state}")
            return 0
        if not args.name:
            print("Bitte den Namen der Sicherung angeben.", file=sys.stderr)
            return EXIT_USAGE
        if args.action == "add":
            source = jobsmod.find(base, args.copy_from).path if args.copy_from else None
            job = jobsmod.create(base, args.name, source)
            print(f"Sicherung angelegt: {job.name}\n  Konfiguration: {job.path}\n  Bitte Sicherungsziel und Quellen anpassen (--job {job.name} config show) und dann mit --job {job.name} backup sichern.")
            return 0
        job = jobsmod.find(base, args.name)
        if job.standard:
            print("Die Standard-Sicherung kann nicht entfernt werden.", file=sys.stderr)
            return 1
        try:
            scheduler.uninstall(jobsmod.task_name(scheduler.default_task_name(), job.name))
        except scheduler.SchedulerError as error:
            print(f"Hinweis: Aufgabe nicht entfernt: {error}", file=sys.stderr)
        jobsmod.remove(base, job.name)
        print(f"Sicherung entfernt: {job.name} (nur die Konfiguration und ihre Aufgabe; bereits gesicherte Daten im Ziel bleiben erhalten)")
        return 0
    except cfgmod.ConfigError as error:
        print(str(error), file=sys.stderr)
        return 1


def _read_password(args: argparse.Namespace) -> str:
    """Kennwort nie als Argument (waere in Prozessliste und Verlauf sichtbar): aus stdin oder per Eingabeaufforderung."""
    if args.password_stdin:
        return (sys.stdin.readline() if sys.stdin else "").rstrip("\r\n")
    if sys.stdin is not None and sys.stdin.isatty():
        import getpass

        return getpass.getpass(f"Kennwort fuer {args.user}: ")
    raise credstore.CredentialError("Ohne Terminal bitte --password-stdin verwenden und das Kennwort ueber die Standardeingabe uebergeben.")


def _print_test(result: netshare.TestResult) -> None:
    print(result.text)
    print("Test erfolgreich." if result.ok else "Test fehlgeschlagen.")


def cmd_credentials(args: argparse.Namespace) -> int:
    try:
        if args.action == "list":
            entries = credstore.list_entries()
            if not entries:
                print("Keine Anmeldedaten hinterlegt.")
            for share, user in entries:
                print(f"{share:<40}{user}   (Kennwort verschluesselt, nur fuer diesen Windows-Benutzer lesbar)")
            return 0
        config = None
        if not args.share and (args.config or cfgmod.default_config_path().is_file()):
            config = cfgmod.load(args.config, require_target=False)
        target = args.share or (config.backup_root if config else "")
        if not target:
            print("Bitte --share mit Server und Freigabe angeben (oder in der Konfiguration ein Sicherungsziel eintragen).", file=sys.stderr)
            return EXIT_USAGE
        if args.action == "remove":
            removed = credstore.remove(target)
            netshare.reset(target)
            print("Anmeldedaten entfernt." if removed else "Es waren keine Anmeldedaten hinterlegt.")
            return 0
        if args.action == "set":
            if not args.user:
                print("Bitte --user angeben.", file=sys.stderr)
                return EXIT_USAGE
            password = _read_password(args)
            key = credstore.save(target, args.user, password)
            netshare.reset(target)
            print(f"Anmeldedaten gespeichert fuer {key} (Benutzer {args.user}, Kennwort verschluesselt).")
            if args.no_test:
                return 0
            result = netshare.test_target(target)
            _print_test(result)
            return 0 if result.ok else 1
        # test
        if args.user:
            result = netshare.test_target(target, args.user, _read_password(args))
        else:
            result = netshare.test_target(target)
        _print_test(result)
        return 0 if result.ok else 1
    except credstore.CredentialError as error:
        print(str(error), file=sys.stderr)
        return 1


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


def cmd_notify_test(args: argparse.Namespace) -> int:
    config = cfgmod.load(args.config, require_target=False)
    state = "eingeschaltet" if config.notifications["enabled"] else "AUSGESCHALTET (notifications.enabled)"
    print(f"Desktop-Meldungen sind in der Konfiguration {state}.")
    if notifymod.show("SuperPyBackup", "Testmeldung: Desktop-Meldungen funktionieren."):
        print("Testmeldung abgesetzt.")
        return 0
    print("Die Testmeldung konnte nicht angezeigt werden.", file=sys.stderr)
    return 1


def cmd_shortcut(args: argparse.Namespace) -> int:
    try:
        if args.action == "create":
            config_path = args.config or str(cfgmod.default_config_path())
            print(f"Verknuepfung angelegt: {shortcutmod.create(config_path)}")
        elif args.action == "remove":
            print("Verknuepfung entfernt." if shortcutmod.remove() else "Es war keine Verknuepfung vorhanden.")
        else:
            current = shortcutmod.read()
            print("Keine Verknuepfung auf dem Desktop." if current is None else f"Verknuepfung: {shortcutmod.link_path()}\nZiel: {current[0]} {current[1]}")
        return 0
    except shortcutmod.ShortcutError as error:
        print(str(error), file=sys.stderr)
        return 1


def cmd_gui(args: argparse.Namespace) -> int:
    from .gui import main_gui

    return main_gui(_base_config(args) if (args.config or getattr(args, "job", None)) else None, tray_start=getattr(args, "tray", False), job=getattr(args, "job", None))


def cmd_update(args: argparse.Namespace) -> int:
    config = cfgmod.load(args.config, require_target=False)
    try:
        if args.action == "rollback":
            print("Wiederhergestellt: " + ", ".join(updater.rollback(_install_dir())))
            return 0
        info = updater.check_for_update(config.update)
        if args.quiet:  # geplante Pruefung: still, nur Desktop-Meldung bei neuer Version und Eintrag im Protokoll
            log = _safe_log(config)
            _warn_overdue_jobs(args)  # die taegliche Aufgabe prueft auch, ob eine Sicherung zu lange ausbleibt
            if info is None:
                if log:
                    log.info(f"Updatepruefung: SuperPyBackup {__version__} ist aktuell.")
                return 0
            if log:
                log.info(f"Updatepruefung: neue Version {info.version} verfuegbar (installiert {__version__}, {info.behind} Version(en) zurueck).")
            if not _apply_forced_update(config, info, log, True):
                _notify_new_version(config, info, log)
            return 0
        if info is None:
            print(f"SuperPyBackup {__version__} ist aktuell.")
            return 0
        print(f"Neue Version verfuegbar: {info.version} (installiert: {__version__}) {info.page}")
        if info.forced:
            print(f"Hinweis: Die installierte Version ist {info.behind} Versionen alt (Grenze: mehr als {config.update['force_after_versions']}). Das Pflichtupdate wird bei der naechsten automatischen Pruefung ohne Rueckfrage installiert.")
        if args.action == "check":
            return 0

        def confirm(update: updater.UpdateInfo) -> bool:
            if args.yes:
                return True
            return _ask(f"Version {update.version} jetzt herunterladen und installieren? (j/N): ", ("j", "ja", "y", "yes"))

        names = updater.install_update(info, _install_dir(), config.update, confirm=confirm, log=_log(config, quiet=True))
        print(f"Aktualisiert auf {info.version}: {', '.join(names)}")
        notifymod.notify(config.notifications, notifymod.UPDATE, "SuperPyBackup aktualisiert", f"Version {info.version} ist installiert.", _safe_log(config))
        return 0
    except updater.UpdateError as error:
        print(f"Update: {error}", file=sys.stderr)
        if args.quiet:
            log = _safe_log(config)
            if log:
                log.warn(f"Updatepruefung nicht moeglich: {error}")
        return 1


# --- Aufbau ------------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="SuperPyBackup", description="Inkrementelle, versionierte Sicherung der Windows-Benutzerordner.")
    parser.add_argument("--config", help="Pfad zur config.json (Standard: %%LOCALAPPDATA%%\\SuperPyBackup\\config.json)")
    parser.add_argument("--job", help="Name der Sicherung (Standard: die Hauptsicherung); weitere Sicherungen verwaltet der Befehl 'job'")
    parser.add_argument("--version", action="store_true", help="Version anzeigen")
    sub = parser.add_subparsers(dest="command")

    backup = sub.add_parser("backup", help="Sicherung ausfuehren")
    backup.add_argument("--new-snapshot", action="store_true", help="auch bei vorhandenem Tagesstand einen weiteren Stand anlegen")
    kind_group = backup.add_mutually_exclusive_group()
    kind_group.add_argument("--full", action="store_true", help="Vollsicherung erzwingen: alle Dateien neu kopieren, eigenstaendiger Stand (legt auch bei vorhandenem Tagesstand einen weiteren an)")
    kind_group.add_argument("--incremental", action="store_true", help="Inkrement erzwingen: nur Geaendertes kopieren, Unveraendertes per Hardlink (ohne Vorgaenger wird trotzdem voll gesichert)")
    backup.add_argument("--quiet", action="store_true", help="keine Ausgabe (fuer die geplante Aufgabe)")
    backup.add_argument("--all", action="store_true", help="alle Sicherungen nacheinander ausfuehren (eine fehlerhafte haelt die uebrigen nicht auf)")
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

    find = sub.add_parser("find", help="Datei in allen Sicherungsstaenden suchen (jede unterschiedliche Fassung)")
    find.add_argument("pattern", help="Teil des Pfades oder Muster mit * und ?, z. B. rechnung oder *.docx")
    find.add_argument("--limit", type=int, default=200, help="hoechstens so viele Fassungen anzeigen (Standard 200)")
    find.set_defaults(func=cmd_find)

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
    task.add_argument("--all", action="store_true", help="Aufgabe fuer alle Sicherungen (fuehrt 'backup --all' aus) statt fuer die gewaehlte Sicherung")
    task.add_argument("--delay", type=int, default=2, help="Minuten nach der Anmeldung (Standard 2)")
    task.add_argument("--daily-at", help="zusaetzlich taeglich um HH:MM")
    task.add_argument("--elevated", action="store_true", help="Aufgabe mit hoechsten Rechten (Administrator) einrichten bzw. pruefen: noetig, damit Schattenkopien (VSS) gesperrte Dateien sichern koennen")
    task.set_defaults(func=cmd_task)

    job = sub.add_parser("job", help="Mehrere Sicherungen verwalten (anzeigen, anlegen, entfernen)")
    job.add_argument("action", choices=["list", "add", "remove"])
    job.add_argument("name", nargs="?", help="Name der Sicherung (Buchstaben, Ziffern, . _ -)")
    job.add_argument("--copy-from", help="bei add: Einstellungen einer vorhandenen Sicherung uebernehmen (z. B. Standard)")
    job.set_defaults(func=cmd_job)

    credentials = sub.add_parser("credentials", help="Anmeldedaten fuer ein Netzwerkziel (UNC-Freigabe) speichern, anzeigen, entfernen, testen")
    credentials.add_argument("action", choices=["set", "list", "remove", "test"])
    credentials.add_argument("--share", help="Freigabe oder Zielpfad (Standard: das Sicherungsziel der Konfiguration)")
    credentials.add_argument("--user", help="Benutzername (z. B. benutzer, NAS-name\\benutzer oder benutzer@domain)")
    credentials.add_argument("--password-stdin", action="store_true", help="Kennwort aus der Standardeingabe lesen (nie als Argument uebergeben)")
    credentials.add_argument("--no-test", action="store_true", help="bei set: nach dem Speichern nicht testen")
    credentials.set_defaults(func=cmd_credentials)

    update = sub.add_parser("update", help="Updates pruefen/installieren")
    update.add_argument("action", choices=["check", "install", "rollback"])
    update.add_argument("--yes", action="store_true", help="Installation ohne Rueckfrage bestaetigen")
    update.add_argument("--quiet", action="store_true", help="nur mit check: keine Ausgabe (fuer die geplante Aufgabe); meldet eine neue Version per Desktop-Meldung")
    update.set_defaults(func=cmd_update)

    cleanup = sub.add_parser("cleanup", help="Sicherungsziel bereinigen (Reste, abgelaufene Staende, alte Protokolle)")
    cleanup.add_argument("--dry-run", action="store_true", help="nur anzeigen, was entfernt wuerde")
    cleanup.add_argument("--yes", action="store_true", help="ohne Rueckfrage loeschen")
    cleanup.add_argument("--partial", action="store_true", help="auch Teil-Staende (Status Partial) entfernen")
    cleanup.add_argument("--skip-logs", action="store_true", help="alte Protokolle nicht entfernen")
    cleanup.set_defaults(func=cmd_cleanup)

    notify_test = sub.add_parser("notify-test", help="Testmeldung auf dem Desktop anzeigen")
    notify_test.set_defaults(func=cmd_notify_test)

    shortcut = sub.add_parser("shortcut", help="Verknuepfung auf dem Desktop anlegen/entfernen/anzeigen")
    shortcut.add_argument("action", choices=["create", "remove", "status"])
    shortcut.set_defaults(func=cmd_shortcut)

    gui = sub.add_parser("gui", help="Grafische Oberflaeche oeffnen (ohne Argumente startet SuperPyBackup.exe ebenfalls die Oberflaeche)")
    gui.add_argument("--tray", action="store_true", help="nur als Symbol im Infobereich starten (Autostart); ohne Konfiguration wird das Fenster gezeigt")
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
        args.base_config = args.config
        if args.job and args.command not in ("job", "gui"):
            args.config = str(jobsmod.find(_base_config(args), args.job).path)
        return args.func(args)
    except (cfgmod.ConfigError, scheduler.SchedulerError) as error:
        print(str(error), file=sys.stderr)
        if args.command == "backup":
            notifymod.notify_from_file(args.config, notifymod.ERROR, "Sicherung nicht gestartet", str(error).splitlines()[0][:300])
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
        if args.command == "backup":
            notifymod.notify_from_file(args.config, notifymod.ERROR, "Sicherung abgebrochen", f"Unerwarteter Fehler: {error}"[:300])
        print(f"Unerwarteter Fehler: {error} (Details in {cfgmod.app_home() / 'Logs' / 'crash.log'})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
