"""Tests 14, 15, 17, 18: Abbruch, Neustart, parallele Laeufe, defekte Metadaten."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

from conftest import ROOT, daily_names, read_tree, tier_names, tree_digest, write_tree
from superpybackup import engine, fsutil, manifest as mf, snapshots, winapi


def many_files(env, count=30, prefix="datei"):
    write_tree(env.src, {f"ordner/{prefix}{index:03d}.txt": f"inhalt {index}" * 50 for index in range(count)})


def stage_dirs(env):
    return [p for p in env.target.iterdir() if p.name.startswith(".incomplete")] if env.target.exists() else []


# 14 -----------------------------------------------------------------------------------------
def test_abort_during_backup_leaves_existing_snapshots_untouched(env, monkeypatch):
    many_files(env, 10)
    env.run("2026-03-10 12:00:00")
    before = tree_digest(env.target / "Daily")
    write_tree(env.src, {"ordner/neu.txt": "neu"})
    (env.src / "ordner" / "datei001.txt").write_text("geaendert", encoding="utf-8")
    real = fsutil.copy_file_hashed
    calls = {"n": 0}

    def interrupted(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt  # Benutzerabbruch / Prozessende mitten im Lauf
        return real(*args, **kwargs)

    monkeypatch.setattr(fsutil, "copy_file_hashed", interrupted)
    with pytest.raises(KeyboardInterrupt):
        env.run("2026-03-11 12:00:00")
    monkeypatch.undo()
    assert stage_dirs(env) == []  # Arbeitsordner entfernt
    assert daily_names(env) == ["2026-03-10"] and tree_digest(env.target / "Daily") == before
    # Sperren wurden freigegeben: der naechste Lauf funktioniert sofort
    assert env.run("2026-03-11 12:00:00").status == engine.COMPLETE


# 15 -----------------------------------------------------------------------------------------
def _start_slow_backup(env, delay_ms: int = 150):
    env_vars = dict(os.environ, PYTHONPATH=str(ROOT / "src"), SUPERPYBACKUP_HOME=str(env.home), SUPERPYBACKUP_TEST_DELAY_MS=str(delay_ms))
    return subprocess.Popen([sys.executable, "-m", "superpybackup", "--config", str(env.root / "config.json"), "backup", "--quiet"], env=env_vars, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _wait_for(predicate, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_killed_backup_is_cleaned_up_and_next_run_succeeds(env):
    many_files(env, 40)
    env.write_config_file()
    env.run("2026-03-01 12:00:00")  # gueltiger Vorgaengerstand (Datum weit zurueck, wird nicht bereinigt)
    before = tree_digest(env.target / "Daily" / "2026-03-01")
    (env.src / "ordner" / "datei005.txt").write_text("neu", encoding="utf-8")
    process = _start_slow_backup(env)
    try:
        assert _wait_for(lambda: bool(stage_dirs(env))), "Arbeitsordner wurde nicht angelegt"
        time.sleep(0.5)
        process.kill()  # harter Abbruch wie Stromausfall: keine Aufraeumarbeiten moeglich
    finally:
        process.wait(timeout=30)
        process.stdout.close()
        process.stderr.close()
    leftover = stage_dirs(env)
    assert leftover, "Nach hartem Abbruch muss ein Arbeitsordner uebrig sein"
    assert daily_names(env) == ["2026-03-01"]  # unvollstaendiger Lauf ist kein Wiederherstellungspunkt
    assert tree_digest(env.target / "Daily" / "2026-03-01") == before
    result = env.run("2026-03-02 12:00:00")
    assert result.status == engine.COMPLETE
    assert stage_dirs(env) == []
    assert "2026-03-02" in daily_names(env)
    assert tree_digest(env.target / "Daily" / "2026-03-01") == before
    log_text = "".join(p.read_text(encoding="utf-8") for p in (env.home / "Logs").glob("*.log"))
    assert "abgebrochenen Laufs entfernt" in log_text


# 17 -----------------------------------------------------------------------------------------
def test_parallel_runs_second_one_is_busy(env):
    many_files(env, 30)
    env.write_config_file()
    first = _start_slow_backup(env)
    try:
        assert _wait_for(lambda: bool(stage_dirs(env)))
        second = env.cli("backup")
        assert second.returncode == 0 and "Busy" in second.stdout
        out, err = first.communicate(timeout=120)
    finally:
        if first.poll() is None:
            first.kill()
            first.wait()
    assert first.returncode == 0, err
    assert daily_names(env) == [time.strftime("%Y-%m-%d")]  # genau ein Stand, keine Vermischung
    assert stage_dirs(env) == []


def test_busy_when_local_mutex_is_held(env):
    write_tree(env.src, {"a.txt": "a"})
    with winapi.NamedMutex("Local\\SuperPyBackup-" + env.identity.sid) as mutex:
        assert mutex.try_acquire()
        # Der Mutex gehoert diesem Thread; ein zweiter Besitzer im selben Thread wuerde rekursiv erwerben.
        process = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, r'%s'); import os; os.environ['SUPERPYBACKUP_HOME']=r'%s'; from superpybackup import config, engine; import json; cfg = config.load(r'%s'); r = engine.run_backup(cfg); print(r.status)" % (ROOT / "src", env.home, env.write_config_file())], capture_output=True, text=True)
    assert process.stdout.strip() == "Busy", process.stderr
    assert daily_names(env) == []


def test_busy_when_target_lock_is_held(env):
    write_tree(env.src, {"a.txt": "a"})
    env.run("2026-03-01 12:00:00")
    lock = engine.TargetLock(str(env.target / ".backup.lock"))
    assert lock.acquire()
    try:
        result = env.run("2026-03-02 12:00:00")
    finally:
        lock.release()
    assert result.status == engine.BUSY and daily_names(env) == ["2026-03-01"]
    assert env.run("2026-03-02 12:00:00").status == engine.COMPLETE  # nach Freigabe wieder moeglich


# 18 -----------------------------------------------------------------------------------------
def test_corrupt_manifest_of_todays_stand_is_kept_and_new_stand_gets_unique_name(env):
    write_tree(env.src, {"a.txt": "a"})
    env.run("2026-03-10 08:00:00")
    manifest_path = env.snapshot("Daily", "2026-03-10") / ".snapshot.json"
    fsutil.make_writable(str(manifest_path))
    manifest_path.write_text("{ das ist kein json", encoding="utf-8")
    result = env.run("2026-03-10 17:00:00")
    assert result.status == engine.COMPLETE
    names = daily_names(env)
    assert "2026-03-10" in names and len(names) == 2  # defekter Stand wird weder ueberschrieben noch geloescht
    assert manifest_path.read_text(encoding="utf-8") == "{ das ist kein json"
    assert result.stats.new == 1  # defekter Stand war keine Basis
    assert "Defekter Tagesstand" in "".join(p.read_text(encoding="utf-8") for p in (env.home / "Logs").glob("*.log"))


@pytest.mark.parametrize("mutate", [
    lambda m: m.update(status="Unbekannt"),
    lambda m: m.update(format=99),
    lambda m: m.update(user_sid="S-1-5-FREMD"),
    lambda m: m.update(date="kein-datum"),
    lambda m: m.update(files=[["../ausserhalb.txt", 1, 1, "a" * 64, 0]]),
    lambda m: m.update(files=[["C:/Windows/x.txt", 1, 1, "a" * 64, 0]]),
    lambda m: m.update(files=[["Test\\a.txt", 1, 1, "a" * 64, 0]]),
    lambda m: m.update(files=[["Test/a.txt", 1, 1, "kein-hash", 0]]),
    lambda m: m.update(files=[["Test/a.txt", -5, 1, "a" * 64, 0]]),
    lambda m: m.update(files=[["Test/a.txt", 1, 1, "a" * 64, 0], ["Test/a.txt", 1, 1, "a" * 64, 0]]),
    lambda m: m.update(files=[["Fremd/a.txt", 1, 1, "a" * 64, 0]]),
    lambda m: m.pop("categories"),
])
def test_manifest_loader_rejects_bad_metadata(env, mutate):
    write_tree(env.src, {"a.txt": "a"})
    env.run()
    path = env.snapshot("Daily", "2026-03-10")
    data = json.loads((path / ".snapshot.json").read_text(encoding="utf-8"))
    mutate(data)
    fsutil.make_writable(str(path / ".snapshot.json"))
    (path / ".snapshot.json").write_text(json.dumps(data), encoding="utf-8")
    assert mf.load(str(path), sid=env.identity.sid) is None


def test_defective_base_snapshot_falls_back_to_older_valid_one(env):
    write_tree(env.src, {"a.txt": "a"})
    env.run("2026-03-10 12:00:00")
    env.run("2026-03-11 12:00:00")
    broken = env.snapshot("Daily", "2026-03-11") / ".snapshot.json"
    fsutil.make_writable(str(broken))
    broken.write_text("kaputt", encoding="utf-8")
    result = env.run("2026-03-12 12:00:00")
    assert result.status == engine.COMPLETE and result.stats.unchanged == 1 and result.stats.linked == 1
    assert read_tree(env.snapshot("Daily", "2026-03-12")) == {"Test/a.txt": b"a"}


def test_missing_snapshot_file_makes_next_run_use_hash_safe_path(env):
    write_tree(env.src, {"a.txt": "a"})
    env.run("2026-03-10 12:00:00")
    stored = env.snapshot("Daily", "2026-03-10") / "Test" / "a.txt"
    fsutil.make_writable(str(stored))
    stored.unlink()  # Datei im alten Stand geloescht (Manifest kennt sie noch)
    result = env.run("2026-03-11 12:00:00")
    # Hardlink schlaegt fehl -> Datei wird frisch aus der Quelle kopiert statt einen beschaedigten Stand zu erzeugen
    assert result.status == engine.COMPLETE and (env.snapshot("Daily", "2026-03-11") / "Test" / "a.txt").read_text() == "a"


def test_leftover_deleting_folders_are_cleaned(env):
    write_tree(env.src, {"a.txt": "a"})
    env.run("2026-03-10 12:00:00")
    leftover = env.target / ".deleting-abcdef"
    (leftover / "x").mkdir(parents=True)
    (leftover / "x" / "f.txt").write_text("rest")
    env.run("2026-03-11 12:00:00")
    assert not leftover.exists()
