"""Desktop-Meldungen (Windows-Toast) fuer Sicherung, Update und Fehler - auch im Betrieb als geplante Aufgabe.

Die Meldung wird ueber ein verstecktes Windows PowerShell angezeigt (WinRT-Toast, keine Zusatzpakete, kein Fenster).
Alles hier ist fehlertolerant: eine nicht anzeigbare Meldung darf nie eine Sicherung oder einen Befehl beeintraechtigen.
Zum Testen schreibt SUPERPYBACKUP_NOTIFY_FILE die Meldungen als JSON-Zeilen in eine Datei, statt sie anzuzeigen.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
from pathlib import Path

SUCCESS = "success"
UPDATE = "update"
ERROR = "error"
EVENT_KEYS = {SUCCESS: "on_success", UPDATE: "on_update", ERROR: "on_error"}
CREATE_NO_WINDOW = 0x08000000
APP_ID = "SuperPyBackup.Desktop"  # eigene AppUserModelID: Windows zeigt ihren DisplayName als Absender
APP_NAME = "SuperPyBackup"

_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
[void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml('<toast><visual><binding template="ToastGeneric"><text></text><text></text></binding></visual></toast>')
$texts = $xml.GetElementsByTagName('text')
$texts.Item(0).InnerText = $env:SPB_TITLE
$texts.Item(1).InnerText = $env:SPB_MESSAGE
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:SPB_APPID).Show($toast)
"""


def _powershell() -> str:
    root = os.environ.get("SystemRoot", r"C:\Windows")
    return os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")


def register_app_id(app_id: str = APP_ID, display_name: str = APP_NAME) -> bool:
    """Legt (idempotent) den Absendernamen im Benutzerzweig an: HKCU\\Software\\Classes\\AppUserModelId\\<ID>. Keine Administratorrechte noetig."""
    try:
        import winreg

        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, rf"Software\Classes\AppUserModelId\{app_id}", 0, winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as key:
            try:
                if winreg.QueryValueEx(key, "DisplayName")[0] == display_name:
                    return True
            except FileNotFoundError:
                pass
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, display_name)
            winreg.SetValueEx(key, "ShowInSettings", 0, winreg.REG_DWORD, 1)
        return True
    except OSError:
        return False


def show(title: str, message: str) -> bool:
    """Zeigt eine Desktop-Meldung. True, wenn sie abgesetzt wurde; nie eine Ausnahme."""
    title, message = title[:120], message[:400]
    capture = os.environ.get("SUPERPYBACKUP_NOTIFY_FILE")
    if capture:
        try:
            with open(capture, "a", encoding="utf-8") as handle:
                handle.write(json.dumps({"title": title, "message": message}, ensure_ascii=False) + "\n")
            return True
        except OSError:
            return False
    try:
        register_app_id()  # ohne Eintrag lehnt Windows die Meldung ab; dann meldet PowerShell den Fehler und show() liefert False
        encoded = base64.b64encode(_SCRIPT.encode("utf-16-le")).decode("ascii")
        env = dict(os.environ, SPB_TITLE=title, SPB_MESSAGE=message, SPB_APPID=APP_ID)
        result = subprocess.run(
            [_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
            capture_output=True, timeout=30, env=env, creationflags=CREATE_NO_WINDOW,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def enabled_for(settings: dict | None, event: str) -> bool:
    settings = settings or {}
    return bool(settings.get("enabled")) and bool(settings.get(EVENT_KEYS[event], True))


def notify(settings: dict | None, event: str, title: str, message: str, log=None) -> bool:
    """Zeigt die Meldung nur, wenn Desktop-Meldungen eingeschaltet und fuer dieses Ereignis aktiv sind."""
    if not enabled_for(settings, event):
        return False
    shown = show(title, message)
    if log is not None:
        try:
            (log.info if shown else log.warn)(f"Desktop-Meldung {'angezeigt' if shown else 'nicht moeglich'}: {title}")
        except Exception:  # noqa: BLE001
            pass
    return shown


def notify_from_file(config_path: str | None, event: str, title: str, message: str) -> bool:
    """Fuer Fehler, bei denen die Konfiguration selbst nicht geladen werden konnte: Einstellung roh aus der JSON-Datei lesen."""
    try:
        from . import config as cfgmod

        path = Path(config_path) if config_path else cfgmod.default_config_path()
        settings = json.loads(path.read_text(encoding="utf-8")).get("notifications")
        return notify(settings if isinstance(settings, dict) else None, event, title, message)
    except Exception:  # noqa: BLE001
        return False


def backup_message(result, kind_label: str = "", job: str = "") -> tuple[str, str, str] | None:
    """(Ereignis, Titel, Text) fuer ein Sicherungsergebnis; None, wenn nichts zu melden ist (bereits gesichert, anderer Lauf aktiv)."""
    from . import engine

    stats = result.stats
    who = f" ({job})" if job else ""
    if result.status == engine.COMPLETE:
        art = f" ({kind_label})" if kind_label else ""
        return SUCCESS, "Sicherung abgeschlossen" + who, f"Neu {stats.new}, geaendert {stats.modified}, unveraendert {stats.unchanged}{art}."
    if result.status == engine.PARTIAL:
        return ERROR, "Sicherung nur teilweise erfolgreich" + who, f"{stats.errors} Datei(en) konnten nicht gesichert werden. Details im Protokoll."
    if result.status == engine.FAILED:
        return ERROR, "Sicherung fehlgeschlagen" + who, result.message or "Unbekannter Fehler. Details im Protokoll."
    return None

