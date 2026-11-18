"""Tests der Desktop-Meldungen. Es wird nie eine echte Meldung angezeigt: SUPERPYBACKUP_NOTIFY_FILE faengt sie ab."""

from __future__ import annotations

import json
import shutil

import pytest

from conftest import write_tree
from superpybackup import cli, config as cfgmod, engine, notify, updater


@pytest.fixture
def captured(tmp_path, monkeypatch):
    path = tmp_path / "meldungen.jsonl"
    monkeypatch.setenv("SUPERPYBACKUP_NOTIFY_FILE", str(path))

    def read() -> list[dict]:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    return read


def run_cli(env, captured_path, *args):
    return env.cli(*args, extra_env={"PYTHONIOENCODING": "utf-8", "SUPERPYBACKUP_NOTIFY_FILE": captured_path})


def test_defaults_are_off_and_validated():
    config = cfgmod.from_dict(cfgmod.template("D:\\Sicherung"))
    assert config.notifications == {"enabled": False, "on_success": True, "on_update": True, "on_error": True}
    data = cfgmod.template("D:\\Sicherung")
    data["notifications"]["enabled"] = "ja"
    with pytest.raises(cfgmod.ConfigError, match="notifications.enabled"):
        cfgmod.from_dict(data)


def test_notify_respects_switches(captured):
    settings = {"enabled": False, "on_success": True, "on_update": True, "on_error": True}
    assert notify.notify(settings, notify.ERROR, "T", "M") is False and captured() == []
    settings["enabled"] = True
    assert notify.notify(settings, notify.ERROR, "Titel", "Text") is True
    settings["on_error"] = False
    assert notify.notify(settings, notify.ERROR, "x", "y") is False
    assert captured() == [{"title": "Titel", "message": "Text"}]


def test_backup_message_per_status():
    stats = engine.Stats(new=2, modified=1, unchanged=5, errors=3)
    assert notify.backup_message(engine.RunResult(status=engine.COMPLETE, stats=stats), "Inkrement")[0] == notify.SUCCESS
    assert notify.backup_message(engine.RunResult(status=engine.PARTIAL, stats=stats))[0] == notify.ERROR
    failed = notify.backup_message(engine.RunResult(status=engine.FAILED, message="Ziel fehlt"))
    assert failed[0] == notify.ERROR and "Ziel fehlt" in failed[2]
    assert notify.backup_message(engine.RunResult(status=engine.ALREADY)) is None
    assert notify.backup_message(engine.RunResult(status=engine.BUSY)) is None


def test_scheduled_style_backup_notifies_success(env, tmp_path):
    write_tree(env.src, {"a.txt": "x"})
    env.write_config_file(notifications={"enabled": True})
    sink = tmp_path / "out.jsonl"
    result = run_cli(env, str(sink), "backup", "--quiet")
    assert result.returncode == 0 and result.stdout == ""
    messages = [json.loads(line) for line in sink.read_text(encoding="utf-8").splitlines()]
    assert len(messages) == 1 and messages[0]["title"] == "Sicherung abgeschlossen" and "Vollsicherung" in messages[0]["message"]


def test_nothing_shown_when_disabled_or_nothing_to_report(env, tmp_path):
    write_tree(env.src, {"a.txt": "x"})
    sink = tmp_path / "out.jsonl"
    env.write_config_file()  # Standard: aus
    assert run_cli(env, str(sink), "backup", "--quiet").returncode == 0
    env.write_config_file(notifications={"enabled": True})
    assert run_cli(env, str(sink), "backup", "--quiet").returncode == 0  # heute schon gesichert: nichts zu melden
    assert not sink.exists()


def test_error_is_reported_even_without_success_notifications(env, tmp_path):
    write_tree(env.src, {"a.txt": "x"})
    env.write_config_file(notifications={"enabled": True, "on_success": False})
    sink = tmp_path / "out.jsonl"
    assert run_cli(env, str(sink), "backup", "--quiet").returncode == 0 and not sink.exists()
    shutil.rmtree(env.src)  # Quelle verschwindet: der naechste Lauf scheitert
    assert run_cli(env, str(sink), "backup", "--quiet", "--new-snapshot").returncode == 1
    messages = [json.loads(line) for line in sink.read_text(encoding="utf-8").splitlines()]
    assert [m["title"] for m in messages] == ["Sicherung fehlgeschlagen"] and "Quellordner" in messages[0]["message"]


def test_unloadable_config_is_still_reported(env, tmp_path):
    config_path = env.write_config_file(notifications={"enabled": True})
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data["retention"]["daily"] = 0  # ungueltig
    config_path.write_text(json.dumps(data), encoding="utf-8")
    sink = tmp_path / "out.jsonl"
    result = run_cli(env, str(sink), "backup", "--quiet")
    assert result.returncode == 1
    messages = [json.loads(line) for line in sink.read_text(encoding="utf-8").splitlines()]
    assert len(messages) == 1 and messages[0]["title"] == "Sicherung nicht gestartet"


def test_new_version_found_by_periodic_check_is_reported(env, captured, monkeypatch):
    monkeypatch.setenv("SUPERPYBACKUP_HOME", str(env.home))
    write_tree(env.src, {"a.txt": "x"})
    env.write_config_file(notifications={"enabled": True, "on_success": False})
    info = updater.UpdateInfo(version="9.9.9", tag="v9.9.9", assets={}, page="")
    monkeypatch.setattr(updater, "periodic_check", lambda config, log, *a, **k: info)
    assert cli.main(["--config", str(env.root / "config.json"), "backup", "--quiet"]) == 0
    assert [m["title"] for m in captured()] == ["Neue Version von SuperPyBackup"]


def test_notify_test_command(env, captured):
    env.write_config_file()
    assert cli.main(["--config", str(env.root / "config.json"), "notify-test"]) == 0
    assert len(captured()) == 1


def test_show_never_raises_when_powershell_is_missing(monkeypatch):
    monkeypatch.delenv("SUPERPYBACKUP_NOTIFY_FILE", raising=False)
    monkeypatch.setattr(notify, "_powershell", lambda: r"C:\gibt\es\nicht\powershell.exe")
    assert notify.show("a", "b") is False


def test_sender_name_is_registered_per_user_and_idempotent():
    import winreg

    app_id = "SuperPyBackup.Test.Absender"
    path = rf"Software\Classes\AppUserModelId\{app_id}"
    try:
        assert notify.register_app_id(app_id, "SuperPyBackup") is True
        assert notify.register_app_id(app_id, "SuperPyBackup") is True  # zweiter Aufruf aendert nichts
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            assert winreg.QueryValueEx(key, "DisplayName")[0] == "SuperPyBackup"
    finally:
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
        except OSError:
            pass
    assert notify.APP_ID == "SuperPyBackup.Desktop" and notify.APP_NAME == "SuperPyBackup"


# ---- geplante Updatepruefung (update check --quiet) ----------------------------------------------
def _quiet_check(env, monkeypatch, info):
    monkeypatch.setenv("SUPERPYBACKUP_HOME", str(env.home))
    monkeypatch.setattr(updater, "check_for_update", lambda settings, *a, **k: info)
    return cli.main(["--config", str(env.root / "config.json"), "update", "check", "--quiet"])


def test_quiet_update_check_notifies_once_per_version(env, captured, monkeypatch, capsys):
    env.write_config_file(notifications={"enabled": True})
    info = updater.UpdateInfo(version="9.9.9", tag="v9.9.9", assets={}, page="")
    assert _quiet_check(env, monkeypatch, info) == 0
    assert _quiet_check(env, monkeypatch, info) == 0  # dieselbe Version: keine zweite Meldung
    assert [m["title"] for m in captured()] == ["Neue Version von SuperPyBackup"]
    newer = updater.UpdateInfo(version="10.0.0", tag="v10.0.0", assets={}, page="")
    assert _quiet_check(env, monkeypatch, newer) == 0
    assert len(captured()) == 2
    assert capsys.readouterr().out == ""  # still


def test_quiet_update_check_is_silent_when_current_or_disabled(env, captured, monkeypatch):
    env.write_config_file(notifications={"enabled": True})
    assert _quiet_check(env, monkeypatch, None) == 0 and captured() == []
    env.write_config_file()  # Meldungen aus
    info = updater.UpdateInfo(version="9.9.9", tag="v9.9.9", assets={}, page="")
    assert _quiet_check(env, monkeypatch, info) == 0 and captured() == []
    assert "Updatepruefung: neue Version 9.9.9" in next((env.home / "Logs").glob("*.log")).read_text(encoding="utf-8")


def test_quiet_update_check_failure_is_logged_not_shown(env, captured, monkeypatch):
    env.write_config_file(notifications={"enabled": True})

    def fail(settings, *a, **k):
        raise updater.UpdateError("offline")

    monkeypatch.setenv("SUPERPYBACKUP_HOME", str(env.home))
    monkeypatch.setattr(updater, "check_for_update", fail)
    assert cli.main(["--config", str(env.root / "config.json"), "update", "check", "--quiet"]) == 1
    assert captured() == []
    assert "Updatepruefung nicht moeglich: offline" in next((env.home / "Logs").glob("*.log")).read_text(encoding="utf-8")
