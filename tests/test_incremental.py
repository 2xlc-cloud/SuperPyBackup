"""Tests der Sicherungsart: Vollsicherung (eigenstaendig) und Inkrement (Hardlinks zum Vorgaenger)."""

from __future__ import annotations

import os

from conftest import daily_names, write_tree
from superpybackup import cli, config as cfgmod, engine, restore, snapshots
from superpybackup.runlog import RunLog


def stored(env, name: str, relative: str = "Test/a.txt"):
    return env.snapshot("Daily", name) / relative.replace("/", os.sep)


def links(env, name: str) -> int:
    return os.stat(stored(env, name)).st_nlink


def kind_of(env, name: str) -> str:
    refs = {ref.name: ref for ref in snapshots.list_snapshots(str(env.target), env.identity.sid, ("Daily",))}
    return refs[name].manifest.stats["type"]


def test_first_backup_is_full_and_second_is_incremental(env):
    write_tree(env.src, {"a.txt": "inhalt"})
    first = env.run("2026-03-10 12:00:00")
    second = env.run("2026-03-11 12:00:00")
    assert (first.kind, second.kind) == (engine.FULL, engine.INCREMENTAL)
    assert kind_of(env, "2026-03-10") == "Full" and kind_of(env, "2026-03-11") == "Incremental"
    assert os.stat(stored(env, "2026-03-11")).st_ino == os.stat(stored(env, "2026-03-10")).st_ino  # Inkrement teilt die Datei mit dem Vorgaenger


def test_forced_full_copies_everything_without_hardlinks(env):
    write_tree(env.src, {"a.txt": "inhalt", "b.txt": "zweite"})
    env.run("2026-03-10 12:00:00")
    full = env.run("2026-03-11 12:00:00", mode=engine.FULL)
    assert full.kind == engine.FULL and full.status == engine.COMPLETE
    assert os.stat(stored(env, "2026-03-11")).st_ino != os.stat(stored(env, "2026-03-10")).st_ino  # keine gemeinsame Datei mehr
    assert links(env, "2026-03-11") == 1
    assert full.stats.unchanged == 2 and full.stats.linked == 0
    assert full.stats.bytes_transferred == len("inhalt") + len("zweite")


def test_full_snapshot_is_independent_of_the_previous_one(env):
    write_tree(env.src, {"a.txt": "inhalt"})
    env.run("2026-03-10 12:00:00")
    env.run("2026-03-11 12:00:00", mode=engine.FULL)
    import shutil
    from superpybackup import fsutil

    fsutil.remove_tree_safe(str(env.snapshot("Daily", "2026-03-10")))  # Vorgaenger weg
    assert shutil.os.path.isfile(stored(env, "2026-03-11"))
    assert restore.verify_snapshot(str(env.snapshot("Daily", "2026-03-11")), RunLog(str(env.home / "Logs")), env.identity.sid)[1] == []


def test_forced_incremental_uses_links_even_when_a_full_is_due(env):
    write_tree(env.src, {"a.txt": "inhalt"})
    env.run("2026-03-01 12:00:00")
    result = env.run("2026-03-20 12:00:00", mode=engine.INCREMENTAL, config_overrides={"backup": {"full_every_days": 7}})
    assert result.kind == engine.INCREMENTAL and result.stats.linked == 1 and result.stats.bytes_transferred == 0


def test_automatic_full_after_configured_days(env):
    write_tree(env.src, {"a.txt": "inhalt"})
    overrides = {"backup": {"full_every_days": 3}}
    kinds = [env.run(f"2026-03-{day:02d} 12:00:00", config_overrides=overrides).kind for day in (10, 11, 12, 13, 14, 15, 16)]
    assert kinds == ["Full", "Incremental", "Incremental", "Full", "Incremental", "Incremental", "Full"]


def test_zero_disables_automatic_full(env):
    write_tree(env.src, {"a.txt": "inhalt"})
    overrides = {"backup": {"full_every_days": 0}}
    kinds = [env.run(f"2026-03-{day:02d} 12:00:00", config_overrides=overrides).kind for day in (10, 20, 30)]
    assert kinds == ["Full", "Incremental", "Incremental"]


def test_without_hardlinks_every_snapshot_is_full(env):
    write_tree(env.src, {"a.txt": "inhalt"})
    overrides = {"backup": {"use_hardlinks": False}}
    kinds = [env.run(f"2026-03-{day:02d} 12:00:00", config_overrides=overrides).kind for day in (10, 11)]
    assert kinds == ["Full", "Full"]


def test_explicit_full_creates_extra_snapshot_when_today_exists(env):
    write_tree(env.src, {"a.txt": "inhalt"})
    env.run("2026-03-10 12:00:00")
    again = env.run("2026-03-10 15:00:00")
    assert again.status == engine.ALREADY and again.kind == ""
    full = env.run("2026-03-10 16:00:00", mode=engine.FULL)
    assert full.status == engine.COMPLETE and full.kind == engine.FULL and len(daily_names(env)) == 2


def test_snapshots_without_type_do_not_count_as_full(env):
    write_tree(env.src, {"a.txt": "inhalt"})
    env.run("2026-03-10 12:00:00")
    import json

    manifest_path = env.snapshot("Daily", "2026-03-10") / ".snapshot.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    del data["stats"]["type"]  # Stand aus einer aelteren Programmversion
    os.chmod(manifest_path, 0o666)
    manifest_path.write_text(json.dumps(data), encoding="utf-8")
    assert snapshots.latest_full(str(env.target), env.identity.sid) is None
    assert env.run("2026-03-11 12:00:00").kind == engine.FULL  # Vollsicherung wird nachgeholt


def test_unknown_mode_fails_cleanly(env):
    write_tree(env.src, {"a.txt": "inhalt"})
    result = env.run(mode="differenziell")
    assert result.status == engine.FAILED and "Unbekannte Art" in result.message


def test_config_validates_full_every_days():
    data = cfgmod.template("D:\\Sicherung")
    data["backup"]["full_every_days"] = -1
    try:
        cfgmod.from_dict(data)
    except cfgmod.ConfigError as error:
        assert "full_every_days" in str(error)
    else:
        raise AssertionError("ungueltiger Wert wurde akzeptiert")
    data["backup"]["full_every_days"] = 0
    assert cfgmod.from_dict(data).backup["full_every_days"] == 0
    assert cfgmod.from_dict(cfgmod.template("D:\\Sicherung")).backup["full_every_days"] == 7


def test_cli_full_and_incremental_flags(env):
    write_tree(env.src, {"a.txt": "inhalt"})
    env.write_config_file()
    first = env.cli("backup", extra_env={"PYTHONIOENCODING": "utf-8"})
    assert first.returncode == 0 and "Art: Vollsicherung" in first.stdout, first.stdout + first.stderr
    full = env.cli("backup", "--full", extra_env={"PYTHONIOENCODING": "utf-8"})
    assert full.returncode == 0 and "Art: Vollsicherung" in full.stdout
    inc = env.cli("backup", "--incremental", "--new-snapshot", extra_env={"PYTHONIOENCODING": "utf-8"})
    assert inc.returncode == 0 and "Art: Inkrement" in inc.stdout
    both = env.cli("backup", "--full", "--incremental", extra_env={"PYTHONIOENCODING": "utf-8"})
    assert both.returncode != 0 and "not allowed" in both.stderr


def test_live_file_lists_every_file_of_the_run(env):
    """aktuell.log (fuer die GUI bei Aufgaben-Laeufen) nennt jede Datei und wird mit jedem Lauf neu begonnen; das Tagesprotokoll bleibt schlank."""
    write_tree(env.src, {"a.txt": "eins", "sub/b.txt": "zwei", "c.txt": "drei"})
    live = env.home / "Logs" / "aktuell.log"
    env.run("2026-03-10 12:00:00")
    first = live.read_text(encoding="utf-8")
    assert first.count("[DATEI] neu:") == 3 and "Test/a.txt" in first and "Test/sub/b.txt" in first and "Sicherungslauf beginnt" in first
    (env.src / "c.txt").write_text("drei-neu", encoding="utf-8")
    env.run("2026-03-11 12:00:00")
    second = live.read_text(encoding="utf-8")
    assert "neu:" not in second and second.count("unveraendert (Hardlink)") == 2 and second.count("[DATEI] geaendert:") == 1  # geleert und neu beschrieben
    day_logs = [p for p in (env.home / "Logs").glob("*.log") if p.name != "aktuell.log"]
    assert day_logs and all("[DATEI]" not in p.read_text(encoding="utf-8") for p in day_logs)
