"""Autostart der Oberflaeche beim Anmelden: Wert im Benutzerzweig HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run.

Gilt nur fuer den angemeldeten Benutzer, braucht keine Administratorrechte und aendert keine Systemeinstellungen.
Gestartet wird `SuperPyBackup.exe [--config ...] gui --tray`: die Oberflaeche erscheint nur als Symbol im Infobereich.
"""

from __future__ import annotations

import winreg

from . import scheduler

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
DEFAULT_NAME = "SuperPyBackup"


class AutostartError(Exception):
    pass


def command(config_path: str | None) -> str:
    program, arguments = scheduler.program_command(config_path, "gui --tray")
    return f'"{program}" {arguments}'


def current(name: str = DEFAULT_NAME) -> str | None:
    """Eingetragener Befehl oder None, wenn kein Autostart eingerichtet ist."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_QUERY_VALUE) as key:
            return str(winreg.QueryValueEx(key, name)[0])
    except FileNotFoundError:
        return None
    except OSError as error:
        raise AutostartError(f"Autostart konnte nicht gelesen werden: {error}") from error


def is_enabled(name: str = DEFAULT_NAME) -> bool:
    return current(name) is not None


def enable(config_path: str | None, name: str = DEFAULT_NAME) -> str:
    """Traegt den Autostart ein oder aktualisiert ihn (idempotent). Liefert den eingetragenen Befehl."""
    value = command(config_path)
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
    except OSError as error:
        raise AutostartError(f"Autostart konnte nicht eingerichtet werden: {error}") from error
    return value


def disable(name: str = DEFAULT_NAME) -> bool:
    """Entfernt den Autostart. True, wenn er vorhanden war."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, name)
        return True
    except FileNotFoundError:
        return False
    except OSError as error:
        raise AutostartError(f"Autostart konnte nicht entfernt werden: {error}") from error
