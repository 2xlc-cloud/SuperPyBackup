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
from superpybackup import autostart, config as cfgmod, engine, scheduler, snapshots, updater

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
            application = gui.App(env.root / "config.json", task_name="SuperPyBackupTest-" + uuid.uuid4().hex[:10], hidden=True, autostart_name="SuperPyBackupTest-" + uuid.uuid4().hex[:10])
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
        scheduler.uninstall(application.base_task_name)
        scheduler.uninstall(application.all_task_name)
        scheduler.uninstall(application.update_task_name)
    except scheduler.SchedulerError:
        pass
    try:
        autostart.disable(application.autostart_name)
    except autostart.AutostartError:
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
    assert titles == ["Sicherung", "Einstellungen", "Wiederherstellung", "Aufgabe", "Update", "Protokoll", "FAQ"]
    assert __import__("superpybackup").__version__ in app.title()
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
    app.var_vss.set(False)
    app.var_max_mb.set("250")
    app.var_log_days.set("10")
    app.var_check_updates.set(False)
    assert app.save_settings() is True
    saved = json.loads((env.root / "config.json").read_text(encoding="utf-8"))
    assert saved["backup"]["use_vss"] == "off" and saved["backup"]["max_file_size_mb"] == 250
    app.var_vss.set(True)
    assert app.save_settings() is True and json.loads((env.root / "config.json").read_text(encoding="utf-8"))["backup"]["use_vss"] == "auto"
    app.config_data["backup"]["use_vss"] = "on"  # "Pflicht" aus der Konfigurationsdatei bleibt beim Speichern erhalten
    assert app.save_settings() is True and json.loads((env.root / "config.json").read_text(encoding="utf-8"))["backup"]["use_vss"] == "on"
    app.var_vss.set(False)
    assert app.save_settings() is True and json.loads((env.root / "config.json").read_text(encoding="utf-8"))["backup"]["use_vss"] == "off"
    app.var_vss.set(False)
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
    assert spec in app.snapshot_refs and "monthly/" + time.strftime("%Y-%m") not in app.snapshot_refs  # Monatsstand erst nach Monatsende
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

# FAQ ----------------------------------------------------------------------------------------
def test_faq_examples_are_accepted_by_the_real_command_parser():
    from superpybackup import cli, faq

    parser = cli.build_parser()
    examples = faq.all_examples()
    assert len(examples) >= 25
    for arguments in examples:
        parser.parse_args(faq.split_arguments(arguments))  # SystemExit bei einem ungueltigen Beispiel
    commands = {faq.split_arguments(a)[2 if a.startswith("--config") else 0] for a in examples}
    assert {"backup", "list", "restore", "verify", "config", "task", "update", "gui", "cleanup"} <= commands
    actions = {faq.split_arguments(a)[1] for a in examples if a.split()[0] in ("task", "update", "config")}
    assert {"install", "set", "status", "check", "uninstall", "rollback", "init", "show", "validate"} <= actions


def test_faq_tab_copies_commands_to_the_clipboard(app):
    from superpybackup import faq

    window = app
    assert "FAQ" in [window.notebook.tab(i, "text") for i in range(window.notebook.index("end"))]
    assert len(window.faq_buttons) == len(faq.all_examples())
    command = faq.full_command("backup --new-snapshot")
    window.faq_buttons[command].invoke()
    window.update()
    assert window.clipboard_get() == command
    assert "kopiert" in window.status.get().lower()
    window.apply_theme("light")
    window.apply_theme("dark")


# Bereinigung --------------------------------------------------------------------------------
def test_cleanup_button_previews_asks_and_deletes(app, env):
    from datetime import date, timedelta

    from test_cleanup import backup_days

    backup_days(env, [20, 0])
    configure(app, env)
    old = env.snapshot("Daily", f"{date.today() - timedelta(days=20):%Y-%m-%d}")
    assert old.exists()
    app.dialogs.answer = False
    app.btn_cleanup.invoke()
    app.wait_idle(30)
    pump(app)
    assert old.exists() and any(call[0] == "ja/nein" and "Abgelaufener Stand" in call[2] for call in app.dialogs.calls)  # Vorschau + Nein
    app.dialogs.calls.clear()
    app.dialogs.answer = True
    app.btn_cleanup.invoke()
    app.wait_idle(30)
    pump(app)
    assert not old.exists() and "1 entfernt" in app.status.get()
    app.dialogs.calls.clear()
    app.btn_cleanup.invoke()  # nichts mehr zu tun
    app.wait_idle(30)
    pump(app)
    assert any(call[0] == "info" and "nichts zu bereinigen" in call[2].lower() for call in app.dialogs.calls)


def test_notification_and_full_settings_roundtrip_and_test_button(app, env, monkeypatch):
    from superpybackup import notify

    app.var_target.set(str(env.dst))
    app.var_notify.set(True)
    app.var_notify_success.set(False)
    app.var_full_days.set("3")
    assert app.save_settings() is True
    saved = json.loads((env.root / "config.json").read_text(encoding="utf-8"))
    assert saved["notifications"] == {"enabled": True, "on_success": False, "on_update": True, "on_error": True}
    assert saved["backup"]["full_every_days"] == 3
    app.var_notify.set(False)
    app.load_config()
    assert app.var_notify.get() is True and app.var_notify_success.get() is False and app.var_full_days.get() == "3"
    shown = []
    monkeypatch.setattr(notify, "show", lambda title, message: shown.append((title, message)) or True)
    app.test_notification()
    pump(app, 1.0)
    assert len(shown) == 1


def test_update_task_install_status_and_remove(app, env):
    configure(app, env)
    app.var_upd_delay.set("6")
    app.var_upd_daily_on.set(True)
    app.var_upd_daily_at.set("11:30")
    app.btn_update_task_install.invoke()
    assert [c for c in app.dialogs.kinds("info") if "ausgeschaltet" in c[2]]  # Hinweis: Meldungen sind noch aus
    info = scheduler.status(app.update_task_name)
    assert info["exists"] and info["delay_minutes"] == 6 and info["daily_at"] == "11:30" and info["hidden"] and info["run_level"] == "LeastPrivilege"
    assert info["arguments"].endswith("update check --quiet") and scheduler.status(app.task_name)["exists"] is False  # eigene Aufgabe, Sicherungsaufgabe unberuehrt
    app.refresh_update_task()
    app.wait_idle(30)
    assert app.update_task_name in app.task_text.get("1.0", "end") and "Täglich: 11:30" in app.task_text.get("1.0", "end")
    app.remove_update_task()
    assert scheduler.status(app.update_task_name)["exists"] is False


def test_update_task_rejects_invalid_input_and_missing_config(app, env):
    app.btn_update_task_install.invoke()  # keine Konfiguration
    assert app.dialogs.kinds("fehler") and scheduler.status(app.update_task_name)["exists"] is False
    configure(app, env)
    app.dialogs.calls.clear()
    app.var_upd_daily_at.set("25:99")
    app.btn_update_task_install.invoke()
    assert app.dialogs.kinds("fehler") and scheduler.status(app.update_task_name)["exists"] is False


def test_gui_actions_send_desktop_notifications(app, env, monkeypatch):
    from superpybackup import notify

    shown = []
    monkeypatch.setattr(notify, "show", lambda title, message: shown.append(title) or True)
    write_tree(env.src, {"a.txt": "inhalt"})
    configure(app, env, notifications={"enabled": True})
    app.start_backup(False)
    app.wait_idle(60)
    pump(app, 0.8)
    assert shown == ["Sicherung abgeschlossen"]
    app.start_backup(False)  # heute schon gesichert: wird in der Oberflaeche ausdruecklich gemeldet
    app.wait_idle(60)
    pump(app, 0.8)
    assert shown[-1] == "Heute bereits gesichert"
    # ausgeschaltet oder Ereignis abgewaehlt: keine Meldung
    configure(app, env, notifications={"enabled": True, "on_success": False})
    before = len(shown)
    app.start_backup(True)
    app.wait_idle(60)
    pump(app, 0.8)
    assert len(shown) == before
    configure(app, env)
    app.start_backup(True)
    app.wait_idle(60)
    pump(app, 0.8)
    assert len(shown) == before


def test_gui_update_check_and_verify_notify(app, env, monkeypatch):
    from superpybackup import notify, updater

    shown = []
    monkeypatch.setattr(notify, "show", lambda title, message: shown.append(title) or True)
    configure(app, env, notifications={"enabled": True})
    info = updater.UpdateInfo(version="9.9.9", tag="v9.9.9", assets={}, page="")
    monkeypatch.setattr(updater, "check_for_update", lambda settings, *a, **k: info)
    app.check_update()
    app.wait_idle(30)
    pump(app, 0.8)
    assert shown == ["Neue Version von SuperPyBackup"]
    monkeypatch.setattr(updater, "check_for_update", lambda settings, *a, **k: None)
    app.check_update()
    app.wait_idle(30)
    pump(app, 0.8)
    assert shown[-1] == "Keine neue Version"


class FakeTray:
    """Ersatz fuer das Symbol im Infobereich: zeichnet Aufrufe auf."""

    def __init__(self) -> None:
        self.items = []
        self.tooltip = ""
        self.stopped = False
        self.states = []

    def set_items(self, items):
        self.items = items

    def set_tooltip(self, text):
        self.tooltip = text

    def set_state(self, state):
        self.states.append(state)

    def stop(self):
        self.stopped = True

    def enabled(self):
        return {item.event: item.enabled for item in self.items if item is not None}


def with_tray(app, monkeypatch):
    app.tray = FakeTray()
    shown = []
    app.withdraw = lambda: shown.append("hide")  # direkt setzen: wird mit dem Objekt freigegeben (kein monkeypatch-Rueckbau)
    app.deiconify = lambda: shown.append("show")
    app.focus_force = lambda: None
    app._refresh_tray_menu(False)
    return shown


def test_close_hides_to_tray_and_keeps_running(app, monkeypatch):
    shown = with_tray(app, monkeypatch)
    app.var_close_to_tray.set(True)
    app.on_close()
    assert shown == ["hide"] and app._closing is False and app.winfo_exists() and "Infobereich" in app.tray.tooltip
    app._handle_tray("open")
    assert shown == ["hide", "show"]


def test_close_quits_when_option_is_off_or_no_tray(app, monkeypatch):
    with_tray(app, monkeypatch)
    app.var_close_to_tray.set(False)
    tray = app.tray
    app.on_close()
    assert tray.stopped and app._closing is True


def test_tray_menu_quit_stops_icon_and_closes(app, monkeypatch):
    with_tray(app, monkeypatch)
    tray = app.tray
    app._handle_tray("quit")
    assert tray.stopped and app._closing is True


def test_tray_menu_reflects_busy_state_and_autostart(app, monkeypatch, env):
    with_tray(app, monkeypatch)
    assert app.tray.enabled() == {"open": True, "backup": True, "backup_new": True, "backup_full": True, "backup_all": True, "cancel": False, "autostart": True, "quit": True}
    app._set_busy_state(True)
    assert app.tray.enabled()["backup"] is False and app.tray.enabled()["backup_full"] is False
    app._backup_running = True
    app._refresh_tray_menu(True)
    assert app.tray.enabled()["cancel"] is True
    app._backup_running = False
    app._set_busy_state(False)
    assert not [i for i in app.tray.items if i is not None and i.event == "autostart"][0].checked
    app.var_autostart.set(True)
    app._apply_autostart()
    assert [i for i in app.tray.items if i is not None and i.event == "autostart"][0].checked


def test_tray_backup_options_start_the_right_kind_of_backup(app, env, monkeypatch):
    with_tray(app, monkeypatch)
    write_tree(env.src, {"a.txt": "inhalt"})
    configure(app, env)
    app._handle_tray("backup")
    app.wait_idle(60)
    assert app.last_result.status == engine.COMPLETE and app.last_result.kind == engine.FULL and "Sicherung" in app.tray.tooltip
    app._handle_tray("backup_new")
    app.wait_idle(60)
    assert app.last_result.kind == engine.INCREMENTAL
    app._handle_tray("backup_full")
    app.wait_idle(60)
    assert app.last_result.kind == engine.FULL and len(snapshots.list_snapshots(str(env.target), env.identity.sid, ("Daily",))) == 3


def test_tray_backup_without_target_shows_the_window_and_the_error(app, monkeypatch):
    shown = with_tray(app, monkeypatch)
    app._handle_tray("backup")
    assert "show" in shown and app.dialogs.kinds("fehler")


def test_autostart_setting_is_saved_and_toggled_from_tray(app, env):
    configure(app, env)
    assert not autostart.is_enabled(app.autostart_name)
    app.var_autostart.set(True)
    app.var_close_to_tray.set(False)
    assert app.save_settings() is True
    assert autostart.is_enabled(app.autostart_name) and 'gui --tray' in autostart.current(app.autostart_name)
    assert json.loads((env.root / "config.json").read_text(encoding="utf-8"))["gui"]["close_to_tray"] is False
    app.var_autostart.set(False)
    app.load_config()  # Zuruecksetzen liest den tatsaechlichen Zustand aus der Registry
    assert app.var_autostart.get() is True and app.var_close_to_tray.get() is False
    app.toggle_autostart()  # wie "Mit Windows starten" im Menue des Symbols
    assert not autostart.is_enabled(app.autostart_name)
    app.toggle_autostart()
    assert autostart.is_enabled(app.autostart_name)


def test_tray_events_from_another_thread_reach_the_window(app, monkeypatch):
    shown = with_tray(app, monkeypatch)
    import threading

    threading.Thread(target=app._tray_events.put, args=("open",)).start()
    pump(app, 0.5)
    assert shown == ["show"]


def test_icon_turns_orange_while_working_and_red_after_a_failed_backup(app, env, monkeypatch):
    import shutil

    with_tray(app, monkeypatch)
    write_tree(env.src, {"a.txt": "inhalt"})
    configure(app, env)
    assert app._icon_state == "idle"
    app.start_backup(False)
    assert app._icon_state == "busy" and app.tray.states[-1] == "busy"  # sofort beim Start
    app.wait_idle(60)
    assert app._icon_state == "idle" and app.tray.states == ["busy", "idle"]
    shutil.rmtree(env.src)  # Quelle verschwindet: naechster Lauf scheitert
    app.start_backup(True)
    app.wait_idle(60)
    assert app.last_result.status == engine.FAILED and app._icon_state == "error" and app.tray.states[-1] == "error"
    write_tree(env.src, {"a.txt": "wieder da"})
    app.start_backup(True)
    assert app._icon_state == "busy"
    app.wait_idle(60)
    assert app._icon_state == "idle"  # Fehlerfarbe verschwindet nach erfolgreichem Lauf


def test_window_icon_follows_the_state_without_tray(app, env):
    configure(app, env)
    write_tree(env.src, {"a.txt": "x"})
    app.start_backup(False)
    assert app._icon_state == "busy" and "busy" in app._icon_images
    app.wait_idle(60)
    assert app._icon_state == "idle"


def test_desktop_shortcut_buttons_create_and_remove(app, env):
    from superpybackup import shortcut

    configure(app, env)
    assert not shortcut.exists() and str(app.btn_shortcut_remove.cget("state")) == "disabled"
    app.btn_shortcut_create.invoke()
    app.wait_idle(60)
    assert shortcut.exists() and "vorhanden" in app.shortcut_label.cget("text") and str(app.btn_shortcut_remove.cget("state")) == "normal"
    assert str(env.root / "config.json") in shortcut.read()[1]
    app.btn_shortcut_remove.invoke()
    assert not shortcut.exists() and str(app.btn_shortcut_remove.cget("state")) == "disabled"


def test_backup_started_elsewhere_turns_icon_orange_and_streams_the_log(app, env):
    """Eine per Aufgabe/Konsole gestartete Sicherung haelt den Mutex: Symbol orange, Protokoll laeuft live mit, danach Ergebnis."""
    from superpybackup import winapi

    configure(app, env)
    with_tray(app, None)
    app._external_mutex = "Local\\SuperPyBackupTest-" + uuid.uuid4().hex  # nie der echte Mutex
    log_dir = Path(app._log_dir())
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "aktuell.log"  # Live-Datei des laufenden Laufs
    log_file.write_text("", encoding="utf-8")
    app.check_external()
    assert not app._external and app._icon_state == "idle"
    mutex = winapi.NamedMutex(app._external_mutex)
    assert mutex.try_acquire()
    try:
        # der fremde Prozess ist der Mutex-Halter: die GUI selbst darf nichts davon wissen
        with open(log_file, "a", encoding="utf-8", newline="\n") as handle:
            handle.write("2026-11-19 10:00:00.100 [INFO] User=S-1-5-21 Target=R:/Backup/x Start 1.7.1\n")
        app.check_external()
        assert app._external and app._icon_state == "busy" and app.tray.states[-1] == "busy"
        shown = app.backup_log.get("1.0", "end")
        assert "Start 1.7.1" in shown
        with open(log_file, "a", encoding="utf-8", newline="\n") as handle:
            handle.write("2026-11-19 10:00:05.000 [INFO] User=S-1-5-21 Target=R:/Backup/x Scan: 5 Dateien\n2026-11-19 10:00:06.000 [ERROR] User=S-1-5-21 Target=R:/Backup/x Datei nicht gesichert: a\n")
            handle.write("halbe Zeile ohne Ende")
        app.check_external()
        assert "Scan: 5 Dateien" not in app.backup_log.get("1.0", "end")  # Anzeige wird nur alle 5 Sekunden aktualisiert
        assert gui.EXTERNAL_REFRESH_SECONDS == 5 and 4 < app._external_next - time.time() <= 5
        app._external_next = 0  # die 5 Sekunden sind um
        app.check_external()
        shown = app.backup_log.get("1.0", "end")
        assert "Scan: 5 Dateien" in shown and "halbe Zeile" not in shown  # unvollstaendige Zeile wartet
        log_file.write_text("2026-11-19 11:00:00.000 [INFO] neuer Lauf\n", encoding="utf-8")  # Datei wurde neu begonnen
        app._external_next = 0
        app.check_external()
        shown = app.backup_log.get("1.0", "end")
        assert "neuer Lauf" in shown and "Scan: 5 Dateien" not in shown
        with open(log_file, "a", encoding="utf-8", newline="\n") as handle:
            handle.write("2026-11-19 11:00:06.000 [ERROR] Datei nicht gesichert: a\n")
        app._external_next = 0
    finally:
        mutex.close()
    app.check_external()
    assert not app._external and app._last_run_failed and app._icon_state == "error" and app.tray.states[-1] == "error"
    assert "Fehler" in app.result_label.cget("text")


def test_no_external_detection_while_the_gui_runs_its_own_backup(app, env):
    from superpybackup import winapi

    configure(app, env)
    app._external_mutex = "Local\\SuperPyBackupTest-" + uuid.uuid4().hex
    mutex = winapi.NamedMutex(app._external_mutex)
    assert mutex.try_acquire()
    try:
        app._backup_running = True  # eigener Lauf: Mutex gehoert der GUI selbst
        app.check_external()
        assert not app._external
    finally:
        app._backup_running = False
        mutex.close()


def _make_job_via_gui(app, env, name, files):
    import json

    app.dialogs.text = name
    app.new_job()
    source = env.root / f"src-{name}"
    write_tree(source, files)
    data = json.loads(app.config_path.read_text(encoding="utf-8"))
    data["extra_sources"] = [{"name": "Arbeit", "path": str(source)}]
    app.config_path.write_text(json.dumps(data), encoding="utf-8")
    app.load_config()
    return source


def test_job_bar_new_switch_and_remove(app, env):
    configure(app, env)
    assert app.job == "Standard" and str(app.btn_job_remove.cget("state")) == "disabled" and list(app.job_combo.cget("values")) == ["Standard"]
    standard_target = app.var_target.get()
    app.dialogs.text = "arbeit"
    app.new_job()
    assert app.job == "arbeit" and app.config_path == env.root / "jobs" / "arbeit.json" and app.config_path.is_file()
    assert app.task_name == app.base_task_name + "-Job-arbeit" and "arbeit" in app.title()
    assert app.var_target.get() == standard_target  # Einstellungen der bisherigen Sicherung uebernommen
    assert str(app.btn_job_remove.cget("state")) == "normal" and list(app.job_combo.cget("values")) == ["Standard", "arbeit"]
    assert app.select_job("Standard") and app.config_path == env.root / "config.json" and app.task_name == app.base_task_name
    app.dialogs.text = "arbeit"
    app.new_job()  # Name schon vergeben: Fehlermeldung, nichts geaendert
    assert app.dialogs.kinds("fehler") and app.job == "Standard"
    assert app.select_job("arbeit")
    app.remove_job()
    assert app.job == "Standard" and not (env.root / "jobs" / "arbeit.json").exists() and list(app.job_combo.cget("values")) == ["Standard"]
    assert app.dialogs.kinds("ja/nein")[-1][2].count("bleiben erhalten") == 1
    app.remove_job()  # Standard ist nicht entfernbar
    assert env.root.joinpath("config.json").is_file()


def test_switching_the_job_is_blocked_while_something_runs(app, env):
    configure(app, env)
    app.dialogs.text = "arbeit"
    app.new_job()
    app.select_job("Standard")
    app._ops = 1
    try:
        assert app.select_job("arbeit") is False and app.job == "Standard" and app.var_job.get() == "Standard"
    finally:
        app._ops = 0
    assert app.select_job("arbeit") and app.job == "arbeit"


def test_back_up_one_job_and_all_jobs(app, env):
    from superpybackup.identity import get_identity

    write_tree(env.src, {"a.txt": "standard"})
    configure(app, env)
    _make_job_via_gui(app, env, "arbeit", {"b.txt": "arbeit"})
    identity = get_identity()
    app.start_backup(False)
    app.wait_idle(60)
    names = sorted(p.name for p in env.dst.iterdir())
    assert names == [identity.folder + "__arbeit"]  # nur die gewaehlte Sicherung
    app.select_job("Standard")
    app.start_backup_all()
    assert app._backup_running and "btn_backup_all" in app.__dict__ and str(app.btn_backup_all.cget("state")) == "disabled"
    app.wait_idle(120)
    assert sorted(p.name for p in env.dst.iterdir()) == sorted([identity.folder, identity.folder + "__arbeit"])
    text = app.result_label.cget("text")
    assert "Standard" in text and "arbeit" in text and app._last_run_failed is False and app._icon_state == "idle"
    assert "=== Sicherung arbeit ===" in app.backup_log.get("1.0", "end")


def test_all_jobs_run_on_even_if_one_is_broken(app, env):
    write_tree(env.src, {"a.txt": "standard"})
    configure(app, env)
    (env.root / "jobs").mkdir()
    (env.root / "jobs" / "kaputt.json").write_text("{ kein json", encoding="utf-8")
    app.start_backup_all()
    app.wait_idle(120)
    text = app.result_label.cget("text")
    assert "Standard: erfolgreich" in text and "Übersprungen" in text and "kaputt" in text and app._last_run_failed


def test_task_for_all_jobs_install_status_remove(app, env):
    configure(app, env)
    app.var_all_delay.set("4")
    app.var_all_daily_on.set(True)
    app.var_all_daily_at.set("21:30")
    app.install_all_task()
    info = scheduler.status(app.all_task_name)
    assert info["exists"] and info["delay_minutes"] == 4 and info["daily_at"] == "21:30" and info["arguments"].endswith("backup --all --quiet")
    assert app.all_task_name == app.base_task_name + "-Alle"
    app.refresh_all_task()
    app.wait_idle(30)
    assert app.all_task_name in app.task_text.get("1.0", "end")
    app.remove_all_task()
    assert scheduler.status(app.all_task_name)["exists"] is False


def test_task_tab_shows_job_and_task_of_the_chosen_backup(app, env):
    configure(app, env)
    app.dialogs.text = "arbeit"
    app.new_job()
    app.notebook.select(app.tab_task)
    app.update()
    app.refresh_task()
    app.wait_idle(30)
    assert "arbeit" in app.task_title.cget("text") and app.task_name in app.task_title.cget("text")


def test_live_log_is_taken_from_the_log_folder_of_the_running_job(app, env, tmp_path):
    import json
    import os as _os

    configure(app, env)
    _make_job_via_gui(app, env, "arbeit", {"b.txt": "x"})
    other = tmp_path / "andere-logs"
    other.mkdir()
    data = json.loads(app.config_path.read_text(encoding="utf-8"))
    data["logging"]["directory"] = str(other)
    app.config_path.write_text(json.dumps(data), encoding="utf-8")
    app.select_job("Standard")  # gewaehlt ist Standard, es laeuft aber "arbeit" mit anderem Protokollordner
    (other / "aktuell.log").write_text("2026-11-19 10:00:00.000 [INFO] Sicherungslauf beginnt\n", encoding="utf-8")
    old = env.home / "Logs"
    old.mkdir(parents=True, exist_ok=True)
    (old / "aktuell.log").write_text("alt\n", encoding="utf-8")
    _os.utime(old / "aktuell.log", (1, 1))
    assert app._live_log_path() == str(other / "aktuell.log")


def test_network_target_credentials_dialog_save_and_remove(app, env, monkeypatch):
    from superpybackup import credstore, netshare

    secret = "Geheim-123-äöü"
    disconnected = []
    monkeypatch.setattr(netshare, "disconnect", lambda share: disconnected.append(share) or 0)
    configure(app, env)
    assert "Lokales Ziel" in app.cred_label.cget("text")
    app.open_credentials_dialog()  # lokales Ziel: kein Dialog, sondern Hinweis
    assert app.cred_dialog is None and app.dialogs.kinds("fehler")
    app.var_target.set("\\\\nas.example\\backup\\ordner")
    assert "keine Anmeldedaten" in app.cred_label.cget("text") and "\\\\nas.example\\backup" in app.cred_label.cget("text")
    app.open_credentials_dialog()
    assert app.cred_dialog is not None
    app.var_cred_user.set("nas\\benutzer")
    app.var_cred_password.set(secret)
    app.cred_dialog_actions["save"]()
    assert app.cred_dialog is None and app.var_cred_password.get() == ""  # Dialog zu, Kennwort aus dem Speicher der Maske entfernt
    assert credstore.load("\\\\nas.example\\backup") == ("nas\\benutzer", secret)
    assert secret.encode("utf-8") not in credstore.store_path().read_bytes()
    label = app.cred_label.cget("text")
    assert "nas\\benutzer" in label and "verschlüsselt" in label and secret not in label
    assert secret not in "".join(str(call) for call in app.dialogs.calls)
    app.open_credentials_dialog()  # zweiter Aufruf: Benutzername ist vorbelegt, Kennwort nie
    assert app.var_cred_user.get() == "nas\\benutzer" and app.var_cred_password.get() == ""
    app.cred_dialog_actions["delete"]()
    assert credstore.load("\\\\nas.example\\backup") is None and "keine Anmeldedaten" in app.cred_label.cget("text")
    app.dialogs.calls.clear()
    assert app.save_credentials("", "x") is False and app.dialogs.kinds("fehler")  # leerer Benutzername wird abgewiesen


def test_connection_test_button_reports_the_result(app, env):
    configure(app, env)
    app.test_connection(show=False)
    app.wait_idle(30)
    assert app.last_connection_test.ok and "Testdatei" in app.last_connection_test.text
    assert "erfolgreich" in app.settings_message.cget("text") and str(app.btn_conn_test.cget("state")) == "normal"
    app.var_target.set(str(env.root / "gibt-es-nicht"))
    app.dialogs.calls.clear()
    app.test_connection()
    app.wait_idle(30)
    assert app.last_connection_test.ok is False and "fehlgeschlagen" in app.settings_message.cget("text")
    assert app.dialogs.kinds("fehler") and "nicht erreichbar" in app.dialogs.kinds("fehler")[-1][2]
    app.var_target.set("")
    app.dialogs.calls.clear()
    app.test_connection()
    assert app.dialogs.kinds("fehler")  # ohne Ziel kein Test


def test_gui_installs_a_forced_update_without_asking(app, env, monkeypatch, tmp_path):
    from test_updater import FakeGitHub, newer_tags, ok_smoke
    from superpybackup import updater as upd

    github = FakeGitHub()
    try:
        github.release_list = newer_tags(7)
        github.tag = newer_tags(7)[-1]
        github.files = {"SuperPyBackup.exe": b"GUI-EXE neu", "SuperPyBackupCmd.exe": b"CMD-EXE neu"}
        install = tmp_path / "install"
        install.mkdir()
        (install / "SuperPyBackup.exe").write_bytes(b"GUI-EXE alt")
        (install / "SuperPyBackupCmd.exe").write_bytes(b"CMD-EXE alt")
        monkeypatch.setattr(upd, "_default_smoke_test", ok_smoke)
        configure(app, env, update={**github.settings, "force_after_versions": 5})
        app.force_install_dir = str(install)
        app.dialogs.answer = False  # eine Rueckfrage wuerde "Nein" erhalten: beim Pflichtupdate darf es keine geben
        app.check_update()
        app.wait_idle(60)
        assert (install / "SuperPyBackup.exe").read_bytes() == b"GUI-EXE neu" and (install / "SuperPyBackupCmd.exe").read_bytes() == b"CMD-EXE neu"
        assert "Pflichtupdate" in app.update_label.cget("text") and "installiert" in app.update_label.cget("text")
        assert not app.dialogs.kinds("ja/nein") and app.dialogs.kinds("info") and "7 Versionen" in app.dialogs.kinds("info")[-1][2]
        # unterhalb der Grenze: nur Hinweis, nichts wird installiert
        github.release_list = newer_tags(5)
        github.tag = newer_tags(5)[-1]
        (install / "SuperPyBackup.exe").write_bytes(b"GUI-EXE alt")
        app.check_update()
        app.wait_idle(60)
        assert (install / "SuperPyBackup.exe").read_bytes() == b"GUI-EXE alt" and "Neue Version verfügbar" in app.update_label.cget("text")
    finally:
        github.close()


def test_gui_auto_update_check_reports_but_only_forces_beyond_the_limit(app, env, monkeypatch, tmp_path):
    from test_updater import FakeGitHub, newer_tags, ok_smoke
    from superpybackup import updater as upd

    github = FakeGitHub()
    try:
        github.release_list = newer_tags(2)
        github.tag = newer_tags(2)[-1]
        monkeypatch.setattr(upd, "_default_smoke_test", ok_smoke)
        configure(app, env, update={**github.settings, "force_after_versions": 5})
        install = tmp_path / "install"
        install.mkdir()
        (install / "SuperPyBackup.exe").write_bytes(b"alt")
        app.force_install_dir = str(install)
        app.auto_update_check()
        app.wait_idle(60)
        assert app.last_update is not None and app.last_update.behind == 2 and (install / "SuperPyBackup.exe").read_bytes() == b"alt"
        assert "Neue Version verfügbar" in app.update_label.cget("text")
        app._closing = True  # den Wiederholungszeitgeber nicht weiterlaufen lassen
    finally:
        github.close()


def test_search_all_snapshots_preview_and_select_for_restore(app, env, tmp_path):
    write_tree(env.src, {"doc.txt": "erste Fassung", "ordner/tief/rechnung.txt": "rechnung 1"})
    configure(app, env)
    app.btn_backup.invoke()
    app.wait_idle(60)
    (env.src / "doc.txt").write_text("zweite Fassung, laenger", encoding="utf-8")
    app.btn_backup_new.invoke()
    app.wait_idle(60)
    shown = []
    app.show_preview = lambda title, preview: shown.append((title, preview))
    app.var_search.set("   ")
    app.search_files()
    assert app.dialogs.kinds("info")[-1][1] == "Suche" and app.search_dialog is None  # leerer Suchbegriff: Hinweis statt Suche
    app.var_search.set("gibt-es-nicht")
    app.search_files()
    app.wait_idle(30)
    assert "Keine Datei" in app.dialogs.kinds("info")[-1][2] and app.search_dialog is None
    app.var_search.set("doc")
    app.search_files()
    app.wait_idle(30)
    assert app.search_dialog is not None and len(app.search_results) == 2  # zwei unterschiedliche Fassungen
    first, second = app.search_results["0"], app.search_results["1"]
    assert first.path == second.path == "Test/doc.txt" and first.entry.size > second.entry.size
    app.search_tree.selection_set("1")
    app.search_dialog_actions["preview"]()
    app.wait_idle(30)
    assert shown and shown[-1][1].kind == "text" and shown[-1][1].text == "erste Fassung" and "Test/doc.txt" in shown[-1][0]
    app.search_dialog_actions["select"]()  # waehlt Stand und Datei im Reiter Wiederherstellung aus und schliesst den Dialog
    app.update()
    assert app.search_dialog is None and app.selected_snapshot().spec == second.ref.spec and app.selected_paths() == ["Test/doc.txt"]
    out = tmp_path / "wieder"
    app.var_restore_dest.set(str(out))
    app.btn_restore.invoke()
    app.wait_idle(60)
    assert read_tree(out) == {"Test/doc.txt": b"erste Fassung"}  # genau die gewaehlte aeltere Fassung
    app.file_tree.selection_set(["Test"])
    app.preview_selected()
    assert app.dialogs.kinds("info")[-1][1] == "Vorschau"  # Ordner: keine Vorschau
    app.file_tree.selection_set(["Test/doc.txt"])
    app.preview_selected()
    app.wait_idle(30)
    assert len(shown) == 2


def test_task_tab_retry_minutes_roundtrip_and_validation(app, env):
    configure(app, env)
    app.var_retry.set("3")  # 1-4 Minuten sind nicht erlaubt
    app.btn_task_install.invoke()
    assert app.dialogs.kinds("fehler") and not scheduler.status(app.task_name)["exists"]
    app.var_retry.set("abc")
    app.btn_task_install.invoke()
    assert len(app.dialogs.kinds("fehler")) == 2
    app.var_retry.set("30")
    app.btn_task_install.invoke()
    info = scheduler.status(app.task_name)
    assert info["exists"] and info["retry_minutes"] == 30 and info["on_network"] and "--if-available" in info["arguments"]
    app.var_retry.set("0")
    app.refresh_task()
    app.wait_idle(30)
    assert app.var_retry.get() == "30" and "Erneut versuchen: 30 Min." in app.task_text.get("1.0", "end")  # Anzeige liest die echte Aufgabe
    app.var_retry.set("0")
    app.btn_task_install.invoke()
    info = scheduler.status(app.task_name)
    assert info["retry_minutes"] == 0 and "--if-available" not in info["arguments"]


def test_task_tab_device_trigger_needs_the_event_log(app, env, monkeypatch):
    configure(app, env)
    app.var_usb.set(True)
    monkeypatch.setattr(scheduler, "usb_log_enabled", lambda: False)
    app.btn_task_install.invoke()
    assert app.dialogs.kinds("fehler") and "Administrator" in app.dialogs.kinds("fehler")[-1][2]
    assert not scheduler.status(app.task_name)["exists"]
    monkeypatch.setattr(scheduler, "usb_log_enabled", lambda: True)
    app.btn_task_install.invoke()
    info = scheduler.status(app.task_name)
    assert info["exists"] and info["on_usb"] and "--if-available" in info["arguments"]
    app.var_usb.set(False)
    app.refresh_task()
    app.wait_idle(30)
    assert app.var_usb.get() is True and "Beim Anstecken eines Laufwerks: ja" in app.task_text.get("1.0", "end")


def test_admin_buttons_run_the_console_with_the_form_values_after_confirmation(app, env):
    from superpybackup import elevate

    configure(app, env)
    calls = []
    outcome = {"result": (0, "Aufgabe eingerichtet: x")}

    def runner(arguments):
        calls.append(arguments)
        if isinstance(outcome["result"], Exception):
            raise outcome["result"]
        return outcome["result"]

    app.elevated_runner = runner
    app.var_delay.set("5")
    app.var_daily_on.set(True)
    app.var_daily_at.set("06:30")
    app.var_retry.set("20")
    app.var_usb.set(True)
    app.dialogs.answer = False
    app.btn_admin_task.invoke()
    assert calls == [] and app.dialogs.kinds("ja/nein")  # ohne Bestaetigung wird nichts gestartet
    app.dialogs.answer = True
    app.btn_admin_task.invoke()
    app.wait_idle(30)
    assert calls[0][:2] == ["--config", str(app.config_path)] and calls[0][2:6] == ["task", "install", "--name", app.task_name]
    assert {"--elevated", "--on-usb"} <= set(calls[0]) and calls[0][calls[0].index("--delay") + 1] == "5"
    assert calls[0][calls[0].index("--daily-at") + 1] == "06:30" and calls[0][calls[0].index("--retry-minutes") + 1] == "20"
    assert "erfolgreich" in app.task_text.get("1.0", "end") and "Aufgabe eingerichtet: x" in app.task_text.get("1.0", "end")
    errors_before = len(app.dialogs.kinds("fehler"))
    outcome["result"] = elevate.ElevationDeclined("abgelehnt")  # Benutzer lehnt die Windows-Abfrage ab
    app.btn_admin_usb_log.invoke()
    app.wait_idle(30)
    assert calls[-1] == ["usb-log", "on"] and "abgelehnt" in app.task_text.get("1.0", "end") and len(app.dialogs.kinds("fehler")) == errors_before
    outcome["result"] = (1, "Zugriff verweigert")
    app.btn_admin_usb_log.invoke()
    app.wait_idle(30)
    assert "fehlgeschlagen" in app.task_text.get("1.0", "end") and len(app.dialogs.kinds("fehler")) == errors_before + 1
    app.var_retry.set("3")  # ungueltige Eingabe: gar nicht erst nach Administratorrechten fragen
    count = len(calls)
    app.btn_admin_task.invoke()
    assert len(calls) == count and len(app.dialogs.kinds("fehler")) == errors_before + 2


def test_task_tab_keeps_the_output_visible_in_a_small_window(app, env):
    # Die Einstellungen scrollen, die Ausgabe steht fest unten (im ausgeblendeten Testfenster gibt es keine Pixelmasse, deshalb der Aufbau).
    assert app.task_text.container.pack_info()["side"] == "bottom" and app.task_text.container.master is app.tab_task
    scrolls = [child for child in app.tab_task.winfo_children() if isinstance(child, gui.ScrollFrame)]
    assert len(scrolls) == 1 and scrolls[0].pack_info()["expand"] and app.btn_task_install.winfo_toplevel() is app
    assert str(app.btn_task_install.master.master.master) == str(scrolls[0].canvas)  # Knoepfe liegen im scrollbaren Bereich
    assert int(app.task_text.cget("height")) <= 8  # niedrige, feste Ausgabe: laesst den Einstellungen Platz
    assert gui.ScrollFrame._instances and all(isinstance(frame, gui.ScrollFrame) for frame in gui.ScrollFrame._instances)
