"""Windows-Aufgabenplanung: Aufgabe anlegen, pruefen, aendern, entfernen (ueber schtasks.exe und Aufgaben-XML)."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

from . import winapi

NAME_PATTERN = re.compile(r"^SuperPyBackup[A-Za-z0-9._-]*$")
NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
CREATE_NO_WINDOW = 0x08000000


class SchedulerError(Exception):
    pass


def default_task_name() -> str:
    return "SuperPyBackup-" + winapi.current_user_sid()


def validate_name(name: str) -> str:
    """Nur Aufgaben, die eindeutig SuperPyBackup zugeordnet sind (nie SuperPSBackup oder fremde Aufgaben)."""
    if not NAME_PATTERN.match(name):
        raise SchedulerError(f"Ungueltiger Aufgabenname {name!r}: Er muss mit 'SuperPyBackup' beginnen und darf nur Buchstaben, Ziffern, '.', '_' und '-' enthalten.")
    return name


def update_task_name(task_name: str) -> str:
    """Name der Aufgabe fuer die Updatepruefung, abgeleitet vom Namen der Sicherungsaufgabe."""
    return task_name + "-Update"


def program_command(config_path: str | None, command_line: str = "backup --quiet") -> tuple[str, str]:
    """Programm und Argumente fuer den geplanten Lauf. Bevorzugt die konsolenlose SuperPyBackup.exe."""
    config_arg = f'--config "{config_path}" ' if config_path else ""
    if getattr(sys, "frozen", False):
        executable = Path(sys.executable)
        windowed = executable.with_name("SuperPyBackup.exe")
        program = windowed if windowed.is_file() else executable
        return str(program), f"{config_arg}{command_line}"
    python = Path(sys.executable)
    pythonw = python.with_name("pythonw.exe")
    program = pythonw if pythonw.is_file() else python
    return str(program), f"-m superpybackup {config_arg}{command_line}"


DESCRIPTION = "SuperPyBackup: versionierte Sicherung der persoenlichen Benutzerordner."


def build_xml(command: str, arguments: str, sid: str, *, delay_minutes: int = 2, daily_at: str | None = None, description: str = DESCRIPTION, elevated: bool = False, retry_minutes: int = 0) -> str:
    if not 0 <= delay_minutes <= 60:
        raise SchedulerError("Die Verzoegerung muss zwischen 0 und 60 Minuten liegen.")
    if retry_minutes != 0 and not 5 <= retry_minutes <= 1440:
        raise SchedulerError("Die Wiederholung muss 0 (aus) oder 5 bis 1440 Minuten betragen.")
    repeat = f"<Repetition><Interval>PT{retry_minutes}M</Interval></Repetition>" if retry_minutes else ""
    network = (
        "<EventTrigger><Enabled>true</Enabled><Subscription>&lt;QueryList&gt;&lt;Query Id='0' Path='Microsoft-Windows-NetworkProfile/Operational'&gt;&lt;Select Path='Microsoft-Windows-NetworkProfile/Operational'&gt;*[System[Provider[@Name='Microsoft-Windows-NetworkProfile'] and EventID=10000]]&lt;/Select&gt;&lt;/Query&gt;&lt;/QueryList&gt;</Subscription><Delay>PT1M</Delay></EventTrigger>"
        if retry_minutes else ""
    )
    daily = ""
    if daily_at:
        try:
            clock = datetime.strptime(daily_at, "%H:%M")
        except ValueError as error:
            raise SchedulerError("Die Uhrzeit muss im Format HH:MM angegeben werden.") from error
        boundary = datetime.now().replace(hour=clock.hour, minute=clock.minute, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%S")
        daily = f"<CalendarTrigger><StartBoundary>{boundary}</StartBoundary><Enabled>true</Enabled><ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>{repeat}</CalendarTrigger>"
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>{escape(description)}</Description></RegistrationInfo>
  <Triggers><LogonTrigger>{repeat}<Enabled>true</Enabled><Delay>PT{delay_minutes}M</Delay><UserId>{escape(sid)}</UserId></LogonTrigger>{daily}{network}</Triggers>
  <Principals><Principal id="User"><UserId>{escape(sid)}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>{'HighestAvailable' if elevated else 'LeastPrivilege'}</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate><StartWhenAvailable>true</StartWhenAvailable>
    <Enabled>true</Enabled><Hidden>true</Hidden><ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
  </Settings>
  <Actions Context="User"><Exec><Command>{escape(command)}</Command><Arguments>{escape(arguments)}</Arguments></Exec></Actions>
</Task>
"""


def _run(arguments: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["schtasks.exe", *arguments], capture_output=True, creationflags=CREATE_NO_WINDOW, timeout=60)


def _decode(data: bytes) -> str:
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16")
    for encoding in ("utf-8", "cp850", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def install(name: str, command: str, arguments: str, *, delay_minutes: int = 2, daily_at: str | None = None, sid: str | None = None, description: str = DESCRIPTION, elevated: bool = False, retry_minutes: int = 0) -> str:
    """Legt die Aufgabe an oder aktualisiert sie (idempotent). Aufgaben anderer Namen bleiben unberuehrt."""
    validate_name(name)
    xml = build_xml(command, arguments, sid or winapi.current_user_sid(), delay_minutes=delay_minutes, daily_at=daily_at, description=description, elevated=elevated, retry_minutes=retry_minutes)
    with tempfile.TemporaryDirectory(prefix="superpybackup-task-") as folder:
        path = os.path.join(folder, "task.xml")
        with open(path, "wb") as handle:
            handle.write(b"\xff\xfe" + xml.encode("utf-16-le"))
        result = _run(["/Create", "/TN", name, "/XML", path, "/F"])
    if result.returncode != 0:
        raise SchedulerError(f"Aufgabe konnte nicht angelegt werden: {_decode(result.stderr or result.stdout).strip()}")
    return name


def status(name: str) -> dict[str, object]:
    """Liest die tatsaechliche Konfiguration der Aufgabe. exists=False, wenn sie nicht vorhanden ist."""
    validate_name(name)
    result = _run(["/Query", "/TN", name, "/XML"])
    if result.returncode != 0:
        return {"exists": False, "name": name}
    text = _decode(result.stdout).strip()
    text = re.sub(r"^<\?xml[^>]*\?>", "", text).strip()
    root = ET.fromstring(text)
    find = lambda path: root.find(path, NS)  # noqa: E731
    logon = find(".//t:LogonTrigger/t:Delay")
    boundary = find(".//t:CalendarTrigger/t:StartBoundary")
    delay = None
    if logon is not None and logon.text:
        match = re.fullmatch(r"PT(\d+)M", logon.text)
        delay = int(match.group(1)) if match else None
    daily = boundary.text[11:16] if boundary is not None and boundary.text else None
    interval = find(".//t:LogonTrigger/t:Repetition/t:Interval")
    retry = None
    if interval is not None and interval.text:
        match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?", interval.text)
        retry = (int(match.group(1) or 0) * 60 + int(match.group(2) or 0)) if match else None
    text_of = lambda path: (find(path).text if find(path) is not None else None)  # noqa: E731
    return {
        "exists": True,
        "name": name,
        "enabled": text_of(".//t:Settings/t:Enabled") != "false",
        "hidden": text_of(".//t:Settings/t:Hidden") == "true",
        "delay_minutes": delay,
        "daily_at": daily,
        "retry_minutes": retry or 0,
        "on_network": find(".//t:EventTrigger") is not None,
        "command": text_of(".//t:Actions/t:Exec/t:Command"),
        "arguments": text_of(".//t:Actions/t:Exec/t:Arguments"),
        # Windows laesst Standardwerte im exportierten XML weg: LeastPrivilege und IgnoreNew sind die Vorgaben.
        "run_level": text_of(".//t:Principals/t:Principal/t:RunLevel") or "LeastPrivilege",
        "multiple_instances": text_of(".//t:Settings/t:MultipleInstancesPolicy") or "IgnoreNew",
        "user_id": text_of(".//t:Principals/t:Principal/t:UserId"),
    }


def check(name: str, allow_elevated: bool = False) -> list[str]:
    """Prueft die Aufgabe gegen die Sollwerte; liefert Abweichungen (leer = in Ordnung)."""
    info = status(name)
    if not info["exists"]:
        return [f"Aufgabe {name} ist nicht eingerichtet."]
    problems = []
    if not info["enabled"]:
        problems.append("Aufgabe ist deaktiviert.")
    if info["run_level"] != "LeastPrivilege" and not allow_elevated:
        problems.append("Aufgabe laeuft mit erhoehten Rechten (gewollt nur fuer Schattenkopien: task check --elevated).")
    if not info["hidden"]:
        problems.append("Aufgabe ist nicht versteckt.")
    if info["multiple_instances"] != "IgnoreNew":
        problems.append("Parallele Instanzen sind nicht ausgeschlossen.")
    command = str(info["command"] or "")
    if not command or not os.path.exists(command):
        problems.append(f"Programm der Aufgabe existiert nicht: {command}")
    return problems


def uninstall(name: str) -> bool:
    validate_name(name)
    if not status(name)["exists"]:
        return False
    result = _run(["/Delete", "/TN", name, "/F"])
    if result.returncode != 0:
        raise SchedulerError(f"Aufgabe konnte nicht entfernt werden: {_decode(result.stderr or result.stdout).strip()}")
    return True
