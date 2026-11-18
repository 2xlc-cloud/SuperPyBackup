"""Tests der Bereinigung (Modul cleanup, Befehl `cleanup`, Schaltflaeche in der Oberflaeche)."""

from __future__ import annotations

import ctypes
import os
import threading
from datetime import date, datetime, timedelta

import pytest

from conftest import daily_names, write_tree
from superpybackup import cleanup, engine, restore, winapi
from superpybackup.runlog import RunLog

TODAY = date.today()


def backup_days(env, offsets, retention_days=3650):
    """Sicherungen an `TODAY - offset` mit grosser Aufbewahrung, damit die Rotation selbst nichts entfernt."""
    for offset in offsets:
        day = TODAY - timedelta(days=offset)
        write_tree(env.src, {"a.txt": f"stand {offset}"})
        assert env.run(f"{day:%Y-%m-%d} 12:00:00", config_overrides={"retention": {"daily": retention_days}}).status == engine.COMPLETE


def config_of(env):
    return env.config()


def test_plan_lists_expired_snapshots_and_changes_nothing(env):
    backup_days(env, [20, 19, 3, 0])
    before = daily_names(env)
    plan = cleanup.build_plan(config_of(env))
    expired = sorted(item.label for item in plan.items if item.kind == cleanup.EXPIRED and item.label.startswith("daily/"))
    assert expired == sorted(f"daily/{TODAY - timedelta(days=d):%Y-%m-%d}" for d in (20, 19))
    assert all(item.bytes for item in plan.items if item.kind == cleanup.EXPIRED)
    assert daily_names(env) == before  # die Vorschau loescht nichts


def test_execute_removes_only_expired_and_keeps_the_rest_readable(env):
    backup_days(env, [20, 19, 3, 0])
    result = cleanup.execute(config_of(env))
    assert result.failed == [] and {item.kind for item in result.removed} == {cleanup.EXPIRED}
    assert daily_names(env) == [f"{TODAY - timedelta(days=3):%Y-%m-%d}", f"{TODAY:%Y-%m-%d}"]
    for name in daily_names(env):
        assert restore.verify_snapshot(str(env.snapshot("Daily", name)), RunLog(str(env.home / "Logs")), env.identity.sid)[1] == []
    assert cleanup.build_plan(config_of(env)).items == []  # zweiter Lauf: nichts mehr zu tun


def test_newest_complete_snapshot_is_never_removed(env):
    backup_days(env, [40])  # einziger Stand, weit ausserhalb der Aufbewahrung
    plan = cleanup.build_plan(config_of(env))
    assert [item for item in plan.items if item.kind == cleanup.EXPIRED] == []
    assert cleanup.execute(config_of(env)).removed == []
    assert len(daily_names(env)) == 1


def test_stale_work_directories_are_removed(env):
    backup_days(env, [0])
    for name in (".incomplete-abc123", ".deleting-def456"):
        folder = env.target / name
        (folder / "x").mkdir(parents=True)
        (folder / "x" / "datei.txt").write_text("rest")
    plan = cleanup.build_plan(config_of(env))
    assert sorted(item.label for item in plan.items if item.kind == cleanup.STALE) == [".deleting-def456", ".incomplete-abc123"]
    result = cleanup.execute(config_of(env))
    assert len(result.removed) == 2 and not (env.target / ".incomplete-abc123").exists() and not (env.target / ".deleting-def456").exists()
    assert len(daily_names(env)) == 1


def test_broken_snapshot_is_reported_but_never_deleted(env):
    backup_days(env, [20, 0])
    broken = env.snapshot("Daily", f"{TODAY - timedelta(days=20):%Y-%m-%d}")
    (broken / ".snapshot.json").unlink()
    plan = cleanup.build_plan(config_of(env))
    assert any("kein gueltiges Manifest" in note for note in plan.notes)
    assert all(item.path != str(broken) for item in plan.items)
    cleanup.execute(config_of(env))
    assert broken.exists()


def test_partial_snapshots_only_with_explicit_option(env):
    from test_backup_core import _lock_exclusively

    write_tree(env.src, {"frei.txt": "frei", "gesperrt.txt": "x"})
    handle = _lock_exclusively(str(env.src / "gesperrt.txt"))
    try:
        partial = env.run(f"{TODAY - timedelta(days=1):%Y-%m-%d} 12:00:00", config_overrides={"retention": {"daily": 3650}})
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)
    assert partial.status == engine.PARTIAL
    backup_days(env, [0])
    assert [i for i in cleanup.build_plan(config_of(env)).items if i.kind == cleanup.PARTIAL] == []
    plan = cleanup.build_plan(config_of(env), cleanup.Options(partial=True))
    assert [i.path for i in plan.items if i.kind == cleanup.PARTIAL] == [partial.snapshot_path]
    result = cleanup.execute(config_of(env), cleanup.Options(partial=True))
    assert result.failed == [] and not os.path.exists(partial.snapshot_path)
    assert f"{TODAY:%Y-%m-%d}" in daily_names(env)


def test_old_logs_are_removed_and_todays_log_kept(env):
    backup_days(env, [0])
    logs = env.home / "Logs"
    for name in ("2020-01-01.log", "restore-2020-01-01.log"):
        (logs / name).write_text("alt", encoding="utf-8")
    plan = cleanup.build_plan(config_of(env))
    assert sorted(i.label for i in plan.items if i.kind == cleanup.LOGS) == ["2020-01-01.log", "restore-2020-01-01.log"]
    cleanup.execute(config_of(env))
    assert not (logs / "2020-01-01.log").exists() and not (logs / "restore-2020-01-01.log").exists()
    assert (logs / f"{datetime.now():%Y-%m-%d}.log").exists()
    assert cleanup.build_plan(config_of(env), cleanup.Options(logs=False)).items == []


def test_cleanup_refuses_while_a_backup_holds_the_lock(env):
    backup_days(env, [20, 0])
    lock = engine.TargetLock(str(env.target / ".backup.lock"))
    assert lock.acquire()
    try:
        with pytest.raises(cleanup.CleanupError, match="anderen Lauf"):
            cleanup.execute(config_of(env))
    finally:
        lock.release()
    assert len(daily_names(env)) == 2  # nichts wurde geloescht
    mutex = winapi.NamedMutex("Local\\SuperPyBackup-" + env.identity.sid)
    assert mutex.try_acquire()
    outcome: list[BaseException | None] = []

    def other_thread() -> None:  # ein Windows-Mutex gehoert dem haltenden Thread; ein zweiter Thread verhaelt sich wie ein zweiter Prozess
        try:
            cleanup.execute(config_of(env))
            outcome.append(None)
        except BaseException as error:  # noqa: BLE001
            outcome.append(error)

    try:
        worker = threading.Thread(target=other_thread)
        worker.start()
        worker.join(30)
    finally:
        mutex.close()
    assert len(outcome) == 1 and isinstance(outcome[0], cleanup.CleanupError) and "Sicherung laeuft" in str(outcome[0])
    assert len(daily_names(env)) == 2


def test_missing_target_is_reported_clearly(env):
    with pytest.raises(cleanup.CleanupError, match="keinen Ordner"):
        cleanup.build_plan(config_of(env))


# Konsole -------------------------------------------------------------------------------------
def run_cli(env, *args):
    env.write_config_file()
    return env.cli("cleanup", *args, extra_env={"PYTHONIOENCODING": "utf-8"})


def test_cli_dry_run_shows_plan_and_deletes_nothing(env):
    backup_days(env, [20, 19, 0])
    result = run_cli(env, "--dry-run")
    assert result.returncode == 0 and "Abgelaufener Stand" in result.stdout and "Nur Vorschau" in result.stdout
    assert len(daily_names(env)) == 3


def test_cli_without_confirmation_refuses_and_deletes_nothing(env):
    backup_days(env, [20, 0])
    result = run_cli(env)  # kein Terminal, kein --yes
    assert result.returncode == 1 and "Nicht bestaetigt" in result.stderr
    assert len(daily_names(env)) == 2


def test_cli_yes_removes_and_reports(env):
    backup_days(env, [20, 19, 0])
    result = run_cli(env, "--yes")
    assert result.returncode == 0 and "Entfernt: 2" in result.stdout, result.stdout + result.stderr
    assert daily_names(env) == [f"{TODAY:%Y-%m-%d}"]
    nothing = run_cli(env, "--yes")
    assert nothing.returncode == 0 and "Nichts zu bereinigen" in nothing.stdout


def test_cli_reports_missing_target(env):
    result = run_cli(env, "--dry-run")
    assert result.returncode == 1 and "keinen Ordner" in result.stderr
