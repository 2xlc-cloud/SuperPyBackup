"""Administratorrechte auf Anfrage: Hilfsprozess ohne die echte Windows-Abfrage (Verb "open" statt "runas") und Befehl usb-log."""

from __future__ import annotations

import os
import sys

import pytest

from superpybackup import cli, elevate, scheduler, vss

CMD = os.path.join(os.environ["SystemRoot"], "System32", "cmd.exe")


def test_exit_code_comes_back_from_the_started_process():
    assert elevate.shell_execute(CMD, "/c exit 3", verb="open", timeout_ms=30000) == 3
    assert elevate.shell_execute(CMD, "/c exit 0", verb="open", timeout_ms=30000) == 0


def test_missing_program_is_reported_not_swallowed():
    with pytest.raises(elevate.ElevationError):
        elevate.shell_execute(r"C:\gibt\es\nicht.exe", "", verb="open", timeout_ms=5000)


def test_command_line_redirects_output_and_keeps_spaces_intact():
    program, parameters = elevate.cmd_line(["--config", r"C:\mit Leerzeichen\config.json", "usb-log", "status"], r"C:\Temp\ausgabe.txt")
    assert program.lower().endswith("cmd.exe") and parameters.startswith('/s /c "') and parameters.endswith(r'> "C:\Temp\ausgabe.txt" 2>&1"')
    assert r'"C:\mit Leerzeichen\config.json"' in parameters and "usb-log status" in parameters


def test_run_cli_collects_the_output_of_a_real_console_run(monkeypatch):
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
    monkeypatch.setenv("PYTHONPATH", src)
    code, output = elevate.run_cli(["usb-log", "status"], verb="open", timeout_ms=60000)  # ohne Windows-Abfrage: nur der Weg ueber cmd und die Temp-Datei
    assert code == 0 and "Ereignisprotokoll" in output and sys.executable  # Ausgabe kam zurueck
    code, output = elevate.run_cli(["--version"], verb="open", timeout_ms=60000)
    assert code == 0 and output.strip()


@pytest.fixture
def usb(monkeypatch):
    state = {"enabled": False, "set": []}
    monkeypatch.setattr(scheduler, "usb_log_enabled", lambda: state["enabled"])

    def set_log(enabled):
        state["set"].append(enabled)
        state["enabled"] = enabled

    monkeypatch.setattr(scheduler, "set_usb_log", set_log)
    return state


def test_usb_log_status_needs_no_rights(usb, monkeypatch, capsys):
    monkeypatch.setattr(vss, "is_admin", lambda: False)
    assert cli.main(["usb-log", "status"]) == 0 and "ausgeschaltet" in capsys.readouterr().out


def test_usb_log_switching_needs_admin(usb, monkeypatch, capsys):
    monkeypatch.setattr(vss, "is_admin", lambda: False)
    assert cli.main(["usb-log", "on"]) == 1 and usb["set"] == [] and "Administrator" in capsys.readouterr().err
    assert cli.main(["usb-log", "off"]) == 0 and usb["set"] == []  # ist schon aus: nichts zu tun, auch ohne Rechte
    monkeypatch.setattr(vss, "is_admin", lambda: True)
    assert cli.main(["usb-log", "on"]) == 0 and usb["set"] == [True] and "eingeschaltet" in capsys.readouterr().out
    assert cli.main(["usb-log", "on"]) == 0 and usb["set"] == [True]  # schon an: nicht erneut
    assert cli.main(["usb-log", "off"]) == 0 and usb["set"] == [True, False]


def test_usb_log_switch_failure_is_reported(usb, monkeypatch, capsys):
    monkeypatch.setattr(vss, "is_admin", lambda: True)

    def refuse(enabled):
        raise scheduler.SchedulerError("Ereignisprotokoll nicht umschaltbar")

    monkeypatch.setattr(scheduler, "set_usb_log", refuse)
    assert cli.main(["usb-log", "on"]) == 1 and "nicht umschaltbar" in capsys.readouterr().err
