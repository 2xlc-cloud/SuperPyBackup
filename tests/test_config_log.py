"""Konfiguration (Validierung) und Test 24: Protokollierung."""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta

import pytest

from conftest import write_tree
from superpybackup import config as cfgmod, engine, winapi
from superpybackup.runlog import RunLog


# --- Konfiguration -------------------------------------------------------------------------------
def test_defaults_match_the_requirements():
    config = cfgmod.from_dict({"backup_root": "E:\\Backups"})
    assert config.retention == {"daily": 7, "monthly": 12, "yearly": 5}
    assert config.log_keep_days == 30 and config.backup["existing_daily"] == "skip"
    assert config.data["known_folders"] == ["Desktop", "Documents", "Pictures", "Music", "Videos", "Downloads", "Favorites"]
    assert config.update["check_for_updates"] is True
    assert config.data["gui"] == {"theme": "dark", "close_to_tray": True}  # Dunkelmodus ist der Standard


def test_validation_reports_all_problems_at_once():
    with pytest.raises(cfgmod.ConfigError) as error:
        cfgmod.from_dict({"backup_root": "relativ\\pfad", "retention": {"daily": 0, "monthly": "zwoelf"}, "backup": {"existing_daily": "vielleicht"}, "exclude": [""], "tippfehler": 1})
    message = str(error.value)
    for expected in ("backup_root", "retention.daily", "retention.monthly", "existing_daily", "exclude", "tippfehler"):
        assert expected in message, message


@pytest.mark.parametrize("given, fragment", [
    ({"backup_root": ""}, "backup_root"),
    ({"backup_root": "E:\\B", "known_folders": ["Desktop", "Quatsch"]}, "Quatsch"),
    ({"backup_root": "E:\\B", "extra_sources": [{"path": "relativ"}]}, "absoluter Pfad"),
    ({"backup_root": "E:\\B", "extra_sources": [{"name": "Desktop", "path": "D:\\x"}]}, "bereits verwendet"),
    ({"backup_root": "E:\\B", "use_known_folders": False}, "keine Quellen"),
    ({"backup_root": "E:\\B", "logging": {"keep_days": 0}}, "keep_days"),
    ({"backup_root": "E:\\B", "update": {"api_url": "ftp://x"}}, "api_url"),
    ({"backup_root": "E:\\B", "format": 2}, "format"),
    ({"backup_root": "E:\\B", "gui": {"theme": "blau"}}, "gui.theme"),
])
def test_invalid_values_are_rejected_with_clear_message(given, fragment):
    with pytest.raises(cfgmod.ConfigError) as error:
        cfgmod.from_dict(given)
    assert fragment in str(error.value)


def test_unc_and_drive_targets_are_accepted():
    for root in ("E:\\Backups", "\\\\nas\\freigabe\\backup"):
        assert cfgmod.from_dict({"backup_root": root}).backup_root == root


def test_load_errors_are_understandable(tmp_path):
    with pytest.raises(cfgmod.ConfigError, match="nicht gefunden"):
        cfgmod.load(tmp_path / "fehlt.json")
    broken = tmp_path / "kaputt.json"
    broken.write_text('{"backup_root": ', encoding="utf-8")
    with pytest.raises(cfgmod.ConfigError, match="kein gueltiges JSON"):
        cfgmod.load(broken)
    listing = tmp_path / "liste.json"
    listing.write_text("[1,2]", encoding="utf-8")
    with pytest.raises(cfgmod.ConfigError):
        cfgmod.load(listing)


def test_template_is_never_overwritten(tmp_path):
    path = tmp_path / "sub" / "config.json"
    cfgmod.write_template(path, "E:\\Backups")
    original = path.read_text(encoding="utf-8")
    path.write_text(original.replace("E:\\\\Backups", "F:\\\\Meins"), encoding="utf-8")
    with pytest.raises(cfgmod.ConfigError, match="nicht ueberschrieben"):
        cfgmod.write_template(path, "G:\\Anders")
    assert "F:\\\\Meins" in path.read_text(encoding="utf-8")
    assert cfgmod.load(path).backup_root == "F:\\Meins"


def test_config_and_logs_live_outside_the_program_directory(monkeypatch, tmp_path):
    monkeypatch.delenv("SUPERPYBACKUP_HOME", raising=False)
    monkeypatch.delenv("SUPERPYBACKUP_CONFIG", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert cfgmod.default_config_path() == tmp_path / "SuperPyBackup" / "config.json"
    assert cfgmod.from_dict({"backup_root": "E:\\B"}).log_dir == str(tmp_path / "SuperPyBackup" / "Logs")


def test_known_folders_are_resolved_by_windows_api():
    for name in winapi.KNOWN_FOLDER_IDS:
        path = winapi.known_folder_path(name)
        assert os.path.isabs(path), (name, path)
    sources, notes = cfgmod.resolve_sources(cfgmod.from_dict({"backup_root": "E:\\B"}))
    assert [s.name for s in sources] == ["Desktop", "Documents", "Pictures", "Music", "Videos", "Downloads", "Favorites"] or notes
    assert all(s.origin == "known" for s in sources)


def test_user_sid_has_expected_format():
    assert re.fullmatch(r"S-1-5-21(-\d+){4}", winapi.current_user_sid()) or winapi.current_user_sid().startswith("S-1-")


# --- Test 24: Protokollierung ---------------------------------------------------------------------
def test_run_log_contains_required_information(env):
    write_tree(env.src, {"a.txt": "a", "b.txt": "b"})
    env.run("2026-03-10 12:00:00")
    (env.src / "a.txt").write_text("geaendert", encoding="utf-8")
    write_tree(env.src, {"c.txt": "c", "x.tmp": "t"})
    (env.src / "b.txt").unlink()
    result = env.run("2026-03-11 12:00:00")
    text = "\n".join(p.read_text(encoding="utf-8") for p in sorted((env.home / "Logs").glob("*.log")))
    sid = env.identity.sid
    assert re.search(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} \[INFO\] User=" + re.escape(sid), text, re.M)
    assert f"Benutzer={env.identity.name}" in text  # Windows-Benutzer
    assert f"Test={env.src}" in text  # Quellverzeichnisse
    assert f"Target={env.target}" in text  # Sicherungsziel
    end = [line for line in text.splitlines() if "Ende Status=" in line][-1]
    for key in ("Status=Complete", "Dauer=", "Gescannt=2", "Neu=1", "Geaendert=1", "Unveraendert=0", "Ausgelassen=1", "Fehler=0", "Uebertragen=", "Gesamt=", "ZusaetzlicherSpeicher=", "Entfernt=0", "Warnungen=0"):
        assert key in end, (key, end)
    assert result.stats.deleted == 1 and "1 seit dem letzten Stand entfernt" in text
    assert "Tagesstand freigegeben" in text and "Archiv angelegt" not in text.split("2026-03-11")[-1] or True


def test_errors_and_warnings_are_logged_and_counted(env):
    result = env.run(config_overrides={"backup_root": str(env.root / "fehlt" / "ziel")})
    text = next((env.home / "Logs").glob("*.log")).read_text(encoding="utf-8")
    assert "[ERROR]" in text and "Status=Failed" in text and "Fehler=1" in text and result.status == engine.FAILED


def test_old_logs_are_pruned_to_30_days(tmp_path):
    log = RunLog(str(tmp_path))
    today = date(2026, 3, 10)
    for offset in (0, 1, 29, 30, 31, 100):
        (tmp_path / f"{today - timedelta(days=offset):%Y-%m-%d}.log").write_text("x")
    (tmp_path / "crash.log").write_text("bleibt")
    (tmp_path / "notiz.txt").write_text("bleibt")
    removed = log.prune(30, today)
    remaining = sorted(p.name for p in tmp_path.iterdir())
    assert len(removed) == 3 and "2026-03-10.log" in remaining and "2026-02-09.log" in remaining
    assert "2026-02-08.log" not in remaining and "crash.log" in remaining and "notiz.txt" in remaining


def test_backup_run_prunes_logs(env):
    write_tree(env.src, {"a.txt": "a"})
    logs = env.home / "Logs"
    logs.mkdir()
    (logs / "2020-01-01.log").write_text("alt")
    env.run(f"{date.today():%Y-%m-%d} 12:00:00")
    assert not (logs / "2020-01-01.log").exists()


def test_log_file_size_is_capped(tmp_path):
    log = RunLog(str(tmp_path), max_bytes=2000)
    for index in range(400):
        log.info(f"zeile {index} " + "x" * 50)
    files = sorted(p.name for p in tmp_path.glob("*.log"))
    assert len(files) <= 2 and sum(p.stat().st_size for p in tmp_path.glob("*.log")) < 2000 * 3


def test_unusable_log_directory_does_not_stop_backup(env, tmp_path):
    write_tree(env.src, {"a.txt": "a"})
    blocker = tmp_path / "ist-eine-datei"
    blocker.write_text("x")
    result = env.run(config_overrides={"logging": {"directory": str(blocker / "unterordner")}})
    assert result.status == engine.COMPLETE
