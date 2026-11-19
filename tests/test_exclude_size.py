"""Ausschlussmuster (auch nur fuer Ordner) und Groessengrenze je Sicherung."""

from __future__ import annotations

import pytest

from conftest import write_tree
from superpybackup import config as cfgmod
from superpybackup import engine, restore


def saved_paths(env):
    ref = restore.resolve_snapshot(str(env.target), "latest", env.identity.sid)
    return sorted(entry.path for entry in ref.manifest.files)


def test_trailing_slash_pattern_matches_folders_only():
    excluded = engine.build_excluder(["build/", "*.iso"])
    assert excluded("build", "build", True) and excluded("build", "projekt/build", True)
    assert not excluded("build", "build", False)  # eine Datei namens build bleibt
    assert excluded("image.ISO", "x/image.ISO", False)  # normale Muster gelten fuer Dateien und Ordner, ohne Gross-/Kleinschreibung
    assert not excluded("rebuild", "rebuild", True) and not excluded("a.txt", "a.txt")


def test_folder_pattern_excludes_the_whole_folder(env):
    write_tree(env.src, {"a.txt": "a", "node_modules/x/y.js": "js", "doc/node_modules.txt": "text", "node_modules_datei": "bleibt"})
    result = env.run(config_overrides={"exclude": ["node_modules/"]})
    assert result.status == engine.COMPLETE
    assert saved_paths(env) == ["Test/a.txt", "Test/doc/node_modules.txt", "Test/node_modules_datei"]


def test_size_limit_skips_large_files_and_logs_them(env):
    write_tree(env.src, {"klein.txt": "x" * 100, "gross.bin": "y" * (2 * 1024 * 1024 + 1), "genau.bin": "z" * (2 * 1024 * 1024)})
    result = env.run(config_overrides={"backup": {"max_file_size_mb": 2}})
    assert result.status == engine.COMPLETE and result.stats.errors == 0 and result.stats.skipped == 1
    assert saved_paths(env) == ["Test/genau.bin", "Test/klein.txt"]  # genau an der Grenze wird noch gesichert
    log = "".join(p.read_text(encoding="utf-8") for p in (env.home / "Logs").glob("*.log"))
    assert "gross.bin" in log and "groesser als die Grenze" in log


def test_size_limit_zero_means_no_limit_and_is_validated(env):
    write_tree(env.src, {"gross.bin": "y" * (3 * 1024 * 1024)})
    assert env.config().backup["max_file_size_mb"] == 0
    assert env.run().status == engine.COMPLETE and saved_paths(env) == ["Test/gross.bin"]
    with pytest.raises(cfgmod.ConfigError, match="max_file_size_mb"):
        cfgmod.from_dict({**env.config().data, "backup": {**env.config().backup, "max_file_size_mb": -1}})


def test_changing_the_limit_between_runs_does_not_break_incremental(env):
    write_tree(env.src, {"a.txt": "a", "gross.bin": "y" * (2 * 1024 * 1024 + 5)})
    assert env.run(config_overrides={"backup": {"max_file_size_mb": 1}}).status == engine.COMPLETE
    assert saved_paths(env) == ["Test/a.txt"]
    assert env.run("2026-03-11 12:00:00").status == engine.COMPLETE  # Grenze aufgehoben: Datei kommt dazu
    assert saved_paths(env) == ["Test/a.txt", "Test/gross.bin"]
