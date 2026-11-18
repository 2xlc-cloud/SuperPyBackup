"""Tests der Desktop-Verknuepfung (immer in einem Temp-Ordner statt auf dem echten Desktop, siehe conftest)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from superpybackup import shortcut


@pytest.fixture
def home(env, monkeypatch):
    monkeypatch.setenv("SUPERPYBACKUP_HOME", str(env.home))
    return env.home


def test_create_read_update_and_remove(home):
    desktop = shortcut.desktop_dir()
    assert desktop.name == "Desktop" and "pytest" in str(desktop).lower()  # Umleitung aktiv, nicht der echte Desktop
    (desktop / "fremd.txt").write_text("bleibt", encoding="utf-8")
    assert shortcut.exists() is False and shortcut.read() is None and shortcut.remove() is False
    path = shortcut.create("C:\\Daten\\config.json")
    assert path == desktop / "SuperPyBackup.lnk" and path.is_file()
    target, arguments, icon_location = shortcut.read()
    assert os.path.basename(target).lower() in ("pythonw.exe", "python.exe", "superpybackup.exe")
    assert arguments.endswith('--config "C:\\Daten\\config.json" gui')
    assert icon_location.lower().endswith("superpybackup.ico,0") and Path(icon_location.rsplit(",", 1)[0]).is_file()
    assert shortcut.create("C:\\Daten\\config.json") == path  # idempotent
    assert shortcut.remove() is True and not path.exists()
    assert (desktop / "fremd.txt").read_text(encoding="utf-8") == "bleibt"  # andere Dateien unberuehrt


def test_missing_desktop_folder_is_reported(home, monkeypatch, tmp_path):
    monkeypatch.setenv("SUPERPYBACKUP_DESKTOP", str(tmp_path / "gibt-es-nicht"))
    with pytest.raises(shortcut.ShortcutError, match="Desktopordner"):
        shortcut.create(None)


def test_cli_shortcut_create_status_remove(env, home):
    env.write_config_file()
    cmd = lambda action: env.cli("shortcut", action, extra_env={"PYTHONIOENCODING": "utf-8"})  # noqa: E731
    assert "Keine Verknuepfung" in cmd("status").stdout
    created = cmd("create")
    assert created.returncode == 0 and "angelegt" in created.stdout and shortcut.exists()
    assert "Ziel:" in cmd("status").stdout
    removed = cmd("remove")
    assert removed.returncode == 0 and "entfernt" in removed.stdout and not shortcut.exists()
    assert "keine Verknuepfung" in cmd("remove").stdout
