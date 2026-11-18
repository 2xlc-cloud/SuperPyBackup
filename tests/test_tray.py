"""Tests: Programmsymbol, Symbol im Infobereich, Autostart (nur mit eigenen Testnamen im Benutzerzweig)."""

from __future__ import annotations

import ctypes
import struct
import time
import uuid
import winreg

import pytest

from superpybackup import autostart, icon, tray


# ---- Symbol -------------------------------------------------------------------------------------
def test_png_and_ico_are_wellformed():
    png = icon.png_bytes(48)
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and struct.unpack(">II", png[16:24]) == (48, 48)
    ico = icon.ico_bytes((16, 32))
    assert struct.unpack("<HHH", ico[:6]) == (0, 1, 2)
    length, offset = struct.unpack("<II", ico[6 + 8:6 + 16])  # Eintrag: Groesse, dann Offset
    assert ico[offset:offset + 8] == b"\x89PNG\r\n\x1a\n" and length > 0


def test_icon_is_opaque_inside_and_transparent_in_the_corner():
    pixels = icon.render(32)
    assert pixels[3] == 0  # Ecke oben links: durchsichtig (abgerundet)
    middle = (16 * 32 + 16) * 4
    assert pixels[middle + 3] == 255


def _body_color(state: str) -> tuple[int, int, int]:
    pixels = icon.render(64, state)
    offset = (int(64 * 0.25) * 64 + int(64 * 0.12)) * 4  # Gehaeuse links oben, ausserhalb der Scheibe
    assert pixels[offset + 3] == 255
    return pixels[offset], pixels[offset + 1], pixels[offset + 2]


def test_state_colors_are_blue_orange_and_red():
    idle, busy, error = (_body_color(s) for s in (icon.IDLE, icon.BUSY, icon.ERROR))
    assert idle[2] > idle[0] and idle[2] > 150  # blau
    assert busy[0] > 200 and busy[0] > busy[2] + 80 and busy[1] > busy[2]  # orange
    assert error[0] > 180 and error[0] > error[1] + 80 and error[0] > error[2] + 80  # rot
    assert len({idle, busy, error}) == 3


def test_hicon_can_be_created_and_freed():
    handle = icon.create_hicon(32)
    assert handle
    assert ctypes.windll.user32.DestroyIcon(ctypes.c_void_p(handle))


# ---- Autostart -----------------------------------------------------------------------------------
@pytest.fixture
def autostart_name():
    name = "SuperPyBackupTest-" + uuid.uuid4().hex[:10]
    yield name
    try:
        autostart.disable(name)
    except autostart.AutostartError:
        pass


def test_autostart_enable_is_idempotent_and_disable_removes(autostart_name):
    assert autostart.is_enabled(autostart_name) is False and autostart.disable(autostart_name) is False
    value = autostart.enable("C:\\Daten\\config.json", autostart_name)
    assert autostart.enable("C:\\Daten\\config.json", autostart_name) == value
    assert autostart.current(autostart_name) == value
    assert value.endswith('--config "C:\\Daten\\config.json" gui --tray') and value.startswith('"')
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, autostart.RUN_KEY) as key:  # tatsaechlich im Benutzerzweig
        assert winreg.QueryValueEx(key, autostart_name)[0] == value
    assert autostart.disable(autostart_name) is True and autostart.is_enabled(autostart_name) is False


# ---- Symbol im Infobereich -----------------------------------------------------------------------
@pytest.fixture
def live_tray():
    if tray.user32.FindWindowW(tray.CLASS_NAME, None):
        pytest.skip("Es laeuft bereits eine SuperPyBackup-Oberflaeche mit Infobereich-Symbol")
    events: list[str] = []
    icon_ = tray.TrayIcon(events.append, "Test", [
        tray.MenuItem("open", "Oeffnen", default=True), None,
        tray.MenuItem("backup", "Sichern"), tray.MenuItem("aus", "Nicht waehlbar", enabled=False),
        tray.MenuItem("quit", "Beenden"),
    ])
    if not icon_.start():
        pytest.skip("Kein Infobereich verfuegbar")
    yield icon_, events
    icon_.stop()


def wait_for(predicate, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_tray_icon_reports_clicks_and_menu_commands(live_tray):
    icon_, events = live_tray
    post = lambda message, wparam=0, lparam=0: tray.user32.PostMessageW(icon_.hwnd, message, wparam, lparam)  # noqa: E731
    post(tray.WM_TRAY, 0, tray.WM_LBUTTONUP)  # Linksklick auf das Symbol
    assert wait_for(lambda: events == ["open"])
    post(tray.WM_MENU_COMMAND, 1)  # zweiter auswaehlbarer Eintrag = "Sichern"
    post(tray.WM_MENU_COMMAND, 2)  # deaktivierter Eintrag: kein Ereignis
    post(tray.WM_MENU_COMMAND, 3)
    post(tray.WM_OPEN)  # Anfrage einer zweiten Programminstanz
    assert wait_for(lambda: events == ["open", "backup", "quit", "open"]), events


def test_tray_tooltip_and_menu_can_be_changed_while_running(live_tray):
    icon_, events = live_tray
    icon_.set_tooltip("Sicherung laeuft ...")
    icon_.set_items([tray.MenuItem("nur", "Einziger Eintrag")])
    tray.user32.PostMessageW(icon_.hwnd, tray.WM_MENU_COMMAND, 0, 0)
    assert wait_for(lambda: events == ["nur"]) and icon_.tooltip == "Sicherung laeuft ..."


def test_tray_icon_changes_color_state(live_tray):
    icon_, _ = live_tray
    for state in (icon.BUSY, icon.ERROR, icon.IDLE, icon.BUSY):
        icon_.set_state(state)
        assert icon_.state == state and icon_._hicon == icon_._hicons[state] and icon_._hicon
    assert len(icon_._hicons) == 3 and len(set(icon_._hicons.values())) == 3  # je Zustand ein eigenes Symbol, wiederverwendet


def test_second_instance_can_find_the_running_one(live_tray):
    icon_, events = live_tray
    assert tray.request_open_existing() is True
    assert wait_for(lambda: events == ["open"])


def test_stop_removes_the_window(live_tray):
    icon_, _ = live_tray
    hwnd = icon_.hwnd
    icon_.stop()
    assert not tray.user32.IsWindow(hwnd)
    assert tray.request_open_existing() is False
