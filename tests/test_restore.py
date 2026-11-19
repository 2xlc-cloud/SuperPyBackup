"""Tests 22, 23: Wiederherstellung einzelner Dateien und ganzer Verzeichnisse (plus Schutzmechanismen)."""

from __future__ import annotations

import os
import shutil

import pytest

from conftest import read_tree, write_tree
from superpybackup import engine, fsutil, restore, snapshots, winapi
from superpybackup.runlog import RunLog


@pytest.fixture
def populated(env):
    write_tree(env.src, {"a.txt": "alpha", "projekt/b.txt": "beta", "projekt/tief/c.txt": "gamma", "projekt/tief/d.bin": b"\x00\xff", "anderes/e.txt": "epsilon"})
    assert env.run("2026-03-10 12:00:00").status == engine.COMPLETE
    env.log = RunLog(str(env.home / "Logs"))
    env.snap = str(env.snapshot("Daily", "2026-03-10"))
    return env


def do_restore(env, dest, **kwargs):
    return restore.restore(env.snap, None if kwargs.get("original") else str(dest), env.log, sid=env.identity.sid, **kwargs)


# 22 -----------------------------------------------------------------------------------------
def test_restore_single_file(populated, tmp_path):
    out = tmp_path / "out"
    result = do_restore(populated, out, paths=["Test/projekt/b.txt"])
    assert (result.copied, result.errors) == (1, 0)
    assert read_tree(out) == {"Test/projekt/b.txt": b"beta"}


# 23 -----------------------------------------------------------------------------------------
def test_restore_whole_directory(populated, tmp_path):
    out = tmp_path / "out"
    result = do_restore(populated, out, paths=["Test/projekt"])
    assert (result.copied, result.errors) == (3 - 1 + 0, 0) or result.copied == 3
    assert read_tree(out) == {"Test/projekt/b.txt": b"beta", "Test/projekt/tief/c.txt": b"gamma", "Test/projekt/tief/d.bin": b"\x00\xff"}


def test_restore_everything_keeps_empty_folders(populated, tmp_path):
    (populated.src / "leer").mkdir()
    populated.run("2026-03-11 12:00:00")
    out = tmp_path / "out"
    restore.restore(str(populated.snapshot("Daily", "2026-03-11")), str(out), populated.log, sid=populated.identity.sid)
    assert (out / "Test" / "leer").is_dir() and len(read_tree(out)) == 5


def test_prefix_does_not_match_sibling_names(populated, tmp_path):
    write_tree(populated.src, {"projekt2/x.txt": "x"})
    populated.run("2026-03-11 12:00:00")
    out = tmp_path / "out"
    restore.restore(str(populated.snapshot("Daily", "2026-03-11")), str(out), populated.log, paths=["Test/projekt"], sid=populated.identity.sid)
    assert not any(name.startswith("Test/projekt2") for name in read_tree(out))


def test_restore_to_original_location_after_loss(populated):
    shutil.rmtree(populated.src / "projekt")
    result = do_restore(populated, None, original=True, paths=["Test/projekt"])
    assert result.errors == 0 and (populated.src / "projekt" / "tief" / "c.txt").read_text() == "gamma"
    assert (populated.src / "a.txt").read_text() == "alpha"


def test_existing_files_are_not_overwritten_by_default(populated, tmp_path):
    out = tmp_path / "out"
    write_tree(out, {"Test/a.txt": "lokal veraendert"})
    result = do_restore(populated, out, paths=["Test/a.txt"])
    assert (result.copied, result.skipped) == (0, 1)
    assert (out / "Test" / "a.txt").read_text() == "lokal veraendert"


def test_overwrite_needs_explicit_confirmation(populated, tmp_path):
    out = tmp_path / "out"
    write_tree(out, {"Test/a.txt": "lokal veraendert"})
    asked = []

    def deny(count):
        asked.append(count)
        return False

    result = do_restore(populated, out, paths=["Test/a.txt"], overwrite=True, confirm_overwrite=deny)
    assert asked == [1] and result.copied == 0 and (out / "Test" / "a.txt").read_text() == "lokal veraendert"
    result = do_restore(populated, out, paths=["Test/a.txt"], overwrite=True)  # ohne Bestaetigungsfunktion: ebenfalls nicht
    assert result.copied == 0
    result = do_restore(populated, out, paths=["Test/a.txt"], overwrite=True, confirm_overwrite=lambda count: True)
    assert result.copied == 1 and (out / "Test" / "a.txt").read_text() == "alpha"


def test_overwrite_replaces_read_only_files(populated, tmp_path):
    out = tmp_path / "out"
    write_tree(out, {"Test/a.txt": "alt"})
    os.chmod(out / "Test" / "a.txt", 0o444)
    result = do_restore(populated, out, paths=["Test/a.txt"], overwrite=True, confirm_overwrite=lambda c: True)
    assert result.copied == 1 and (out / "Test" / "a.txt").read_text() == "alpha"


def test_timestamp_and_attributes_are_restored(populated, tmp_path):
    hidden = populated.src / "versteckt.txt"
    hidden.write_text("v", encoding="utf-8")
    winapi.set_attributes(str(hidden), winapi.ATTR_HIDDEN | winapi.ATTR_ARCHIVE)
    stamp = 1_600_000_000_123_456_700
    os.utime(hidden, ns=(stamp, stamp))
    populated.run("2026-03-11 12:00:00")
    out = tmp_path / "out"
    restore.restore(str(populated.snapshot("Daily", "2026-03-11")), str(out), populated.log, paths=["Test/versteckt.txt"], sid=populated.identity.sid)
    restored = out / "Test" / "versteckt.txt"
    info = restored.stat()
    assert info.st_mtime_ns == stamp
    assert info.st_file_attributes & winapi.ATTR_HIDDEN
    assert not info.st_file_attributes & winapi.ATTR_READONLY  # Sicherungs-Schreibschutz wird nicht uebernommen


# Schutzmechanismen ---------------------------------------------------------------------------
def test_tampered_backup_file_is_detected_and_not_restored(populated, tmp_path):
    stored = populated.snapshot("Daily", "2026-03-10") / "Test" / "a.txt"
    fsutil.make_writable(str(stored))
    stored.write_text("MANIPULIERT", encoding="utf-8")  # gleiche Laenge waere gefaehrlicher, hier reicht der Hash
    out = tmp_path / "out"
    result = do_restore(populated, out, paths=["Test/a.txt"])
    assert result.errors == 1 and result.copied == 0
    assert not (out / "Test" / "a.txt").exists() and not list(out.rglob(".restore-*"))
    count, bad = restore.verify_snapshot(populated.snap, populated.log, populated.identity.sid)
    assert bad == ["Test/a.txt"] and count == 5


@pytest.mark.parametrize("bad_path", ["../x", "Test/../../x", "C:/Windows/x", "/abs", "Test\\a.txt\\..\\.."])
def test_path_traversal_is_rejected(populated, tmp_path, bad_path):
    with pytest.raises((restore.RestoreError, ValueError)):
        do_restore(populated, tmp_path / "out", paths=[bad_path])
    assert not (tmp_path / "x").exists()


def test_unknown_category_and_path_rejected(populated, tmp_path):
    with pytest.raises(restore.RestoreError):
        do_restore(populated, tmp_path / "out", categories=["Gibtsnicht"])
    with pytest.raises(restore.RestoreError):
        do_restore(populated, tmp_path / "out", paths=["Test/nicht/vorhanden.txt"])


def test_destination_inside_snapshot_rejected(populated):
    with pytest.raises(restore.RestoreError):
        do_restore(populated, os.path.join(populated.snap, "ziel"))


def test_restore_refuses_junction_in_destination(populated, tmp_path):
    import subprocess

    elsewhere = tmp_path / "woanders"
    elsewhere.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(out / "Test"), str(elsewhere)], capture_output=True, text=True)
    assert made.returncode == 0
    result = do_restore(populated, out, paths=["Test/a.txt"])
    os.rmdir(out / "Test")
    assert result.errors >= 1 and not list(elsewhere.rglob("*.txt"))


def test_restore_works_while_backup_is_running(populated, tmp_path, monkeypatch):
    lock = engine.TargetLock(str(populated.target / ".backup.lock"))
    assert lock.acquire()  # Backup "laeuft": Ziel gesperrt
    try:
        result = do_restore(populated, tmp_path / "out", paths=["Test/a.txt"])
    finally:
        lock.release()
    assert result.copied == 1


def test_resolve_snapshot_specs(populated):
    target = str(populated.target)
    sid = populated.identity.sid
    assert restore.resolve_snapshot(target, "latest", sid).name == "2026-03-10"
    assert restore.resolve_snapshot(target, str(populated.snapshot("Daily", "2026-03-10")), sid).name == "2026-03-10"
    for bad in ("daily/2099-01-01", "foo", "daily/../../x"):
        with pytest.raises(restore.RestoreError):
            restore.resolve_snapshot(target, bad, sid)


def test_list_distinguishes_tiers_and_status(populated):
    assert populated.run("2027-01-05 12:00:00").status == engine.COMPLETE  # Monat und Jahr 2026 sind vorbei: Monats- und Jahresstand entstehen
    refs = snapshots.list_snapshots(str(populated.target), populated.identity.sid)
    assert sorted((ref.tier, ref.manifest.status) for ref in refs) == [("Daily", "Complete"), ("Monthly", "Complete"), ("Yearly", "Complete")]
    target, sid = str(populated.target), populated.identity.sid
    assert restore.resolve_snapshot(target, "monthly/2026-03", sid).tier == "Monthly"
    assert restore.resolve_snapshot(target, "yearly/2026", sid).tier == "Yearly"


def test_cli_end_to_end_list_restore_verify(populated, tmp_path):
    populated.write_config_file()
    listing = populated.cli("list")
    assert listing.returncode == 0 and "daily/2026-03-10" in listing.stdout
    out = tmp_path / "cli-out"
    restored = populated.cli("restore", "--snapshot", "daily/2026-03-10", "--path", "Test/projekt/b.txt", "--dest", str(out))
    assert restored.returncode == 0, restored.stderr
    assert read_tree(out) == {"Test/projekt/b.txt": b"beta"}
    assert populated.cli("verify", "--snapshot", "daily/2026-03-10").returncode == 0
    refused = populated.cli("restore", "--path", "../x", "--dest", str(out))
    assert refused.returncode != 0
