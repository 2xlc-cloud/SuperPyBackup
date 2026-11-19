"""Punkt 6: Sicherung, wenn das Ziel (wieder) erreichbar ist - 'backup --if-available', Wiederholung und Netzwerkereignis der Aufgabe."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET

import pytest

from conftest import write_tree
from superpybackup import cli, credstore, engine, netshare, scheduler, staleness

from test_netshare import SECRET, SHARE, fake_wnet  # noqa: F401
from test_scheduler import task_name  # noqa: F401

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def gone(env):
    # der uebergeordnete Ordner fehlt auch, deshalb wird das Ziel nicht automatisch angelegt
    return str(env.root / "fehlt" / "ziel")


def test_missing_target_waits_quietly_when_asked_to(env):
    write_tree(env.src, {"a.txt": "a"})
    result = env.run(config_overrides={"backup_root": gone(env)}, if_available=True)
    assert result.status == engine.WAITING and result.exit_code == 0 and "nicht vorhanden" in result.message
    assert result.stats.errors == 0
    log = "".join(p.read_text(encoding="utf-8") for p in (env.home / "Logs").glob("*.log"))
    assert "[ERROR]" not in log and "spaeter erneut versucht" in log
    assert env.run(config_overrides={"backup_root": gone(env)}).status == engine.FAILED  # ohne die Option bleibt es ein Fehler


def test_once_the_target_is_back_the_backup_runs(env):
    write_tree(env.src, {"a.txt": "a"})
    missing = gone(env)
    assert env.run(config_overrides={"backup_root": missing}, if_available=True).status == engine.WAITING
    (env.root / "fehlt").mkdir()
    (env.root / "fehlt" / "ziel").mkdir()
    done = env.run(config_overrides={"backup_root": missing}, if_available=True)
    assert done.status == engine.COMPLETE and list((env.root / "fehlt" / "ziel").glob("*/Daily/*"))


def test_unreachable_share_waits_but_wrong_password_still_fails(env, fake_wnet, monkeypatch):  # noqa: F811
    calls, state = fake_wnet
    credstore.save(SHARE, "benutzer", SECRET)
    write_tree(env.src, {"a.txt": "a"})
    for code, status in ((53, engine.WAITING), (1231, engine.WAITING), (1326, engine.FAILED), (1219, engine.FAILED)):
        netshare.reset(SHARE)
        state["code"] = code
        result = env.run(config_overrides={"backup_root": SHARE + r"\Ziel"}, if_available=True)
        assert result.status == status, (code, result.status, result.message)
        assert SECRET not in result.message


def run_cli(env, sink, *args, monkeypatch):
    monkeypatch.setenv("SUPERPYBACKUP_NOTIFY_FILE", str(sink))
    return cli.main(["--config", str(env.root / "config.json"), "backup", "--quiet", *args])


def test_cli_flag_returns_zero_without_any_notification(env, tmp_path, monkeypatch):
    write_tree(env.src, {"a.txt": "a"})
    env.write_config_file(notifications={"enabled": True}, backup_root=gone(env))
    sink = tmp_path / "meldungen.jsonl"
    assert run_cli(env, sink, "--if-available", monkeypatch=monkeypatch) == 0 and not sink.exists()  # kein Fehler, keine Meldung
    assert run_cli(env, sink, monkeypatch=monkeypatch) == 1  # ohne --if-available wie bisher: Fehler und Meldung
    assert [m["title"] for m in map(json.loads, sink.read_text(encoding="utf-8").splitlines())] == ["Sicherung fehlgeschlagen"]


def test_a_long_wait_still_triggers_the_overdue_warning(env, tmp_path, monkeypatch):
    from datetime import datetime, timedelta

    write_tree(env.src, {"a.txt": "a"})
    env.write_config_file(notifications={"enabled": True}, backup={"warn_after_days": 1}, backup_root=gone(env))
    config = cli.cfgmod.load(str(env.root / "config.json"), require_target=False)
    staleness.record_success(config, datetime.now() - timedelta(days=3))
    sink = tmp_path / "meldungen.jsonl"
    assert run_cli(env, sink, "--if-available", monkeypatch=monkeypatch) == 0
    assert [m["title"] for m in map(json.loads, sink.read_text(encoding="utf-8").splitlines())] == ["Sicherung überfällig"]


def test_task_xml_has_repetition_and_network_trigger_only_when_requested():
    plain = ET.fromstring(scheduler.build_xml("p.exe", "backup", "S-1-5-21-1-2-3-1001", daily_at="07:15").split("?>", 1)[1])
    assert plain.find(".//t:Repetition", NS) is None and plain.find(".//t:EventTrigger", NS) is None
    xml = ET.fromstring(scheduler.build_xml("p.exe", "backup", "S-1-5-21-1-2-3-1001", daily_at="07:15", retry_minutes=30).split("?>", 1)[1])
    assert [e.text for e in xml.findall(".//t:Repetition/t:Interval", NS)] == ["PT30M", "PT30M"]  # nach Anmeldung und taeglich
    assert "NetworkProfile" in xml.find(".//t:EventTrigger/t:Subscription", NS).text and xml.find(".//t:EventTrigger/t:Delay", NS).text == "PT1M"
    for bad in (1, 4, 1441, -5):
        with pytest.raises(scheduler.SchedulerError):
            scheduler.build_xml("p.exe", "backup", "S-1-5-21-1-2-3-1001", retry_minutes=bad)


def test_real_task_roundtrip_with_retry(task_name):  # noqa: F811
    cmd = scheduler.os.path.join(scheduler.os.environ["SystemRoot"], "System32", "cmd.exe")
    scheduler.install(task_name, cmd, "/c exit 0", delay_minutes=2, daily_at="07:15", retry_minutes=45)
    info = scheduler.status(task_name)
    assert info["retry_minutes"] == 45 and info["on_network"] is True and info["daily_at"] == "07:15" and info["delay_minutes"] == 2
    assert scheduler.check(task_name) == []
    scheduler.install(task_name, cmd, "/c exit 0", delay_minutes=2)  # ohne Wiederholung wieder wie frueher
    info = scheduler.status(task_name)
    assert info["retry_minutes"] == 0 and info["on_network"] is False


def test_cli_task_install_passes_retry_and_flag(env, monkeypatch, capsys):
    env.write_config_file()
    seen = {}
    monkeypatch.setattr(scheduler, "install", lambda name, program, arguments, **kw: seen.update(arguments=arguments, **kw))
    assert cli.main(["--config", str(env.root / "config.json"), "task", "install", "--name", "SuperPyBackup-Test-Retry", "--retry-minutes", "20"]) == 0
    assert "--if-available" in seen["arguments"] and seen["retry_minutes"] == 20
    assert "Wiederholung: alle 20 Minuten" in capsys.readouterr().out
    seen.clear()
    assert cli.main(["--config", str(env.root / "config.json"), "task", "install", "--name", "SuperPyBackup-Test-Retry"]) == 0
    assert "--if-available" not in seen["arguments"] and seen["retry_minutes"] == 0
    seen.clear()
    assert cli.main(["--config", str(env.root / "config.json"), "task", "install", "--all", "--name", "SuperPyBackup-Test-RetryAlle", "--retry-minutes", "60"]) == 0
    assert "backup --all --quiet --if-available" in seen["arguments"]
