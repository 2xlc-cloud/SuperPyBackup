"""Suche in allen Staenden (find_versions), Vorschau (preview) und der Befehl 'find'."""

from __future__ import annotations

import base64

import pytest

from conftest import write_tree
from superpybackup import manifest as mf
from superpybackup import restore, snapshots

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def three_days(env):
    write_tree(env.src, {"doc.txt": "eins", "x/Rechnung.pdf": "pdf1", "x/notiz.md": "notiz"})
    env.run("2026-03-10 12:00:00")
    env.run("2026-03-11 12:00:00")  # nichts geaendert
    (env.src / "doc.txt").write_text("zwei und mehr", encoding="utf-8")
    env.run("2026-03-12 12:00:00")
    return snapshots.list_snapshots(str(env.target), env.identity.sid)


def test_each_distinct_version_once_newest_first(env):
    refs = three_days(env)
    versions = restore.find_versions(refs, "doc")
    assert [(v.path, v.ref.name) for v in versions] == [("Test/doc.txt", "2026-03-12"), ("Test/doc.txt", "2026-03-11")]
    assert versions[0].also_in == [] and versions[1].also_in == ["daily/2026-03-10"]  # dieselbe Fassung im aeltesten Stand
    assert versions[0].entry.size == len("zwei und mehr") and versions[1].entry.size == 4


def test_unchanged_file_appears_once_with_all_snapshots(env):
    refs = three_days(env)
    (version,) = restore.find_versions(refs, "notiz")
    assert version.ref.name == "2026-03-12" and version.also_in == ["daily/2026-03-11", "daily/2026-03-10"]


def test_patterns_substring_glob_and_case(env):
    refs = three_days(env)
    assert [v.path for v in restore.find_versions(refs, "RECHNUNG")] == ["Test/x/Rechnung.pdf"]  # Gross/klein egal
    assert [v.path for v in restore.find_versions(refs, "*.pdf")] == ["Test/x/Rechnung.pdf"]
    both = ["Test/x/Rechnung.pdf", "Test/x/notiz.md"]  # das Muster gilt fuer den ganzen Pfad oder nur den Dateinamen; \ wird wie / behandelt
    assert sorted({v.path for v in restore.find_versions(refs, "Test/x/*")}) == both
    assert sorted({v.path for v in restore.find_versions(refs, "test\\x\\*")}) == both
    assert restore.find_versions(refs, "gibt-es-nicht") == []
    with pytest.raises(restore.RestoreError):
        restore.find_versions(refs, "   ")


def test_limit_and_defective_snapshots_are_skipped(env):
    refs = three_days(env)
    refs.append(snapshots.SnapshotRef("Daily", "2026-03-13", str(env.target / "Daily" / "2026-03-13"), None))  # defekt: ohne Manifest
    assert len(restore.find_versions(refs, "Test/", limit=2)) == 2
    assert all(v.ref.manifest is not None for v in restore.find_versions(refs, "doc"))


def entry_of(env, name, relative):
    ref = restore.resolve_snapshot(str(env.target), name, env.identity.sid)
    return ref, ref.manifest.index()[relative]


def test_preview_of_text_image_and_binary(env):
    write_tree(env.src, {"a.txt": "Grüße aus Köln\nzweite Zeile"})
    (env.src / "bild.png").write_bytes(PNG)
    (env.src / "daten.bin").write_bytes(bytes(range(256)) * 4)
    (env.src / "alt.txt").write_bytes("Müller".encode("cp1252"))
    (env.src / "gross.txt").write_text("ä" * 100_000, encoding="utf-8")
    env.run()
    ref, entry = entry_of(env, "latest", "Test/a.txt")
    shown = restore.preview(ref.path, entry)
    assert shown.kind == "text" and shown.text.replace("\r\n", "\n") == "Grüße aus Köln\nzweite Zeile" and not shown.truncated and "Test/a.txt" in shown.info
    _, entry = entry_of(env, "latest", "Test/bild.png")
    image = restore.preview(ref.path, entry)
    assert image.kind == "image" and image.data == PNG
    _, entry = entry_of(env, "latest", "Test/daten.bin")
    assert restore.preview(ref.path, entry).kind == "binary"
    _, entry = entry_of(env, "latest", "Test/alt.txt")
    assert restore.preview(ref.path, entry).text == "Müller"  # Windows-Zeichensatz wird erkannt
    _, entry = entry_of(env, "latest", "Test/gross.txt")
    big = restore.preview(ref.path, entry)
    assert big.truncated and big.text.startswith("ä") and set(big.text) == {"ä"} and len(big.text) <= restore.PREVIEW_BYTES  # nicht mitten im Zeichen geschnitten


def test_preview_refuses_paths_outside_the_snapshot_and_missing_files(env):
    write_tree(env.src, {"a.txt": "a"})
    env.run()
    ref, entry = entry_of(env, "latest", "Test/a.txt")
    with pytest.raises(restore.RestoreError):
        restore.preview(ref.path, mf.FileEntry("../../geheim.txt", 1, 0, "0" * 64))
    with pytest.raises(restore.RestoreError):
        restore.preview(ref.path, mf.FileEntry("Test/fehlt.txt", 1, 0, "0" * 64))


def test_find_command(env):
    three_days(env)
    env.write_config_file()
    result = env.cli("--config", str(env.root / "config.json"), "find", "doc")
    assert result.returncode == 0 and result.stdout.count("Test/doc.txt") == 2 and "daily/2026-03-12" in result.stdout and "+1 weitere" in result.stdout
    missing = env.cli("--config", str(env.root / "config.json"), "find", "gibt-es-nicht")
    assert missing.returncode == 1 and "Keine passende Datei" in missing.stdout
