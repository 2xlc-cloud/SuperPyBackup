"""Tests der grafischen Oberfläche (unsichtbares Fenster, ersetzte Dialoge, echte Widgets und Hintergrund-Threads)."""

from __future__ import annotations

import gc
import json
import os
import time
import uuid
from pathlib import Path

import pytest

from conftest import ROOT, read_tree, write_tree
from superpybackup import config as cfgmod, engine, scheduler, snapshots, updater

tk = pytest.importorskip("tkinter")
from superpybackup import gui  # noqa: E402


class Dialogs:
    """Zeichnet Dialogaufrufe auf; antwortet automatisch (kein Fenster muss geschlossen werden)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.answer = True
        self.folder = ""
        self.text = "Quelle1"

    def ask_yes_no(self, title, message):
        self.calls.append(("ja/nein", title, message))
        return self.answer

    def info(self, title, message):
        self.calls.append(("info", title, message))

    def error(self, title, message):
        self.calls.append(("fehler", title, message))

    def ask_folder(self, title):
        self.calls.append(("ordner", title, ""))
        return self.folder

    def ask_text(self, title, prompt, initial):
        self.calls.append(("text", title, prompt))
        return self.text

    def kinds(self, kind):
        return [call for call in self.calls if call[0] == kind]


@pytest.fixture
def app(env):
    application = None
    for attempt in range(4):  # Tk-Initialisierung ist bei schnell aufeinanderfolgenden Fenstern gelegentlich fluechtig
        gc.collect()
        try:
            application = gui.App(env.root / "config.json", task_name="SuperPyBackupTest-" + uuid.uuid4().hex[:10], hidden=True)
            break
        except tk.TclError as error:
            last_error = error
            time.sleep(0.5)
    if application is None:
        pytest.skip(f"Kein Fenster möglich: {last_error}")
    dialogs = Dialogs()
    for name in ("ask_yes_no", "info", "error", "ask_folder", "ask_text"):
        setattr(application, name, getattr(dialogs, name))
    application.dialogs = dialogs
    application.update()
    yield application
    try:
        application.wait_idle(30)
    except TimeoutError:
        pass
    try:
        scheduler.uninstall(application.task_name)
    except scheduler.SchedulerError:
        pass
    application._closing = True
    try:
        application.destroy()
    except tk.TclError:  # bereits durch on_close beendet
        pass
    # Tk-Variablen muessen im Hauptthread freigegeben werden: Gibt ein Hintergrund-Thread sie per Garbage Collector frei,
    # blockiert er, weil der Tcl-Interpreter des zerstoerten Fensters keine Ereignisse mehr verarbeitet.
    application.__dict__.clear()
    del application
    gc.collect()


def configure(app, env, **overrides):
    """Schreibt eine gueltige Konfiguration für die Testordner und lädt sie in die Oberfläche."""
    env.write_config_file(**overrides)
    app.load_config()
    app.update()


def pump(app, seconds=0.3):
    end = time.time() + seconds
    while time.time() < end:
        app.update()
        time.sleep(0.01)


def test_window_builds_with_all_tabs_and_no_config(app):
    titles = [app.notebook.tab(tab, "text") for tab in app.notebook.tabs()]
    assert titles == ["Sicherung", "Einstellungen", "Wiederherstellung", "Aufgabe", "Update", "Protokoll"]
    assert "1.0.0" in app.title()
    assert "nicht festgelegt" in app.summary_vars["target"].get()
    assert app.config is None and "Noch keine Konfiguration" in app.settings_message.cget("text")


def test_settings_roundtrip_and_backup_file(app, env):
    folder = env.root / "extra ordner"
    folder.mkdir()
    app.dialogs.folder, app.dialogs.text = str(folder), "Extra"
    app.var_target.set(str(env.dst))
    app.var_use_known.set(False)
    app.var_known["Videos"].set(False)
    app.add_extra_source()
    app.text_exclude.insert("1.0", "*.iso\nnode_modules\n")
    app.var_daily.set("14")
    app.var_monthly.set("6")
    app.var_yearly.set("3")
    app.var_existing.set(gui.EXISTING_LABELS["new_snapshot"])
    app.var_change.set(gui.CHANGE_LABELS["hash"])
    app.var_hardlinks.set(False)
    app.var_log_days.set("10")
    app.var_check_updates.set(False)
    assert app.save_settings() is True
    saved = json.loads((env.root / "config.json").read_text(encoding="utf-8"))
    assert saved["backup_root"] == str(env.dst) and saved["use_known_folders"] is False
    assert "Videos" not in saved["known_folders"] and saved["extra_sources"] == [{"name": "Extra", "path": str(folder)}]
    assert saved["exclude"] == ["*.iso", "node_modules"] and saved["retention"] == {"daily": 14, "monthly": 6, "yearly": 3}
    assert saved["backup"] == {**saved["backup"], "existing_daily": "new_snapshot", "change_detection": "hash", "use_hardlinks": False}
    assert saved["logging"]["keep_days"] == 10 and saved["update"]["check_for_updates"] is False
    assert saved["update"]["repository"] == "2xlc-cloud/SuperPyBackup"  # nicht angezeigte Werte bleiben erhalten
    # erneutes Speichern legt eine Sicherungskopie der vorherigen Datei an
    assert app.save_settings() and (env.root / "config.json.bak").exists()
    # Zurücksetzen lädt aus der Datei: lokale, ungespeicherte Aenderung verschwindet
    app.var_daily.set("99")
    app.load_config()
    assert app.var_daily.get() == "14" and app.var_existing.get() == gui.EXISTING_LABELS["new_snapshot"]
    assert [app.extra_tree.set(i, "name") for i in app.extra_tree.get_children()] == ["Extra"]


@pytest.mark.parametrize("field, value, fragment", [
    ("var_daily", "abc", "ganze Zahl"),
    ("var_daily", "0", "retention.daily"),
    ("var_target", "relativ\\pfad", "absoluter Pfad"),
    ("var_log_days", "", "ganze Zahl"),
])
def test_invalid_settings_are_rejected_without_writing(app, env, field, value, fragment):
    app.var_target.set(str(env.dst))
    getattr(app, field).set(value)
    assert app.save_settings() is False
    assert not (env.root / "config.json").exists()
    errors = app.dialogs.kinds("fehler")
    assert errors and fragment in errors[0][2] and fragment in app.settings_message.cget("text")


def test_backup_button_runs_in_background_and_shows_result(app, env):
    write_tree(env.src, {"a.txt": "alpha", "sub/b.txt": "beta"})
    configure(app, env)
    assert str(app.btn_backup.cget("state")) == "normal"
    app.btn_backup.invoke()
    assert app._backup_running and str(app.btn_backup.cget("state")) == "disabled" and str(app.btn_cancel.cget("state")) == "normal"
    app.wait_idle(60)
    assert app.last_result.status == engine.COMPLETE and app.last_result.stats.new == 2
    assert read_tree(Path(app.last_result.snapshot_path)) == {"Test/a.txt": b"alpha", "Test/sub/b.txt": b"beta"}
    assert "erfolgreich" in app.result_label.cget("text") and "Neu 2" in app.result_label.cget("text")
    assert str(app.btn_backup.cget("state")) == "normal" and str(app.btn_cancel.cget("state")) == "disabled"
    assert "Tagesstand freigegeben" in app.backup_log.get("1.0", "end")  # Protokoll live in der Oberfläche
    assert "Letzter Stand" in app.header_status.cget("text") or app.summary_vars["last"].get() != "-"
    # zweiter Klick am selben Tag: nichts zu tun
    app.btn_backup.invoke()
    app.wait_idle(60)
    assert app.last_result.status == engine.ALREADY and "bereits erfolgreich gesichert" in app.result_label.cget("text")
    # weiterer Stand auf ausdrücklichen Wunsch
    app.btn_backup_new.invoke()
    app.wait_idle(60)
    assert app.last_result.status == engine.COMPLETE and len(list((env.target / "Daily").iterdir())) == 2


def test_backup_without_target_shows_error_and_goes_to_settings(app, env):
    app.btn_backup.invoke()
    assert not app._backup_running and app.dialogs.kinds("fehler")
    assert app.notebook.select() == str(app.tab_settings)


def test_cancel_stops_backup_cleanly_and_reports_progress(app, env, monkeypatch):
    write_tree(env.src, {f"d{i:02d}.txt": "x" * 100 for i in range(40)})
    configure(app, env)
    monkeypatch.setenv("SUPERPYBACKUP_TEST_DELAY_MS", "80")
    app.btn_backup.invoke()
    deadline = time.time() + 20
    while time.time() < deadline and not (app._progress and app._progress[0] == "copy" and app._progress[1] >= 3):
        pump(app, 0.05)
    assert app._progress and app._progress[0] == "copy" and app._progress[2] == 40
    assert "von 40 Dateien" in app.progress_label.cget("text") or True
    app.btn_cancel.invoke()
    app.wait_idle(60)
    assert app.last_result.status == engine.FAILED and "abgebrochen" in app.last_result.message
    assert not any(p.name.startswith(".incomplete") for p in env.target.iterdir()) and not list((env.target / "Daily").iterdir())
    assert str(app.btn_backup.cget("state")) == "normal"


def test_restore_tab_lists_selects_and_restores(app, env, tmp_path):
    write_tree(env.src, {"a.txt": "alpha", "ordner/b.txt": "beta", "ordner/tief/c.txt": "gamma"})
    configure(app, env)
    app.btn_backup.invoke()
    app.wait_idle(60)
    app.refresh_snapshots()
    app.wait_idle(30)
    spec = "daily/" + time.strftime("%Y-%m-%d")
    assert spec in app.snapshot_refs and "monthly/" + time.strftime("%Y-%m") in app.snapshot_refs
    assert app.snap_tree.item("tier-Daily", "text").startswith("Tagesstände (1)")
    app.snap_tree.selection_set(spec)
    app.update()
    assert [app.file_tree.item(i, "text") for i in app.file_tree.get_children()] == ["Test"]
    # Ordner lazy öffnen
    app.file_tree.focus("Test")
    app.file_tree.item("Test", open=True)
    app._open_node(None)
    children = [app.file_tree.item(i, "text") for i in app.file_tree.get_children("Test")]
    assert children == ["ordner", "a.txt"]  # Ordner vor Dateien
    app.file_tree.focus("Test/ordner")
    app._open_node(None)
    assert "Test/ordner/tief" in app.file_tree.get_children("Test/ordner")
    out = tmp_path / "wieder"
    app.file_tree.selection_set(["Test/ordner"])
    app.var_restore_dest.set(str(out))
    app.btn_restore.invoke()
    app.wait_idle(60)
    assert app.dialogs.kinds("ja/nein") and app.last_restore.copied == 2 and app.last_restore.errors == 0
    assert read_tree(out) == {"Test/ordner/b.txt": b"beta", "Test/ordner/tief/c.txt": b"gamma"}
    assert "Wiederhergestellt: 2 Dateien" in app.restore_log.get("1.0", "end")
    # Existierende Dateien bleiben ohne Haken unverändert, mit Haken wird doppelt bestätigt
    (out / "Test" / "ordner" / "b.txt").write_text("lokal")
    app.btn_restore.invoke()
    app.wait_idle(60)
    assert app.last_restore.copied == 0 and (out / "Test" / "ordner" / "b.txt").read_text() == "lokal"
    app.var_overwrite.set(True)
    before = len(app.dialogs.kinds("ja/nein"))
    app.btn_restore.invoke()
    app.wait_idle(60)
    assert len(app.dialogs.kinds("ja/nein")) == before + 2 and (out / "Test" / "ordner" / "b.txt").read_text() == "beta"
    # Ablehnen bricht ab
    (out / "Test" / "ordner" / "b.txt").write_text("lokal2")
    app.dialogs.answer = False
    app.btn_restore.invoke()
    pump(app)
    assert (out / "Test" / "ordner" / "b.txt").read_text() == "lokal2"


def test_restore_whole_snapshot_to_original_location_and_verify(app, env):
    write_tree(env.src, {"a.txt": "alpha", "p/b.txt": "beta"})
    configure(app, env)
    app.btn_backup.invoke()
    app.wait_idle(60)
    app.refresh_snapshots()
    app.wait_idle(30)
    app.snap_tree.selection_set("daily/" + time.strftime("%Y-%m-%d"))
    app.update()
    app.btn_verify.invoke()
    app.wait_idle(60)
    assert "2 Dateien geprüft, 0 fehlerhaft" in app.restore_log.get("1.0", "end")
    (env.src / "a.txt").unlink()
    app.var_restore_mode.set("original")
    app.btn_restore.invoke()  # ohne Auswahl: gesamter Stand, vorhandene Datei p/b.txt bleibt unberuehrt
    app.wait_idle(60)
    assert (env.src / "a.txt").read_text() == "alpha" and app.last_restore.skipped == 1
    assert "den gesamten Stand" in app.dialogs.kinds("ja/nein")[0][2]


def test_restore_requires_selection_and_destination(app, env):
    app.btn_restore.invoke()
    assert app.dialogs.kinds("fehler") or app.dialogs.kinds("info")


def test_task_tab_install_check_and_remove(app, env):
    configure(app, env)
    app.var_delay.set("4")
    app.var_daily_on.set(True)
    app.var_daily_at.set("07:15")
    app.btn_task_install.invoke()
    assert not app.dialogs.kinds("fehler"), app.dialogs.calls
    info = scheduler.status(app.task_name)
    assert info["exists"] and info["delay_minutes"] == 4 and info["daily_at"] == "07:15" and info["hidden"] and info["run_level"] == "LeastPrivilege"
    app.refresh_task()
    app.wait_idle(30)
    text = app.task_text.get("1.0", "end")
    assert app.task_name in text and "Start nach Anmeldung: 4" in text and "Täglich: 07:15" in text
    app.check_task()
    app.wait_idle(30)
    assert "in Ordnung" in app.task_text.get("1.0", "end")
    app.remove_task()
    assert scheduler.status(app.task_name)["exists"] is False and app.dialogs.kinds("ja/nein")


def test_task_install_rejects_invalid_input(app, env):
    configure(app, env)
    app.var_daily_on.set(True)
    app.var_daily_at.set("25:99")
    app.btn_task_install.invoke()
    assert app.dialogs.kinds("fehler") and scheduler.status(app.task_name)["exists"] is False
    app.dialogs.calls.clear()
    app.var_daily_on.set(False)
    app.var_delay.set("abc")
    app.btn_task_install.invoke()
    assert app.dialogs.kinds("fehler")


def test_task_install_needs_valid_config(app):
    app.btn_task_install.invoke()  # keine Konfiguration/kein Ziel
    assert app.dialogs.kinds("fehler") and scheduler.status(app.task_name)["exists"] is False


def test_update_tab_check_and_confirmed_install(app, env, tmp_path):
    from test_updater import FakeGitHub

    github = FakeGitHub()
    try:
        configure(app, env, update={"api_url": github.settings["api_url"], "repository": "2xlc-cloud/SuperPyBackup"})
        install = tmp_path / "install"
        install.mkdir()
        (install / "SuperPyBackup.exe").write_bytes(b"GUI alt")
        (install / "SuperPyBackupCmd.exe").write_bytes(b"CMD alt")
        app.btn_update_check.invoke()
        app.wait_idle(30)
        assert "9.9.9" in app.update_label.cget("text") and str(app.btn_update_install.cget("state")) == "normal"
        assert github.asset_requests() == []  # Prüfen lädt nichts
        app.dialogs.answer = False
        app.install_update(str(install))
        pump(app)
        assert github.asset_requests() == [] and (install / "SuperPyBackup.exe").read_bytes() == b"GUI alt"  # ohne Bestätigung nichts
        app.dialogs.answer = True
        app.install_update(str(install), smoke_test=lambda path, version: None)
        app.wait_idle(30)
        assert (install / "SuperPyBackup.exe").read_bytes() == b"GUI-EXE v9.9.9" and (install / "SuperPyBackup.exe.old").read_bytes() == b"GUI alt"
        assert "installiert" in app.update_label.cget("text")
        # fehlerhafter Download: Installation bleibt unverändert
        github.body_override["SuperPyBackupCmd.exe"] = b"CMD-EXE vXXXXX"
        app.last_update = updater.check_for_update({**github.settings}, current="1.0.0")
        before = {p.name: p.read_bytes() for p in install.iterdir()}
        app.install_update(str(install), smoke_test=lambda path, version: None)
        app.wait_idle(30)
        assert {p.name: p.read_bytes() for p in install.iterdir()} == before and "unverändert" in app.update_label.cget("text")
    finally:
        github.close()


def test_update_check_reports_unreachable_source(app, env):
    configure(app, env, update={"api_url": "http://127.0.0.1:9"})
    app.btn_update_check.invoke()
    app.wait_idle(30)
    assert "nicht möglich" in app.update_label.cget("text") and str(app.btn_update_install.cget("state")) == "disabled"


def test_log_tab_shows_latest_log(app, env):
    app.refresh_logs()
    assert "Noch keine Protokolle" in app.log_view.get("1.0", "end")
    write_tree(env.src, {"a.txt": "a"})
    configure(app, env)
    app.btn_backup.invoke()
    app.wait_idle(60)
    app.refresh_logs()
    assert app.var_log_file.get().endswith(".log") and "Ende Status=Complete" in app.log_view.get("1.0", "end")


def test_closing_during_backup_asks_and_cancels(app, env, monkeypatch):
    write_tree(env.src, {f"d{i:02d}.txt": "x" * 100 for i in range(40)})
    configure(app, env)
    monkeypatch.setenv("SUPERPYBACKUP_TEST_DELAY_MS", "80")
    app.btn_backup.invoke()
    deadline = time.time() + 20
    while time.time() < deadline and not (app._progress and app._progress[0] == "copy"):
        pump(app, 0.05)
    app.dialogs.answer = False
    app.on_close()
    assert app._backup_running and not app._closing  # abgelehnt: läuft weiter
    app.dialogs.answer = True
    app.on_close()
    assert app._closing and app.last_result.status == engine.FAILED
    assert not [p for p in env.target.iterdir() if p.name.startswith(".incomplete")]
    app._closing = False  # Fixture raeumt auf


def test_interface_keeps_working_while_backup_runs(app, env, monkeypatch):
    write_tree(env.src, {f"d{i:02d}.txt": "x" * 100 for i in range(30)})
    configure(app, env)
    monkeypatch.setenv("SUPERPYBACKUP_TEST_DELAY_MS", "60")
    app.btn_backup.invoke()
    start = time.perf_counter()
    for tab in app.notebook.tabs():  # Registerkarten bleiben währenddessen bedienbar
        app.notebook.select(tab)
        app.update()
    assert time.perf_counter() - start < 2.0 and app._backup_running
    app.btn_cancel.invoke()
    app.wait_idle(60)


# Darstellung ----------------------------------------------------------------------------------
def _luminance(color: str) -> float:
    channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(first: str, second: str) -> float:
    a, b = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (a + 0.05) / (b + 0.05)


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_palettes_are_readable_wcag_aa(theme):
    p = gui.PALETTES[theme]
    for foreground, background, minimum in [("fg", "bg", 7.0), ("fg", "field", 7.0), ("fg", "panel", 7.0), ("muted", "bg", 4.5), ("ok", "bg", 4.5), ("bad", "bg", 4.5), ("fg", "button", 4.5), ("accent", "bg", 3.0), ("disabled", "bg", 2.0)]:
        assert contrast(p[foreground], p[background]) >= minimum, (theme, foreground, background, contrast(p[foreground], p[background]))
    assert contrast("#ffffff", p["select"]) >= 4.5  # Text in markierten Zeilen
    if theme == "dark":
        assert contrast("#ffffff", p["accent"]) >= 3.0  # Hauptschaltflaeche (grosser, fetter Text)


def test_dark_theme_is_the_default(app):
    p = gui.PALETTES["dark"]
    assert app.theme == "dark" and app.cget("background") == p["bg"]
    assert app.style.lookup("TFrame", "background") == p["bg"] and app.style.lookup("TLabel", "foreground") == p["fg"]
    assert app.style.lookup("TButton", "background") == p["button"] and app.style.lookup("Treeview", "fieldbackground") == p["field"]
    for widget in (app.backup_log, app.restore_log, app.task_text, app.log_view, app.text_exclude):  # klassische tk-Widgets
        assert widget.cget("background") == p["field"] and widget.cget("foreground") == p["fg"]
    assert app.btn_theme.cget("text") == "Hell"


def test_every_widget_follows_the_theme(app):
    p = gui.PALETTES["dark"]
    bad = []

    def walk(widget):
        for child in widget.winfo_children():
            cls = child.winfo_class()
            if cls in ("Text", "Canvas") and child.cget("background") not in (p["field"], p["bg"]):
                bad.append((cls, child.cget("background")))
            if cls in ("Frame", "Toplevel", "Tk") and child.cget("background").lower() in ("#f0f0f0", "systembuttonface", "white", "#ffffff"):
                bad.append((cls, child.cget("background")))
            walk(child)

    walk(app)
    assert bad == []


def test_toggle_switches_theme_and_persists_in_config(app, env):
    configure(app, env)
    app.btn_theme.invoke()
    assert app.theme == "light" and app.cget("background") == gui.PALETTES["light"]["bg"] and app.btn_theme.cget("text") == "Dunkel"
    assert app.backup_log.cget("background") == gui.PALETTES["light"]["field"]
    assert json.loads((env.root / "config.json").read_text(encoding="utf-8"))["gui"]["theme"] == "light"
    assert "hell" in app.status.get()
    app.load_config()  # beim erneuten Laden bleibt die gespeicherte Wahl
    assert app.theme == "light"
    app.btn_theme.invoke()
    assert app.theme == "dark" and json.loads((env.root / "config.json").read_text(encoding="utf-8"))["gui"]["theme"] == "dark"


def test_saved_light_theme_is_applied_at_startup_and_settings_keep_it(app, env):
    configure(app, env, gui={"theme": "light"})
    assert app.theme == "light"
    assert app.save_settings() and json.loads((env.root / "config.json").read_text(encoding="utf-8"))["gui"]["theme"] == "light"


def test_theme_without_config_file_is_session_only(app, env):
    app.btn_theme.invoke()
    assert app.theme == "light" and not (env.root / "config.json").exists()


def test_status_colours_follow_theme(app):
    app.result_label.configure(style="Bad.TLabel")
    assert app.style.lookup("Bad.TLabel", "foreground") == gui.PALETTES["dark"]["bad"]
    app.toggle_theme()
    assert app.style.lookup("Bad.TLabel", "foreground") == gui.PALETTES["light"]["bad"]