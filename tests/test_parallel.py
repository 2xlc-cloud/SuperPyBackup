"""Parallele Verarbeitung der Dateien (backup.threads): gleiches Ergebnis wie der Ablauf der Reihe nach, Fehler und Abbruch bleiben beherrscht."""

from __future__ import annotations

import threading

import pytest

from conftest import write_tree
from superpybackup import config as cfgmod
from superpybackup import engine, snapshots

from test_vss import Lock


BASELINE: dict = {}  # Ergebnis des Ablaufs der Reihe nach (threads=1), mit dem die uebrigen verglichen werden


def tree(count=120):
    files = {f"d{i % 7}/sub{i % 3}/f{i}.txt": f"inhalt {i} " * (i % 5 + 1) for i in range(count)}
    files["leer.txt"] = ""
    return files


def manifest_of(env):
    ref = snapshots.list_snapshots(str(env.target), env.identity.sid, ("Daily",))[0]  # neuester Stand (auch ein Teil-Stand)
    return [(e.path, e.size, e.sha256) for e in ref.manifest.files], ref


def run_days(env, threads):
    first = env.run("2026-03-10 12:00:00", config_overrides={"backup": {"threads": threads}})
    (env.src / "d1" / "sub1" / "f1.txt").write_text("geaendert und laenger als vorher " * 3, encoding="utf-8")
    (env.src / "neu.txt").write_text("neu", encoding="utf-8")
    second = env.run("2026-03-11 12:00:00", config_overrides={"backup": {"threads": threads}})
    return first, second


@pytest.mark.parametrize("threads", [1, 2, 8, 32])
def test_every_thread_count_gives_the_same_snapshot(env, tmp_path, threads):
    write_tree(env.src, tree())
    first, second = run_days(env, threads)
    assert first.status == second.status == engine.COMPLETE and second.kind == engine.INCREMENTAL
    assert (first.stats.new, second.stats.new, second.stats.modified, second.stats.linked) == (121, 1, 1, 120)
    entries, ref = manifest_of(env)
    assert [path for path, *_ in entries] == sorted({path for path, *_ in entries}, key=lambda p: [e.path for e in ref.manifest.files].index(p))  # Reihenfolge wie im Plan
    from pathlib import Path

    for path, size, digest in entries:
        assert Path(ref.path, *path.split("/")).stat().st_size == size
    if threads == 1:
        BASELINE["entries"] = entries
    else:
        assert entries == BASELINE["entries"]  # identisch zum Ablauf der Reihe nach


def test_unreadable_file_is_reported_while_the_others_are_saved(env):
    write_tree(env.src, tree(60))
    lock = Lock(env.src / "d2" / "sub2" / "f23.txt")
    try:
        result = env.run(config_overrides={"backup": {"threads": 8, "use_vss": "off"}})
    finally:
        lock.release()
    assert result.status == engine.PARTIAL and result.stats.errors == 1 and result.stats.new == 60
    entries, _ = manifest_of(env)
    assert len(entries) == 60 and all(path != "Test/d2/sub2/f23.txt" for path, *_ in entries)


def test_cancel_stops_the_parallel_run_cleanly(env, monkeypatch):
    write_tree(env.src, tree(80))
    cancel = threading.Event()
    seen = []

    def progress(phase, position, total, name):
        if phase == "copy":
            seen.append(position)
            if position == 10:
                cancel.set()

    result = engine.run_backup(env.config(backup={"threads": 8}), cancel=cancel, progress=progress)
    assert result.status == engine.FAILED and "abgebrochen" in result.message
    assert max(seen) <= 11 and not list(env.target.glob("Daily/*")) and not any(p.name.startswith(".incomplete") for p in env.target.iterdir())


def test_full_disk_stops_the_run_in_parallel_mode(env, monkeypatch):
    import errno

    from superpybackup import fsutil

    write_tree(env.src, tree(40))
    real = fsutil.copy_file_hashed
    counter = {"n": 0}
    lock = threading.Lock()

    def full_after_a_few(source, destination, **kwargs):
        with lock:
            counter["n"] += 1
            number = counter["n"]
        if number > 5:
            raise OSError(errno.ENOSPC, "Kein Speicherplatz")
        return real(source, destination, **kwargs)

    monkeypatch.setattr(fsutil, "copy_file_hashed", full_after_a_few)
    result = env.run(config_overrides={"backup": {"threads": 8}})
    assert result.status == engine.FAILED and "voll" in result.message
    assert not list(env.target.glob("Daily/*")) and not any(p.name.startswith(".incomplete") for p in env.target.iterdir())


def test_threads_setting_is_validated(env):
    assert env.config().backup["threads"] == 8
    for bad in (0, 33, "viele"):
        with pytest.raises(cfgmod.ConfigError, match="threads"):
            cfgmod.from_dict({**env.config().data, "backup": {**env.config().backup, "threads": bad}})
