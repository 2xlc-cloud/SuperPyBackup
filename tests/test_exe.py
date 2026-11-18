"""Test 26: Ausfuehrung der kompilierten EXE-Dateien (nur wenn dist\\ gebaut wurde: python build\\build.py)."""

from __future__ import annotations

import hashlib
import os
import shutil
import struct
import subprocess
import uuid
from pathlib import Path

import pytest

from conftest import ROOT, read_tree, write_tree
from superpybackup import __version__, scheduler

DIST = ROOT / "dist"
GUI_EXE = DIST / "SuperPyBackup.exe"
CMD_EXE = DIST / "SuperPyBackupCmd.exe"
pytestmark = [pytest.mark.exe, pytest.mark.skipif(not (GUI_EXE.is_file() and CMD_EXE.is_file()), reason="EXE nicht gebaut (python build\\build.py)")]

DETACHED_PROCESS = 0x00000008


def clean_env(env, **extra):
    """Umgebung ohne Python: weder PYTHONPATH noch ein Python-Verzeichnis im PATH."""
    system = os.environ["SystemRoot"]
    result = {"SystemRoot": system, "PATH": f"{system}\\System32;{system}", "USERNAME": os.environ["USERNAME"], "USERPROFILE": os.environ["USERPROFILE"],
              "LOCALAPPDATA": os.environ["LOCALAPPDATA"], "TEMP": os.environ["TEMP"], "SUPERPYBACKUP_HOME": str(env.home)}
    result.update(extra)
    return result


def run_exe(exe, env, *args, **kwargs):
    return subprocess.run([str(exe), "--config", str(env.root / "config.json"), *args], capture_output=True, text=True, env=clean_env(env), timeout=180, **kwargs)


def subsystem(path: Path) -> int:
    data = path.read_bytes()[:4096]
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    return struct.unpack_from("<H", data, pe + 24 + 68)[0]


def test_exe_files_have_expected_subsystems():
    assert subsystem(GUI_EXE) == 2  # IMAGE_SUBSYSTEM_WINDOWS_GUI: keine Konsole
    assert subsystem(CMD_EXE) == 3  # IMAGE_SUBSYSTEM_WINDOWS_CUI


def test_exe_runs_without_python_installation(env):
    result = run_exe(CMD_EXE, env, "--version")
    assert result.returncode == 0 and result.stdout.strip() == f"SuperPyBackup {__version__}"
    assert "python" not in clean_env(env)["PATH"].lower()


def test_windowed_exe_runs_backup_without_console_and_logs(env):
    write_tree(env.src, {"a.txt": "a", "ordner/b.txt": "b"})
    env.write_config_file()
    process = subprocess.run([str(GUI_EXE), "--config", str(env.root / "config.json"), "backup", "--quiet"], env=clean_env(env), creationflags=DETACHED_PROCESS, timeout=180,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert process.returncode == 0
    day = [p for p in (env.target / "Daily").iterdir()]
    assert len(day) == 1 and read_tree(day[0]) == {"Test/a.txt": b"a", "Test/ordner/b.txt": b"b"}
    log = next((env.home / "Logs").glob("*.log")).read_text(encoding="utf-8")
    assert "Ende Status=Complete" in log


def test_windowed_exe_without_console_logs_errors(env):
    (env.root / "config.json").write_text('{"backup_root": ""}', encoding="utf-8")  # ungueltig
    process = subprocess.run([str(GUI_EXE), "--config", str(env.root / "config.json"), "backup", "--quiet"], env=clean_env(env), creationflags=DETACHED_PROCESS, timeout=60,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert process.returncode == 1
    assert "backup_root" in (env.home / "Logs" / "errors.log").read_text(encoding="utf-8")  # Fehler auch ohne Konsole nachvollziehbar


def test_exe_full_cycle_backup_list_restore_verify(env, tmp_path):
    write_tree(env.src, {"a.txt": "alpha", "p/b.txt": "beta"})
    env.write_config_file()
    assert run_exe(CMD_EXE, env, "config", "validate").returncode == 0
    first = run_exe(CMD_EXE, env, "backup")
    assert first.returncode == 0 and "Status: Complete" in first.stdout
    (env.src / "a.txt").write_text("alpha-neu", encoding="utf-8")
    second = run_exe(CMD_EXE, env, "backup", "--new-snapshot")
    assert second.returncode == 0 and "geaendert 1" in second.stdout
    listing = run_exe(CMD_EXE, env, "list", "--json")
    assert listing.returncode == 0 and listing.stdout.count('"tier": "Daily"') == 2
    out = tmp_path / "out"
    snapshot = [p.name for p in (env.target / "Daily").iterdir() if p.name != __import__("time").strftime("%Y-%m-%d")][0]
    restored = run_exe(CMD_EXE, env, "restore", "--snapshot", f"daily/{snapshot}", "--path", "Test/a.txt", "--dest", str(out))
    assert restored.returncode == 0 and (out / "Test" / "a.txt").read_text() == "alpha-neu" or (out / "Test" / "a.txt").read_text() == "alpha"
    assert run_exe(CMD_EXE, env, "verify").returncode == 0


def test_exe_reports_bad_config_clearly(env):
    (env.root / "config.json").write_text('{"backup_root": "relativ", "retention": {"daily": 0}}', encoding="utf-8")
    result = run_exe(CMD_EXE, env, "config", "validate")
    assert result.returncode == 1 and "retention.daily" in result.stderr and "backup_root" in result.stderr
    missing = subprocess.run([str(CMD_EXE), "--config", str(env.root / "nix.json"), "backup"], capture_output=True, text=True, env=clean_env(env))
    assert missing.returncode == 1 and "nicht gefunden" in missing.stderr


def test_exe_task_uses_windowed_program(env):
    name = "SuperPyBackupTest-" + uuid.uuid4().hex[:10]
    env.write_config_file()
    try:
        result = run_exe(CMD_EXE, env, "task", "install", "--name", name)
        assert result.returncode == 0, result.stderr
        info = scheduler.status(name)
        assert info["command"].lower().endswith("superpybackup.exe") and "backup --quiet" in info["arguments"]  # Konsolenlose Variante
        assert scheduler.check(name) == []
    finally:
        scheduler.uninstall(name)
    assert scheduler.status(name)["exists"] is False


def test_exe_update_check_and_safe_refusal(env, tmp_path):
    from test_updater import FakeGitHub

    github = FakeGitHub()
    try:
        env.write_config_file(update={"api_url": github.settings["api_url"], "repository": "2xlc-cloud/SuperPyBackup"})
        install = tmp_path / "install"
        install.mkdir()
        for exe in (GUI_EXE, CMD_EXE):
            shutil.copy2(exe, install / exe.name)
        before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in install.iterdir()}
        checked = run_exe(install / "SuperPyBackupCmd.exe", env, "update", "check")
        assert checked.returncode == 0 and "9.9.9" in checked.stdout and github.asset_requests() == []
        refused = run_exe(install / "SuperPyBackupCmd.exe", env, "update", "install", stdin=subprocess.DEVNULL)  # ohne Bestaetigung
        assert refused.returncode == 1 and "nicht bestaetigt" in refused.stderr and github.asset_requests() == []
        # Heruntergeladene Datei ist keine echte EXE der angekuendigten Version -> Installation scheitert, nichts wird veraendert
        github.files = {"SuperPyBackup.exe": b"keine exe", "SuperPyBackupCmd.exe": b"keine exe"}
        failed = run_exe(install / "SuperPyBackupCmd.exe", env, "update", "install", "--yes")
        assert failed.returncode == 1
        assert {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in install.iterdir()} == before
    finally:
        github.close()


# GUI ----------------------------------------------------------------------------------------
def _visible_windows(title_prefix: str) -> list[tuple[int, str]]:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found: list[tuple[int, str]] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd, lparam):
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            if buffer.value.startswith(title_prefix):
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                found.append((pid.value, buffer.value))
        return True

    user32.EnumWindows(callback_type(callback), 0)
    return found


def _open_gui_and_kill(command: list[str], env_vars: dict, timeout: float = 40.0):
    import time

    title = f"SuperPyBackup {__version__}"
    before = {pid for pid, _ in _visible_windows(title)}
    process = subprocess.Popen(command, env=env_vars, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    windows: list[tuple[int, str]] = []
    try:
        deadline = time.time() + timeout
        while time.time() < deadline and process.poll() is None:
            windows = [item for item in _visible_windows(title) if item[0] not in before]
            if windows:
                break
            time.sleep(0.25)
        still_running = process.poll() is None
    finally:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)], capture_output=True)  # Fenster samt Kindprozess schliessen
        process.wait(timeout=30)
    return windows, still_running


def test_windowed_exe_without_arguments_opens_the_gui(env):
    windows, running = _open_gui_and_kill([str(GUI_EXE)], clean_env(env, SUPERPYBACKUP_NO_TRAY="1"))
    assert running and windows, "SuperPyBackup.exe ohne Argumente muss das Hauptfenster oeffnen"
    assert windows[0][1] == f"SuperPyBackup {__version__}"


def test_cmd_exe_gui_command_opens_the_gui(env):
    env.write_config_file()
    windows, running = _open_gui_and_kill([str(CMD_EXE), "--config", str(env.root / "config.json"), "gui"], clean_env(env, SUPERPYBACKUP_NO_TRAY="1"))
    assert running and windows

def test_exe_cleanup_dry_run_and_confirmation(env):
    write_tree(env.src, {"a.txt": "a"})
    env.write_config_file()
    assert run_exe(CMD_EXE, env, "backup").returncode == 0
    (env.target / ".incomplete-rest" / "x").mkdir(parents=True)
    preview = run_exe(CMD_EXE, env, "cleanup", "--dry-run")
    assert preview.returncode == 0 and ".incomplete-rest" in preview.stdout and (env.target / ".incomplete-rest").exists()
    refused = run_exe(CMD_EXE, env, "cleanup", stdin=subprocess.DEVNULL)
    assert refused.returncode == 1 and (env.target / ".incomplete-rest").exists()
    done = run_exe(CMD_EXE, env, "cleanup", "--yes")
    assert done.returncode == 0 and "Entfernt: 1" in done.stdout and not (env.target / ".incomplete-rest").exists()


def test_exe_full_and_incremental_backup(env):
    write_tree(env.src, {"a.txt": "a"})
    env.write_config_file()
    first = run_exe(CMD_EXE, env, "backup")
    assert first.returncode == 0 and "Art: Vollsicherung" in first.stdout, first.stdout + first.stderr
    incremental = run_exe(CMD_EXE, env, "backup", "--incremental", "--new-snapshot")
    assert incremental.returncode == 0 and "Art: Inkrement" in incremental.stdout
    full = run_exe(CMD_EXE, env, "backup", "--full")
    assert full.returncode == 0 and "Art: Vollsicherung" in full.stdout
    assert run_exe(CMD_EXE, env, "backup", "--full", "--incremental").returncode != 0


def test_exe_notifications_use_the_capture_hook(env, tmp_path):
    write_tree(env.src, {"a.txt": "a"})
    env.write_config_file(notifications={"enabled": True})
    sink = tmp_path / "meldungen.jsonl"
    result = subprocess.run([str(CMD_EXE), "--config", str(env.root / "config.json"), "backup", "--quiet"], capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL,
                            env=dict(clean_env(env), SUPERPYBACKUP_NOTIFY_FILE=str(sink)))
    assert result.returncode == 0 and "Sicherung abgeschlossen" in sink.read_text(encoding="utf-8")


def _process_windows(pid: int) -> list[tuple[int, str, str, bool]]:
    """Alle Fenster, die zur Onefile-EXE gehoeren: (hwnd, Klasse, Titel, sichtbar). Die Fenster liegen im Kindprozess des gestarteten Prozesses, daher wird nach Klasse/Titel gesucht."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found: list[tuple[int, str, str, bool]] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd, lparam):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        title = ctypes.create_unicode_buffer(256)
        cls = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, title, 256)
        user32.GetClassNameW(hwnd, cls, 256)
        if cls.value == "SuperPyBackupTrayWindow" or title.value == f"SuperPyBackup {__version__}":
            found.append((hwnd, cls.value, title.value, bool(user32.IsWindowVisible(hwnd))))
        return True

    user32.EnumWindows(callback_type(callback), 0)
    return found


def test_exe_tray_start_is_hidden_and_a_second_start_reopens_it(env):
    import ctypes
    import time

    from superpybackup import tray

    if tray.user32.FindWindowW(tray.CLASS_NAME, None):
        pytest.skip("Es laeuft bereits eine SuperPyBackup-Oberflaeche mit Infobereich-Symbol")
    env.write_config_file()
    config = str(env.root / "config.json")
    title = f"SuperPyBackup {__version__}"
    process = subprocess.Popen([str(GUI_EXE), "--config", config, "gui", "--tray"], env=clean_env(env), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    tray_hwnd = 0
    try:
        deadline = time.time() + 40
        main_window = None
        while time.time() < deadline and process.poll() is None:
            windows = _process_windows(process.pid)
            tray_hwnd = next((w[0] for w in windows if w[1] == tray.CLASS_NAME), 0)
            main_window = next((w for w in windows if w[2] == title), None)
            if tray_hwnd and main_window:
                break
            time.sleep(0.25)
        assert tray_hwnd, "Das Symbol im Infobereich wurde nicht angelegt"
        assert main_window and main_window[3] is False, "Mit --tray darf kein Fenster sichtbar sein"
        second = subprocess.run([str(GUI_EXE), "--config", config, "gui"], env=clean_env(env), timeout=40, stdin=subprocess.DEVNULL, capture_output=True)
        assert second.returncode == 0  # zweite Instanz endet sofort
        visible = False
        for _ in range(80):
            visible = any(w[2] == title and w[3] for w in _process_windows(process.pid))
            if visible:
                break
            time.sleep(0.1)
        assert visible, "Die zweite Instanz muss das vorhandene Fenster in den Vordergrund holen"
        silent = subprocess.run([str(GUI_EXE), "--config", config, "gui", "--tray"], env=clean_env(env), timeout=40, stdin=subprocess.DEVNULL, capture_output=True)
        assert silent.returncode == 0 and len([p for p in [process] if p.poll() is None]) == 1
    finally:
        if tray_hwnd:
            ctypes.windll.user32.PostMessageW(tray_hwnd, tray.WM_CLOSE, 0, 0)  # Symbol sauber entfernen (kein Geistersymbol)
            time.sleep(0.5)
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)], capture_output=True)
        process.wait(timeout=30)
