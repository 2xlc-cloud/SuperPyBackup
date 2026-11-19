"""Netzwerkfreigaben (UNC-Pfade) als Sicherungsziel: Anmeldung mit hinterlegten Anmeldedaten und Verbindungstest.

Die Verbindung wird ohne Laufwerksbuchstaben aufgebaut (WNetAddConnection2) und gilt nur fuer die Windows-Sitzung des Benutzers;
sie wird nicht dauerhaft gemerkt. Ohne hinterlegte Anmeldedaten arbeitet SuperPyBackup mit dem Windows-Konto wie bisher.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from dataclasses import dataclass, field

from . import credstore, fsutil, winapi

ERROR_SESSION_CREDENTIAL_CONFLICT = 1219
ERROR_ALREADY_ASSIGNED = 85

MESSAGES = {
    5: "Zugriff verweigert (Benutzer hat auf der Freigabe keine Berechtigung).",
    53: "Der Netzwerkpfad wurde nicht gefunden (Servername, Netzwerk oder Firewall pruefen).",
    55: "Die Freigabe ist nicht mehr verfuegbar.",
    67: "Der Netzwerkname wurde nicht gefunden (Name der Freigabe pruefen).",
    86: "Das Kennwort ist falsch.",
    1203: "Es ist kein Netzwerkanbieter fuer diesen Pfad verfuegbar.",
    1219: "Zu diesem Server besteht bereits eine Verbindung mit anderen Anmeldedaten (Windows erlaubt nur eine je Server). Bestehende Verbindung trennen mit: net use \\\\Server\\Freigabe /delete",
    1326: "Anmeldung fehlgeschlagen: Benutzername oder Kennwort ist falsch.",
    1327: "Das Konto ist eingeschraenkt (Anmeldung nicht erlaubt).",
    1331: "Das Konto ist deaktiviert.",
    1909: "Das Konto ist gesperrt.",
    2250: "Die Netzwerkverbindung besteht nicht.",
}

_connected: dict[str, str] = {}  # Freigabe -> Benutzer, mit dem dieser Prozess verbunden hat


class _NetResource(ctypes.Structure):
    _fields_ = [
        ("dwScope", wintypes.DWORD), ("dwType", wintypes.DWORD), ("dwDisplayType", wintypes.DWORD), ("dwUsage", wintypes.DWORD),
        ("lpLocalName", wintypes.LPWSTR), ("lpRemoteName", wintypes.LPWSTR), ("lpComment", wintypes.LPWSTR), ("lpProvider", wintypes.LPWSTR),
    ]


def describe(code: int) -> str:
    return MESSAGES.get(code, f"Windows-Fehler {code}")


def share_name(path: str) -> str | None:
    r"""\\Server\Freigabe (in der vom Benutzer eingegebenen Schreibweise) aus einem UNC-Pfad; None bei lokalen Pfaden."""
    if credstore.share_key(path) is None:
        return None
    drive = os.path.splitdrive(os.path.normpath(path))[0]
    parts = [part for part in drive[2:].split("\\") if part]
    return "\\\\" + parts[0] + "\\" + parts[1]


def connect(share: str, user: str, password: str) -> int:
    """Meldet sich an der Freigabe an. 0 = verbunden; sonst der Windows-Fehlercode."""
    if not winapi.IS_WINDOWS:
        return 1
    mpr = ctypes.windll.mpr
    mpr.WNetAddConnection2W.argtypes = [ctypes.POINTER(_NetResource), wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    mpr.WNetAddConnection2W.restype = wintypes.DWORD
    resource = _NetResource(0, 1, 0, 0, None, share, None, None)  # RESOURCETYPE_DISK
    return int(mpr.WNetAddConnection2W(ctypes.byref(resource), password, user, 0))


def disconnect(share: str) -> int:
    if not winapi.IS_WINDOWS:
        return 1
    mpr = ctypes.windll.mpr
    mpr.WNetCancelConnection2W.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.BOOL]
    mpr.WNetCancelConnection2W.restype = wintypes.DWORD
    return int(mpr.WNetCancelConnection2W(share, 0, False))


def reset(path: str) -> None:
    """Vergisst die Verbindung dieses Prozesses und trennt sie (z. B. nach geaenderten Anmeldedaten)."""
    key = credstore.share_key(path)
    name = share_name(path)
    if key and key in _connected:
        _connected.pop(key, None)
        if name:
            disconnect(name)


def ensure_connected(root: str, log=None) -> str | None:
    """Stellt bei einem UNC-Ziel mit hinterlegten Anmeldedaten die Verbindung her (einmal je Prozess).

    Liefert eine Fehlermeldung oder None. Ohne hinterlegte Anmeldedaten und bei lokalen Pfaden passiert nichts.
    """
    key = credstore.share_key(root)
    if key is None or key in _connected:
        return None
    try:
        creds = credstore.load(root)
    except credstore.CredentialError as error:
        message = str(error)
        if log:
            log.warn("Anmeldedaten: " + message)
        return message
    if creds is None:
        return None
    user, password = creds
    name = share_name(root) or key
    code = connect(name, user, password)
    if code == 0:
        _connected[key] = user
        if log:
            log.info(f"Mit Freigabe {name} als {user} verbunden.")
        return None
    if code == ERROR_SESSION_CREDENTIAL_CONFLICT and os.path.isdir(root):
        _connected[key] = user  # Verbindung besteht schon und funktioniert: weiterarbeiten
        if log:
            log.warn(f"Zu {name} besteht bereits eine Verbindung mit anderen Anmeldedaten; sie wird verwendet.")
        return None
    message = f"Anmeldung an {name} als {user} fehlgeschlagen: {describe(code)}"
    if log:
        log.error(message)
    return message


@dataclass
class TestResult:
    ok: bool = True
    lines: list[str] = field(default_factory=list)

    def add(self, text: str, ok: bool = True) -> None:
        self.lines.append(("OK      " if ok else "FEHLER  ") + text)
        self.ok = self.ok and ok

    def info(self, text: str) -> None:
        self.lines.append("Hinweis " + text)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def test_target(root: str, user: str | None = None, password: str | None = None) -> TestResult:
    """Prueft ein Sicherungsziel: Anmeldung (falls Freigabe), Ordner, Schreiben/Lesen/Loeschen, Hardlinks, freier Speicher.

    Mit user/password werden diese Anmeldedaten geprueft (ohne sie zu speichern), sonst die hinterlegten.
    Eine fuer den Test aufgebaute Verbindung wird danach wieder getrennt.
    """
    result = TestResult()
    root = (root or "").strip()
    if not root or not os.path.isabs(root):
        result.add("Das Sicherungsziel ist kein absoluter Pfad.", False)
        return result
    key, name = credstore.share_key(root), share_name(root)
    opened: str | None = None
    try:
        if key is None:
            result.add("Lokaler Pfad: keine Anmeldung noetig.")
        else:
            try:
                creds = (user, password or "") if user else credstore.load(root)
            except credstore.CredentialError as error:
                result.add(str(error), False)
                return result
            if creds is None:
                result.info(f"Fuer {name} sind keine Anmeldedaten hinterlegt; es wird mit dem Windows-Konto zugegriffen.")
            elif key in _connected and not user:
                result.add(f"Bereits mit {name} als {_connected[key]} verbunden.")
            else:
                code = connect(name, creds[0], creds[1])
                if code == 0:
                    result.add(f"Anmeldung an {name} als {creds[0]} erfolgreich.")
                    if key not in _connected:
                        opened = name
                elif code == ERROR_SESSION_CREDENTIAL_CONFLICT:
                    result.info(describe(code))
                else:
                    result.add(f"Anmeldung an {name} als {creds[0]} fehlgeschlagen: {describe(code)}", False)
                    return result
        if not os.path.isdir(fsutil.fs(root)):
            result.add(f"Der Ordner ist nicht erreichbar oder existiert nicht: {root}", False)
            return result
        result.add(f"Ordner erreichbar: {root}")
        probe = os.path.join(root, f".spb-test-{fsutil.new_id(12)}")
        try:
            with open(fsutil.fs(probe), "wb") as handle:
                handle.write(b"SuperPyBackup")
            with open(fsutil.fs(probe), "rb") as handle:
                same = handle.read() == b"SuperPyBackup"
            os.remove(fsutil.fs(probe))
            result.add("Schreiben, Lesen und Loeschen einer Testdatei funktionieren." if same else "Die Testdatei wurde nicht korrekt gelesen.", same)
        except OSError as error:
            result.add(f"Keine Schreibrechte im Ordner: {error}", False)
            try:
                os.remove(fsutil.fs(probe))
            except OSError:
                pass
            return result
        if winapi.volume_supports_hardlinks(root):
            result.add("Hardlinks werden unterstuetzt (Inkremente sparen Speicher).")
        else:
            result.info("Das Ziel unterstuetzt keine Hardlinks: Alle Sicherungen werden vollstaendig kopiert (mehr Speicherbedarf).")
        try:
            free = fsutil.disk_free(root)
            result.add(f"Freier Speicher: {free / 1024 ** 3:.1f} GB.")
        except OSError as error:
            result.info(f"Freier Speicher nicht ermittelbar: {error}")
        return result
    finally:
        if opened:
            disconnect(opened)
