"""Tests 1-6, 11-13, 16, 19-21: Kern der Sicherung."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from ctypes import wintypes

import pytest

from conftest import daily_names, read_tree, tier_names, tree_digest, write_tree
from superpybackup import engine, fsutil, manifest as mf, restore, winapi
from superpybackup.runlog import RunLog


# 1 ------------------------------------------------------------------------------------------
def test_first_full_backup(env):
    write_tree(env.src, {"a.txt": "alpha", "sub/b.txt": "beta", "sub/deep/c.bin": b"\x00\x01\x02", "leer/.keep": ""})
    (env.src / "leerer-ordner").mkdir()
    result = env.run()
    assert result.status == engine.COMPLETE and result.exit_code == 0
    snap = env.snapshot("Daily", "2026-03-10")
    assert read_tree(snap) == {"Test/a.txt": b"alpha", "Test/sub/b.txt": b"beta", "Test/sub/deep/c.bin": b"\x00\x01\x02", "Test/leer/.keep": b""}
    assert (snap / "Test" / "leerer-ordner").is_dir()  # leere Ordner bleiben erhalten
    manifest = mf.load(str(snap), sid=env.identity.sid)
    assert manifest and manifest.status == mf.COMPLETE and len(manifest.files) == 4
    assert result.stats.new == 4 and result.stats.scanned == 4 and result.stats.bytes_transferred == manifest.total_bytes
    assert tier_names(env, "Monthly") == ["2026-03"] and tier_names(env, "Yearly") == ["2026"]
    assert not [p for p in env.target.iterdir() if p.name.startswith(".incomplete")]


# 2 ------------------------------------------------------------------------------------------
def test_second_backup_without_changes_transfers_nothing(env):
    write_tree(env.src, {"a.txt": "alpha", "b.txt": "beta"})
    env.run("2026-03-10 12:00:00")
    result = env.run("2026-03-11 12:00:00")
    assert result.status == engine.COMPLETE
    assert (result.stats.new, result.stats.modified, result.stats.unchanged) == (0, 0, 2)
    assert result.stats.bytes_transferred == 0 and result.stats.extra_storage_bytes == 0 and result.stats.linked == 2
    first, second = env.snapshot("Daily", "2026-03-10") / "Test" / "a.txt", env.snapshot("Daily", "2026-03-11") / "Test" / "a.txt"
    assert os.path.samefile(first, second)  # derselbe Datenblock, kein Duplikat


# 3 ------------------------------------------------------------------------------------------
def test_modified_file_is_stored_independently(env):
    write_tree(env.src, {"a.txt": "version1", "b.txt": "stabil"})
    env.run("2026-03-10 12:00:00")
    (env.src / "a.txt").write_text("version2-laenger", encoding="utf-8")
    result = env.run("2026-03-11 12:00:00")
    assert (result.stats.new, result.stats.modified, result.stats.unchanged) == (0, 1, 1)
    old, new = env.snapshot("Daily", "2026-03-10"), env.snapshot("Daily", "2026-03-11")
    assert (old / "Test" / "a.txt").read_text() == "version1"  # alter Stand unveraendert
    assert (new / "Test" / "a.txt").read_text() == "version2-laenger"
    assert not os.path.samefile(old / "Test" / "a.txt", new / "Test" / "a.txt")
    assert os.path.samefile(old / "Test" / "b.txt", new / "Test" / "b.txt")


# 4 ------------------------------------------------------------------------------------------
def test_new_files_are_added(env):
    write_tree(env.src, {"a.txt": "a"})
    env.run("2026-03-10 12:00:00")
    write_tree(env.src, {"neu.txt": "neu", "ordner/x.txt": "x"})
    result = env.run("2026-03-11 12:00:00")
    assert (result.stats.new, result.stats.unchanged) == (2, 1)
    assert read_tree(env.snapshot("Daily", "2026-03-11")) == {"Test/a.txt": b"a", "Test/neu.txt": b"neu", "Test/ordner/x.txt": b"x"}


# 5 ------------------------------------------------------------------------------------------
def test_deleted_source_file_disappears_only_from_new_snapshot(env):
    write_tree(env.src, {"bleibt.txt": "1", "weg.txt": "2"})
    env.run("2026-03-10 12:00:00")
    (env.src / "weg.txt").unlink()
    result = env.run("2026-03-11 12:00:00")
    assert result.stats.deleted == 1 and result.stats.unchanged == 1
    assert "Test/weg.txt" not in read_tree(env.snapshot("Daily", "2026-03-11"))
    assert read_tree(env.snapshot("Daily", "2026-03-10"))["Test/weg.txt"] == b"2"


# 6 ------------------------------------------------------------------------------------------
def test_restore_earlier_version(env, tmp_path):
    write_tree(env.src, {"doc.txt": "erste Fassung"})
    env.run("2026-03-10 12:00:00")
    (env.src / "doc.txt").write_text("zweite Fassung", encoding="utf-8")
    env.run("2026-03-11 12:00:00")
    out = tmp_path / "restored"
    log = RunLog(str(env.home / "Logs"))
    ref = restore.resolve_snapshot(str(env.target), "daily/2026-03-10", env.identity.sid)
    result = restore.restore(ref.path, str(out), log, sid=env.identity.sid)
    assert result.errors == 0 and result.copied == 1
    assert (out / "Test" / "doc.txt").read_text() == "erste Fassung"
    assert (env.src / "doc.txt").read_text() == "zweite Fassung"  # Quelle unberuehrt


# 11 -----------------------------------------------------------------------------------------
def test_missing_target_fails_without_side_effects(env):
    write_tree(env.src, {"a.txt": "a"})
    gone = env.root / "nicht-vorhanden"
    result = env.run(config_overrides={"backup_root": str(gone)})
    assert result.status == engine.FAILED and result.exit_code == 1
    assert "nicht vorhanden" in result.message and not gone.exists()
    log_text = (env.home / "Logs").glob("*.log").__next__().read_text(encoding="utf-8")
    assert "[ERROR]" in log_text and "Sicherungsziel nicht vorhanden" in log_text
    assert read_tree(env.src) == {"a.txt": b"a"}


def test_missing_source_fails_and_creates_no_snapshot(env):
    result = env.run(config_overrides={"extra_sources": [{"name": "Test", "path": str(env.root / "fehlt")}]})
    assert result.status == engine.FAILED and "Quellordner fehlt" in result.message
    assert not list(env.dst.iterdir())  # kein Zielordner, kein Datenverlust


def test_overlapping_source_and_target_rejected(env):
    result = env.run(config_overrides={"backup_root": str(env.src)})
    assert result.status == engine.FAILED and "ueberlappen" in result.message


# 12 -----------------------------------------------------------------------------------------
def test_no_write_permission(env):
    write_tree(env.src, {"a.txt": "a"})
    user = os.environ["USERNAME"]
    denied = subprocess.run(["icacls", str(env.dst), "/deny", f"{user}:(OI)(CI)(WD,AD)"], capture_output=True, text=True)
    if denied.returncode != 0:
        pytest.skip(f"icacls nicht nutzbar: {denied.stderr}")
    try:
        result = env.run()
    finally:
        subprocess.run(["icacls", str(env.dst), "/remove:d", user], capture_output=True, text=True)
    assert result.status == engine.FAILED
    assert "Schreibrechte" in result.message or "Zugriff" in result.message or "Permission" in result.message or "denied" in result.message.lower()
    assert not list((env.dst).rglob("Daily/*"))


def test_write_probe_failure_is_reported(env, monkeypatch):
    write_tree(env.src, {"a.txt": "a"})
    real_open = open

    def fake_open(path, *args, **kwargs):
        if ".write-test-" in str(path):
            raise PermissionError(13, "Zugriff verweigert")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fake_open)
    result = env.run()
    monkeypatch.undo()
    assert result.status == engine.FAILED and "Schreibrechte" in result.message


# 13 -----------------------------------------------------------------------------------------
def _lock_exclusively(path: str):
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    handle = kernel32.CreateFileW(path, 0x80000000 | 0x40000000, 0, None, 3, 0x80, None)  # lesen+schreiben, kein Teilen
    assert handle not in (None, wintypes.HANDLE(-1).value, 0xFFFFFFFF, -1)
    return handle


def test_locked_file_yields_partial_snapshot(env):
    write_tree(env.src, {"frei.txt": "frei", "gesperrt.txt": "gesperrt"})
    handle = _lock_exclusively(str(env.src / "gesperrt.txt"))
    try:
        result = env.run()
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)
    assert result.status == engine.PARTIAL and result.exit_code == 2
    assert result.stats.errors == 1
    name = os.path.basename(result.snapshot_path)
    assert name != "2026-03-10" and name.startswith("2026-03-10_")  # nie als regulaerer Tagesstand
    manifest = mf.load(result.snapshot_path, sid=env.identity.sid)
    assert manifest.status == mf.PARTIAL and [f.path for f in manifest.files] == ["Test/frei.txt"]
    assert manifest.errors and "gesperrt.txt" in manifest.errors[0]["path"]
    assert tier_names(env, "Monthly") == [] and tier_names(env, "Yearly") == []  # keine Beförderung
    # Folgelauf ohne Sperre liefert den regulaeren, vollstaendigen Tagesstand
    again = env.run("2026-03-10 18:00:00")
    assert again.status == engine.COMPLETE, again.message
    assert os.path.basename(again.snapshot_path) == "2026-03-10"
    assert tier_names(env, "Monthly") == ["2026-03"]


def test_partial_snapshot_is_never_a_base_nor_counts_as_done(env):
    write_tree(env.src, {"a.txt": "a", "b.txt": "b"})
    handle = _lock_exclusively(str(env.src / "b.txt"))
    try:
        env.run("2026-03-10 09:00:00")
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)
    result = env.run("2026-03-10 10:00:00")
    assert result.status == engine.COMPLETE and result.stats.new == 2 and result.stats.linked == 0


# 16 -----------------------------------------------------------------------------------------
def test_second_login_same_day_does_not_rescan(env, monkeypatch):
    write_tree(env.src, {"a.txt": "a"})
    first = env.run("2026-03-10 08:00:00")
    monkeypatch.setattr(engine, "scan_sources", lambda *a, **k: pytest.fail("Quellen duerfen nicht erneut gelesen werden"))
    second = env.run("2026-03-10 17:00:00")
    assert first.status == engine.COMPLETE and second.status == engine.ALREADY and second.exit_code == 0
    assert daily_names(env) == ["2026-03-10"] and second.snapshot_path == first.snapshot_path


def test_new_snapshot_mode_creates_extra_stand_with_unique_name(env):
    write_tree(env.src, {"a.txt": "a"})
    env.run("2026-03-10 08:00:00")
    result = env.run("2026-03-10 17:30:15", config_overrides={"backup": {"existing_daily": "new_snapshot"}})
    assert result.status == engine.COMPLETE
    names = daily_names(env)
    assert len(names) == 2 and "2026-03-10" in names and any(n.startswith("2026-03-10_173015_") for n in names)


# 19 -----------------------------------------------------------------------------------------
def test_insufficient_disk_space_stops_before_writing(env, monkeypatch):
    write_tree(env.src, {"gross.bin": b"x" * 100_000})
    monkeypatch.setattr(fsutil, "disk_free", lambda path: 1000)
    result = env.run()
    assert result.status == engine.FAILED and "Speicherplatz" in result.message
    assert daily_names(env) == [] and not [p for p in env.target.iterdir() if p.name.startswith(".incomplete")]


def test_disk_full_during_copy_aborts_cleanly(env, monkeypatch):
    write_tree(env.src, {"a.txt": "a", "b.txt": "b"})
    real = fsutil.copy_file_hashed
    calls = {"n": 0}

    def failing(source, destination, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(28, "Auf dem Datentraeger ist nicht genuegend Speicherplatz vorhanden")
        return real(source, destination, **kwargs)

    monkeypatch.setattr(fsutil, "copy_file_hashed", failing)
    result = env.run()
    assert result.status == engine.FAILED and "voll" in result.message
    assert daily_names(env) == [] and not [p for p in env.target.iterdir() if p.name.startswith(".incomplete")]


# 20 -----------------------------------------------------------------------------------------
def test_hardlink_counts_and_read_only_protection(env):
    write_tree(env.src, {"a.txt": "unveraendert"})
    for day in ("10", "11", "12"):
        env.run(f"2026-03-{day} 12:00:00")
    stored = env.snapshot("Daily", "2026-03-12") / "Test" / "a.txt"
    # drei Tagesstaende + Monats- und Jahresstand teilen sich eine Datei
    assert os.stat(stored).st_nlink == 5
    with pytest.raises(PermissionError):
        stored.write_text("Manipulation")  # schreibgeschuetzt: Hardlinks koennten sonst alle Staende veraendern
    assert (env.snapshot("Daily", "2026-03-10") / "Test" / "a.txt").read_text() == "unveraendert"


def test_copy_fallback_when_hardlinks_fail(env, monkeypatch):
    write_tree(env.src, {"a.txt": "inhalt", "b.txt": "zwei"})
    env.run("2026-03-10 12:00:00")

    def no_link(*args, **kwargs):
        raise OSError(1, "Hardlinks nicht unterstuetzt")

    monkeypatch.setattr(os, "link", no_link)
    result = env.run("2026-03-11 12:00:00")
    monkeypatch.undo()
    assert result.status == engine.COMPLETE and result.stats.linked == 0
    assert result.stats.bytes_transferred == 10 and result.stats.extra_storage_bytes >= 10
    new = env.snapshot("Daily", "2026-03-11")
    assert read_tree(new) == {"Test/a.txt": b"inhalt", "Test/b.txt": b"zwei"}
    assert not os.path.samefile(new / "Test" / "a.txt", env.snapshot("Daily", "2026-03-10") / "Test" / "a.txt")
    assert restore.verify_snapshot(str(new), RunLog(str(env.home / "Logs")), env.identity.sid) == (2, [])


def test_hardlinks_can_be_disabled(env):
    write_tree(env.src, {"a.txt": "inhalt"})
    env.run("2026-03-10 12:00:00", config_overrides={"backup": {"use_hardlinks": False}})
    result = env.run("2026-03-11 12:00:00", config_overrides={"backup": {"use_hardlinks": False}})
    assert result.stats.linked == 0 and result.stats.bytes_transferred == 6


def test_hash_change_detection_catches_content_change_with_same_mtime(env):
    write_tree(env.src, {"a.txt": "AAAA"})
    env.run("2026-03-10 12:00:00", config_overrides={"backup": {"change_detection": "hash"}})
    stamp = (env.src / "a.txt").stat()
    (env.src / "a.txt").write_text("BBBB", encoding="utf-8")
    os.utime(env.src / "a.txt", ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    result = env.run("2026-03-11 12:00:00", config_overrides={"backup": {"change_detection": "hash"}})
    assert result.stats.modified == 1
    assert (env.snapshot("Daily", "2026-03-11") / "Test" / "a.txt").read_text() == "BBBB"


# 21 -----------------------------------------------------------------------------------------
def test_junctions_and_symlinks_are_not_followed(env):
    write_tree(env.src, {"echt.txt": "echt"})
    outside = env.root / "ausserhalb"
    write_tree(outside, {"geheim.txt": "nicht sichern"})
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(env.src / "junction"), str(outside)], capture_output=True, text=True)
    assert made.returncode == 0, made.stderr
    symlink_ok = True
    try:
        os.symlink(outside / "geheim.txt", env.src / "datei-link.txt")
    except OSError:
        symlink_ok = False  # ohne Entwicklermodus/Adminrechte nicht erstellbar; Junction deckt den Pfad ab
    result = env.run()
    assert result.status == engine.COMPLETE
    stored = read_tree(env.snapshot("Daily", "2026-03-10"))
    assert stored == {"Test/echt.txt": b"echt"}
    assert result.stats.skipped >= (2 if symlink_ok else 1)
    log_text = next((env.home / "Logs").glob("*.log")).read_text(encoding="utf-8")
    assert "Verknuepfung ausgelassen" in log_text
    os.rmdir(env.src / "junction")  # Junction entfernen, ohne das Ziel zu loeschen
    assert (outside / "geheim.txt").exists()


def test_cloud_only_placeholders_are_skipped_not_downloaded(env, monkeypatch):
    # Echte Recall-Attribute kann nur der Cloud-Dienst setzen. Die Erkennung nutzt die Maske winapi.ATTR_ONLINE_ONLY;
    # fuer den Test wird sie auf ein setzbares Attribut (NOT_CONTENT_INDEXED) gelegt, der Codepfad bleibt derselbe.
    marker = 0x2000
    monkeypatch.setattr(winapi, "ATTR_ONLINE_ONLY", marker)
    write_tree(env.src, {"lokal.txt": "lokal", "cloud.txt": "nur online"})
    winapi.set_attributes(str(env.src / "cloud.txt"), marker | 0x20)
    result = env.run()
    assert read_tree(env.snapshot("Daily", "2026-03-10")) == {"Test/lokal.txt": b"lokal"}
    assert result.stats.skipped == 1 and result.status == engine.COMPLETE
    assert "OneDrive" in next((env.home / "Logs").glob("*.log")).read_text(encoding="utf-8")


def test_exclusions_and_defaults(env):
    write_tree(env.src, {"a.txt": "a", "b.iso": "iso", "x.tmp": "t", "Thumbs.db": "t", "node_modules/m.js": "m", "ok/deep/keep.md": "k", "ok/deep/skip.log": "l"})
    env.run(config_overrides={"exclude": ["*.iso", "node_modules", "ok/deep/*.log"]})
    assert sorted(read_tree(env.snapshot("Daily", "2026-03-10"))) == ["Test/a.txt", "Test/ok/deep/keep.md"]


def test_nested_sources_are_not_saved_twice(env):
    write_tree(env.src, {"top.txt": "t", "Bilder/foto.jpg": "j"})
    config_data = {"extra_sources": [{"name": "Alles", "path": str(env.src)}, {"name": "Bilder", "path": str(env.src / "Bilder")}], "use_known_folders": False}
    result = env.run(config_overrides=config_data)
    stored = read_tree(env.snapshot("Daily", "2026-03-10"))
    assert sorted(stored) == ["Alles/top.txt", "Bilder/foto.jpg"] and result.stats.scanned == 2


def test_duplicate_source_is_saved_once(env):
    write_tree(env.src, {"a.txt": "a"})
    result = env.run(config_overrides={"extra_sources": [{"name": "Eins", "path": str(env.src)}, {"name": "Zwei", "path": str(env.src)}]})
    assert sorted(read_tree(env.snapshot("Daily", "2026-03-10"))) == ["Eins/a.txt"]
    assert result.status == engine.COMPLETE


def test_paths_longer_than_max_path(env):
    deep = env.src
    for index in range(8):
        deep = deep / ("verzeichnis_mit_langem_namen_" + str(index) * 12)
    long_file = deep / ("datei_" + "x" * 40 + ".txt")
    os.makedirs(fsutil.fs(str(deep)))
    with open(fsutil.fs(str(long_file)), "w", encoding="utf-8") as handle:
        handle.write("lang")
    assert len(str(long_file)) > 260
    result = env.run()
    assert result.status == engine.COMPLETE and result.stats.scanned == 1
    found = [p for p in fsutil.walk_files_and_dirs(str(env.snapshot("Daily", "2026-03-10"))) if p[0].endswith(".txt")]
    assert len(found) == 1 and open(fsutil.fs(found[0][0]), encoding="utf-8").read() == "lang"
