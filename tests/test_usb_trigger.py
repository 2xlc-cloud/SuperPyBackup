"""Start beim Anstecken eines Laufwerks (Ereignisauslöser der Aufgabe, Ereignisprotokoll nur mit Administratorrechten einschaltbar)."""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET

import pytest

from superpybackup import cli, scheduler, vss

from test_scheduler import task_name  # noqa: F401

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
SID = "S-1-5-21-1-2-3-1001"


def test_xml_has_the_device_trigger_only_when_requested():
    plain = ET.fromstring(scheduler.build_xml("p.exe", "backup", SID).split("?>", 1)[1])
    assert plain.find(".//t:EventTrigger", NS) is None
    usb = ET.fromstring(scheduler.build_xml("p.exe", "backup", SID, on_usb=True).split("?>", 1)[1])
    triggers = usb.findall(".//t:EventTrigger", NS)
    assert len(triggers) == 1
    query = triggers[0].find("t:Subscription", NS).text
    assert "DriverFrameworks-UserMode/Operational" in query and "EventID=2003" in query and triggers[0].find("t:Delay", NS).text == "PT30S"
    both = ET.fromstring(scheduler.build_xml("p.exe", "backup", SID, on_usb=True, retry_minutes=30).split("?>", 1)[1])
    assert len(both.findall(".//t:EventTrigger", NS)) == 2  # Netzwerk und Laufwerk


def test_real_task_roundtrip_with_device_trigger(task_name):  # noqa: F811
    cmd = os.path.join(os.environ["SystemRoot"], "System32", "cmd.exe")
    scheduler.install(task_name, cmd, "/c exit 0", on_usb=True)
    info = scheduler.status(task_name)
    assert info["on_usb"] is True and info["on_network"] is False
    scheduler.install(task_name, cmd, "/c exit 0", retry_minutes=30)
    info = scheduler.status(task_name)
    assert info["on_usb"] is False and info["on_network"] is True  # die beiden Ereignisse werden nicht verwechselt


def test_reading_the_log_state_never_raises():
    assert scheduler.usb_log_enabled() in (True, False, None)


@pytest.fixture
def cli_env(env, monkeypatch):
    env.write_config_file()
    seen: dict = {}
    monkeypatch.setattr(scheduler, "install", lambda name, program, arguments, **kw: seen.update(arguments=arguments, **kw))
    calls: list[bool] = []
    monkeypatch.setattr(scheduler, "set_usb_log", lambda enabled: calls.append(enabled))
    return env, seen, calls


def install(env, *extra):
    return cli.main(["--config", str(env.root / "config.json"), "task", "install", "--name", "SuperPyBackup-Test-Usb", "--on-usb", *extra])


def test_without_admin_and_with_the_log_off_nothing_is_installed(cli_env, monkeypatch, capsys):
    env, seen, calls = cli_env
    monkeypatch.setattr(scheduler, "usb_log_enabled", lambda: False)
    monkeypatch.setattr(vss, "is_admin", lambda: False)
    assert install(env) == 1
    err = capsys.readouterr().err
    assert "Administrator" in err and "ausgeschaltet" in err and seen == {} and calls == []


def test_with_the_log_already_on_a_normal_user_can_install(cli_env, monkeypatch, capsys):
    env, seen, calls = cli_env
    monkeypatch.setattr(scheduler, "usb_log_enabled", lambda: True)
    monkeypatch.setattr(vss, "is_admin", lambda: False)
    assert install(env) == 0
    assert seen["on_usb"] is True and "--if-available" in seen["arguments"] and calls == []  # nichts am System umgeschaltet
    assert "Anstecken" in capsys.readouterr().out


def test_admin_switches_the_log_on_then_installs(cli_env, monkeypatch, capsys):
    env, seen, calls = cli_env
    monkeypatch.setattr(scheduler, "usb_log_enabled", lambda: False)
    monkeypatch.setattr(vss, "is_admin", lambda: True)
    assert install(env) == 0 and calls == [True] and seen["on_usb"] is True
    assert "eingeschaltet" in capsys.readouterr().out


def test_failure_to_switch_the_log_on_installs_nothing(cli_env, monkeypatch, capsys):
    env, seen, calls = cli_env
    monkeypatch.setattr(scheduler, "usb_log_enabled", lambda: None)
    monkeypatch.setattr(vss, "is_admin", lambda: True)

    def refuse(enabled):
        raise scheduler.SchedulerError("Ereignisprotokoll nicht umschaltbar")

    monkeypatch.setattr(scheduler, "set_usb_log", refuse)
    assert install(env) == 1 and seen == {} and "nicht umschaltbar" in capsys.readouterr().err
