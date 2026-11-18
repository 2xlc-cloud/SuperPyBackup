"""Test 25: Installation und Entfernung der Windows-Aufgabe (nur mit eindeutigem Testnamen; fremde Aufgaben bleiben unberuehrt)."""

from __future__ import annotations

import os
import subprocess
import uuid
import xml.etree.ElementTree as ET

import pytest

from superpybackup import scheduler


def all_task_names() -> set[str]:
    output = subprocess.run(["schtasks.exe", "/Query", "/FO", "CSV", "/NH"], capture_output=True, creationflags=0x08000000).stdout
    text = scheduler._decode(output)
    return {line.split('","')[0].strip('"').lstrip("\\") for line in text.splitlines() if line.strip()}


@pytest.fixture
def task_name():
    name = "SuperPyBackupTest-" + uuid.uuid4().hex[:10]
    yield name
    try:
        scheduler.uninstall(name)
    except scheduler.SchedulerError:
        pass


CMD = os.path.join(os.environ["SystemRoot"], "System32", "cmd.exe")


def test_xml_matches_requirements():
    xml = scheduler.build_xml("C:\\x\\SuperPyBackup.exe", '--config "C:\\a b\\config.json" backup --quiet', "S-1-5-21-1-2-3-1001", delay_minutes=2)
    root = ET.fromstring(xml.split("?>", 1)[1])
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    assert root.find(".//t:LogonTrigger/t:Delay", ns).text == "PT2M"
    assert root.find(".//t:LogonTrigger/t:UserId", ns).text == "S-1-5-21-1-2-3-1001"
    assert root.find(".//t:Principal/t:LogonType", ns).text == "InteractiveToken"  # Kontext des angemeldeten Benutzers
    assert root.find(".//t:Principal/t:RunLevel", ns).text == "LeastPrivilege"  # keine Administratorrechte
    assert root.find(".//t:Settings/t:Hidden", ns).text == "true"
    assert root.find(".//t:Settings/t:MultipleInstancesPolicy", ns).text == "IgnoreNew"  # keine parallelen Prozesse
    assert root.find(".//t:Exec/t:Arguments", ns).text == '--config "C:\\a b\\config.json" backup --quiet'
    assert root.find(".//t:CalendarTrigger", ns) is None


@pytest.mark.parametrize("bad", ["SuperPSBackup-S-1-5-21", "UserProfileBackup-x", "Fremd", "SuperPyBackup x", "SuperPyBackup/..", ""])
def test_only_superpybackup_task_names_are_accepted(bad):
    with pytest.raises(scheduler.SchedulerError):
        scheduler.validate_name(bad)


def test_install_check_change_and_uninstall(task_name):
    before = all_task_names()
    scheduler.install(task_name, CMD, "/c exit 0", delay_minutes=2)
    info = scheduler.status(task_name)
    assert info["exists"] and info["delay_minutes"] == 2 and info["daily_at"] is None
    assert info["hidden"] and info["enabled"] and info["run_level"] == "LeastPrivilege" and info["multiple_instances"] == "IgnoreNew"
    assert info["command"].lower() == CMD.lower() and info["arguments"] == "/c exit 0"
    assert scheduler.check(task_name) == []
    assert task_name in all_task_names()

    scheduler.install(task_name, CMD, "/c exit 0", delay_minutes=7, daily_at="06:45")  # Aenderung = erneutes Einrichten, keine zweite Aufgabe
    changed = scheduler.status(task_name)
    assert changed["delay_minutes"] == 7 and changed["daily_at"] == "06:45"
    assert sorted(all_task_names() - before) == [task_name]

    assert scheduler.uninstall(task_name) is True
    assert scheduler.status(task_name)["exists"] is False
    assert scheduler.uninstall(task_name) is False
    assert all_task_names() == before  # nichts anderes wurde veraendert


def test_check_reports_deviations(task_name):
    scheduler.install(task_name, os.path.join(os.environ["SystemRoot"], "System32", "does-not-exist.exe"), "", delay_minutes=2)
    problems = scheduler.check(task_name)
    assert any("existiert nicht" in problem for problem in problems)
    assert scheduler.check("SuperPyBackupTest-gibt-es-nicht") == ["Aufgabe SuperPyBackupTest-gibt-es-nicht ist nicht eingerichtet."]


def test_invalid_delay_and_time_are_rejected():
    for kwargs in ({"delay_minutes": 61}, {"delay_minutes": -1}, {"daily_at": "25:00"}, {"daily_at": "abc"}):
        with pytest.raises(scheduler.SchedulerError):
            scheduler.build_xml("x.exe", "", "S-1-5-1", **kwargs)


def test_existing_superpsbackup_tasks_are_untouched(task_name):
    before = {name for name in all_task_names() if "SuperPSBackup" in name or "UserProfileBackup" in name}
    scheduler.install(task_name, CMD, "/c exit 0")
    scheduler.uninstall(task_name)
    after = {name for name in all_task_names() if "SuperPSBackup" in name or "UserProfileBackup" in name}
    assert before == after


def test_cli_task_install_and_uninstall(env, task_name):
    env.write_config_file()
    installed = env.cli("task", "install", "--name", task_name, "--delay", "3", "--daily-at", "05:30")
    assert installed.returncode == 0, installed.stderr
    info = scheduler.status(task_name)
    assert info["delay_minutes"] == 3 and info["daily_at"] == "05:30"
    assert str(env.root / "config.json") in info["arguments"] and "backup --quiet" in info["arguments"]  # Konfiguration ausserhalb des Programmordners
    assert env.cli("task", "check", "--name", task_name).returncode == 0
    refused = env.cli("task", "install", "--name", "SuperPSBackup-S-1-5-21-x")
    assert refused.returncode != 0
    assert env.cli("task", "uninstall", "--name", task_name).returncode == 0
    assert scheduler.status(task_name)["exists"] is False


def test_cli_refuses_task_install_with_invalid_config(env, task_name):
    (env.root / "config.json").write_text('{"backup_root": ""}', encoding="utf-8")
    result = env.cli("task", "install", "--name", task_name)
    assert result.returncode != 0 and scheduler.status(task_name)["exists"] is False


def test_update_task_helpers_and_description():
    assert scheduler.update_task_name("SuperPyBackup-S-1-5-21-1") == "SuperPyBackup-S-1-5-21-1-Update"
    scheduler.validate_name(scheduler.update_task_name("SuperPyBackup-S-1-5-21-1"))
    _, arguments = scheduler.program_command("C:\\a\\config.json", "update check --quiet")
    assert arguments.endswith('--config "C:\\a\\config.json" update check --quiet') or arguments.endswith('update check --quiet')
    xml = scheduler.build_xml("C:\\x\\SuperPyBackup.exe", "update check --quiet", "S-1-5-21-1-2-3-1001", daily_at="12:00", description="Pruefung & Meldung")
    root = ET.fromstring(xml.split("?>", 1)[1])
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    assert root.find(".//t:RegistrationInfo/t:Description", ns).text == "Pruefung & Meldung"
    assert root.find(".//t:Exec/t:Arguments", ns).text == "update check --quiet" and root.find(".//t:CalendarTrigger", ns) is not None
