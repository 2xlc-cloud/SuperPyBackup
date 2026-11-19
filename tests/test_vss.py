"""Schattenkopie (VSS) fuer gesperrte Dateien. Der Windows-Dienst selbst braucht Administratorrechte; die Tests ersetzen ihn durch
einen Ersatz, der den "eingefrorenen Stand" in einem temporaeren Ordner bereitstellt (Pfadumsetzung, Aufraeumen, Rueckfallverhalten)."""

from __future__ import annotations

import ctypes
import shutil
from ctypes import wintypes
from pathlib import Path

import pytest

from conftest import write_tree
from superpybackup import config as cfgmod
from superpybackup import engine, restore, vss


class FakeProvider:
    def __init__(self, device: Path) -> None:
        self.device = device
        self.created: list[str] = []
        self.deleted: list[str] = []
        self.fail = False

    def create(self, volume: str) -> tuple[str, str]:
        if self.fail:
            raise vss.ShadowError("kaputt")
        self.created.append(volume)
        return f"{{ID-{len(self.created)}}}", str(self.device)

    def delete(self, shadow_id: str) -> None:
        self.deleted.append(shadow_id)


@pytest.fixture
def shadow(env, tmp_path, monkeypatch):
    """Eingefrorener Stand von env.src im Ersatz-Geraet; der Prozess gilt als Administrator."""
    device = tmp_path / "shadowdev"
    provider = FakeProvider(device)
    monkeypatch.setattr(vss, "is_admin", lambda: True)
    monkeypatch.setattr(vss, "new_provider", lambda: provider)
    provider.freeze = lambda: shutil.copytree(env.src, Path(str(device) + str(env.src)[2:]), dirs_exist_ok=True)
    return provider


class Lock:
    """Haelt eine Datei exklusiv offen (Freigabemodus 0), wie es z. B. Outlook mit einer PST-Datei tut."""

    def __init__(self, path: Path) -> None:
        kernel = ctypes.windll.kernel32
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        self.handle = kernel.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)
        assert self.handle not in (None, wintypes.HANDLE(-1).value), "Sperre konnte nicht gesetzt werden"

    def release(self) -> None:
        ctypes.windll.kernel32.CloseHandle(wintypes.HANDLE(self.handle))


def backup_with(env, mode):
    return env.run(config_overrides={"backup": {"use_vss": mode}})


def snapshot_file(env, relative):
    ref = restore.resolve_snapshot(str(env.target), "latest", env.identity.sid)
    return ref, (Path(ref.path) / relative)


def test_locked_file_is_skipped_without_vss_and_saved_with_it(env, shadow):
    write_tree(env.src, {"normal.txt": "normal", "mail.pst": "eingefroren"})
    shadow.freeze()
    lock = Lock(env.src / "mail.pst")
    try:
        off = backup_with(env, "off")
        assert off.status == engine.PARTIAL and off.stats.errors == 1  # gesperrt: nicht lesbar
        on = env.run(when="2026-03-11 12:00:00", config_overrides={"backup": {"use_vss": "auto"}})
        assert on.status == engine.COMPLETE and on.stats.errors == 0
    finally:
        lock.release()
    ref, pst = snapshot_file(env, "Test/mail.pst")
    assert pst.read_text() == "eingefroren" and (ref.path and Path(ref.path, "Test", "normal.txt").read_text() == "normal")
    assert ref.manifest.categories[0]["source"] == str(env.src)  # Manifest kennt nur den Originalpfad, nie den der Schattenkopie
    assert all("shadow" not in entry.path.lower() for entry in ref.manifest.files)


def test_shadow_copy_is_deleted_after_the_run_and_nothing_stays_pending(env, shadow):
    write_tree(env.src, {"a.txt": "a"})
    shadow.freeze()
    assert backup_with(env, "auto").status == engine.COMPLETE
    assert shadow.created == ["C:\\"] and shadow.deleted == ["{ID-1}"]
    assert not vss._pending_path().exists()


def test_shadow_copy_is_deleted_even_if_the_backup_fails(env, shadow):
    write_tree(env.src, {"a.txt": "a"})
    shadow.freeze()
    result = env.run(config_overrides={"backup": {"use_vss": "auto"}, "backup_root": str(env.root / "fehlt" / "ziel")})
    assert result.status == engine.FAILED
    assert shadow.created == []  # Ziel fehlt: es wurde gar nicht erst eine Schattenkopie angelegt


def test_leftover_shadow_from_a_killed_run_is_cleaned_up_next_time(env, shadow):
    write_tree(env.src, {"a.txt": "a"})
    shadow.freeze()
    vss._save_pending(["{ALT-1}", "{ALT-2}"])
    assert backup_with(env, "auto").status == engine.COMPLETE
    assert sorted(shadow.deleted) == ["{ALT-1}", "{ALT-2}", "{ID-1}"] and not vss._pending_path().exists()


def test_undeletable_shadow_stays_pending_for_the_next_run(env, shadow, monkeypatch):
    write_tree(env.src, {"a.txt": "a"})
    shadow.freeze()

    def refuse(shadow_id):
        raise vss.ShadowError("Zugriff verweigert")

    monkeypatch.setattr(shadow, "delete", refuse)
    assert backup_with(env, "auto").status == engine.COMPLETE  # die Sicherung bleibt erfolgreich
    assert vss._pending() == ["{ID-1}"]


def test_auto_without_admin_backs_up_normally_with_a_hint(env, monkeypatch):
    monkeypatch.setattr(vss, "is_admin", lambda: False)
    write_tree(env.src, {"a.txt": "a"})
    assert backup_with(env, "auto").status == engine.COMPLETE
    log = "".join(p.read_text(encoding="utf-8") for p in (env.home / "Logs").glob("*.log"))
    assert "Administratorrechte" in log


def test_on_without_admin_fails_clearly(env, monkeypatch):
    monkeypatch.setattr(vss, "is_admin", lambda: False)
    write_tree(env.src, {"a.txt": "a"})
    result = backup_with(env, "on")
    assert result.status == engine.FAILED and "Administratorrechte" in result.message
    assert not list((env.target).glob("*/Daily/*"))


def test_failing_shadow_copy_falls_back_in_auto_but_fails_in_on(env, shadow):
    write_tree(env.src, {"a.txt": "a"})
    shadow.fail = True
    assert backup_with(env, "auto").status == engine.COMPLETE
    result = env.run(when="2026-03-11 12:00:00", config_overrides={"backup": {"use_vss": "on"}})
    assert result.status == engine.FAILED and "Schattenkopie" in result.message


def test_off_never_touches_vss(env, monkeypatch):
    monkeypatch.setattr(vss, "new_provider", lambda: pytest.fail("VSS darf bei 'off' nicht benutzt werden"))
    write_tree(env.src, {"a.txt": "a"})
    assert backup_with(env, "off").status == engine.COMPLETE


def test_nested_sources_are_not_saved_twice(env, shadow):
    write_tree(env.src, {"a.txt": "a", "sub/b.txt": "b"})
    shadow.freeze()
    extra = [{"name": "Test", "path": str(env.src)}, {"name": "Teil", "path": str(env.src / "sub")}]
    plain = env.run(config_overrides={"extra_sources": extra, "backup": {"use_vss": "off"}})
    viashadow = env.run(when="2026-03-11 12:00:00", config_overrides={"extra_sources": extra, "backup": {"use_vss": "auto"}})
    assert plain.status == viashadow.status == engine.COMPLETE
    ref, _ = snapshot_file(env, "Test")
    paths = sorted(entry.path for entry in ref.manifest.files)
    assert paths == ["Teil/b.txt", "Test/a.txt"]


def test_paths_without_a_local_drive_are_left_alone():
    assert vss.volume_of(r"\\nas\freigabe\ordner") is None and vss.volume_of(r"c:\x\y") == "C:\\"
    shadows = vss.ShadowSet(FakeProvider(Path("X:/unbenutzt")), type("L", (), {"info": lambda *a: None, "warn": lambda *a: None})())
    assert shadows.translate(r"\\nas\freigabe\ordner") == r"\\nas\freigabe\ordner"  # Netzwerkpfade lassen sich nicht schattenkopieren
    assert shadows.translate(r"C:\ohne\kopie") == r"C:\ohne\kopie"  # Laufwerk ohne Schattenkopie bleibt unveraendert


def test_config_validation(env):
    assert env.config().backup["use_vss"] == "auto"
    with pytest.raises(cfgmod.ConfigError, match="use_vss"):
        cfgmod.from_dict({**env.config().data, "backup": {**env.config().backup, "use_vss": "vielleicht"}})


def test_elevated_task_xml_and_cli_refuses_without_admin(env, monkeypatch, capsys):
    from superpybackup import cli, scheduler

    sid = "S-1-5-21-1-2-3-1001"
    assert "<RunLevel>HighestAvailable</RunLevel>" in scheduler.build_xml("program.exe", "backup", sid, elevated=True)
    assert "<RunLevel>LeastPrivilege</RunLevel>" in scheduler.build_xml("program.exe", "backup", sid)  # Standard: keine erhoehten Rechte
    monkeypatch.setattr(vss, "is_admin", lambda: False)
    env.write_config_file()
    installed = []
    monkeypatch.setattr(scheduler, "install", lambda *args, **kwargs: installed.append(kwargs))
    assert cli.main(["--config", str(env.root / "config.json"), "task", "install", "--elevated", "--name", "SuperPyBackup-Test-Elevated"]) == 1
    assert "Administrator" in capsys.readouterr().err and installed == []  # nichts eingerichtet
    monkeypatch.setattr(vss, "is_admin", lambda: True)
    assert cli.main(["--config", str(env.root / "config.json"), "task", "install", "--elevated", "--name", "SuperPyBackup-Test-Elevated"]) == 0
    assert installed and installed[0]["elevated"] is True  # mit Administratorrechten wird die Aufgabe mit hoechsten Rechten angefordert


@pytest.mark.skipif(not __import__("os").environ.get("SUPERPYBACKUP_TEST_REAL_VSS") or not vss.is_admin(), reason="Echte Schattenkopie nur bei SUPERPYBACKUP_TEST_REAL_VSS=1 in einer Administrator-Sitzung (legt kurz eine echte Schattenkopie an)")
def test_real_shadow_copy_roundtrip(env):
    write_tree(env.src, {"a.txt": "echt"})
    log = type("L", (), {"info": lambda *a: None, "warn": lambda *a: None})()
    shadows = vss.open_for("on", [str(env.src)], log)
    try:
        assert (Path(shadows.translate(str(env.src))) / "a.txt").read_text() == "echt"
    finally:
        shadows.close()
