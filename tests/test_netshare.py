r"""Netzwerkziele (UNC): verschluesselte Anmeldedaten (DPAPI), Verbindung, Verbindungstest, Befehl `credentials`.

Eine echte Freigabe mit eigenen Anmeldedaten gibt es in der Testumgebung nicht: Die Anmeldung wird durch eine Attrappe ersetzt,
die Pfadlogik, Verschluesselung, Dateiablage und die Pruefungen laufen echt (Pruefungen gegen einen lokalen Ordner).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from conftest import ROOT, write_tree
from superpybackup import credstore, engine, netshare

SHARE = r"\\nas.example\backup"
SECRET = "Geheim-Kennwort-äöü-123!"


def test_share_key_and_name_from_unc_paths():
    assert credstore.share_key(SHARE) == r"\\nas.example\backup"
    assert credstore.share_key(r"\\NAS.example\Backup\Ordner\Unter") == r"\\nas.example\backup"
    assert credstore.share_key("\\\\nas.example\\backup\\\\x\\") == r"\\nas.example\backup"
    assert netshare.share_name(r"\\NAS.example\Backup\Ordner") == r"\\NAS.example\Backup"
    for local in (r"E:\Backup", r"C:\\", "", None, r"\\nur-server", r"\\?\C:\x", "relativ\\pfad"):
        assert credstore.share_key(local) is None


def test_password_is_encrypted_roundtrip_and_never_stored_in_clear_text(env):
    token = credstore.encrypt(SECRET)
    assert SECRET not in token and credstore.decrypt(token) == SECRET
    credstore.save(SHARE, "nas\\benutzer", SECRET)
    raw = credstore.store_path().read_bytes()
    assert SECRET.encode("utf-8") not in raw and SECRET.encode("utf-16-le") not in raw and b"Geheim" not in raw
    data = json.loads(raw)
    assert list(data["entries"]) == [r"\\nas.example\backup"] and data["entries"][r"\\nas.example\backup"]["user"] == "nas\\benutzer"
    assert credstore.load(SHARE + r"\unterordner") == ("nas\\benutzer", SECRET)  # Pfad auf der Freigabe genuegt
    assert credstore.load(SHARE.upper()) == ("nas\\benutzer", SECRET)  # ohne Gross-/Kleinschreibung
    assert credstore.user_of(SHARE) == "nas\\benutzer" and credstore.list_entries() == [(r"\\nas.example\backup", "nas\\benutzer")]
    assert credstore.store_path().parent == env.home  # im Programmordner


def test_credential_store_validation_and_removal(env):
    assert credstore.load(SHARE) is None and credstore.list_entries() == []
    with pytest.raises(credstore.CredentialError, match="keine Netzwerkfreigabe"):
        credstore.save(r"E:\Backup", "u", "p")
    with pytest.raises(credstore.CredentialError, match="Benutzername"):
        credstore.save(SHARE, "  ", "p")
    with pytest.raises(credstore.CredentialError, match="Kennwort"):
        credstore.save(SHARE, "u", "")
    credstore.save(SHARE, "u", "p1")
    credstore.save(SHARE, "u", "p2")  # ersetzt
    credstore.save(r"\\andere\freigabe", "x", "y")
    assert credstore.load(SHARE) == ("u", "p2") and len(credstore.list_entries()) == 2
    assert credstore.remove(SHARE) is True and credstore.remove(SHARE) is False
    assert credstore.load(SHARE) is None and credstore.load(r"\\andere\freigabe") == ("x", "y")


def test_damaged_files_and_foreign_ciphertext_are_reported_not_crashed(env):
    path = credstore.store_path()
    path.write_text("{ kein json", encoding="utf-8")
    with pytest.raises(credstore.CredentialError, match="nicht lesbar"):
        credstore.load(SHARE)
    path.write_text(json.dumps({"format": 1, "entries": {r"\\nas.example\backup": {"user": "u", "password": "AAAA"}}}), encoding="utf-8")
    with pytest.raises(credstore.CredentialError):
        credstore.load(SHARE)  # kein gueltiger DPAPI-Wert (z. B. von einem anderen Benutzer/Rechner)
    path.write_text(json.dumps({"format": 1, "entries": {r"\\nas.example\backup": {"user": "u", "password": "###"}}}), encoding="utf-8")
    with pytest.raises(credstore.CredentialError, match="beschaedigt"):
        credstore.load(SHARE)


def test_real_windows_connection_to_an_unreachable_server_returns_a_readable_error():
    code = netshare.connect(r"\\127.0.0.1\spb-gibt-es-nicht-xyz", "spb-test-nutzer", "falsch")
    assert code != 0 and netshare.describe(code)
    assert netshare.describe(1326).startswith("Anmeldung fehlgeschlagen") and netshare.describe(99999) == "Windows-Fehler 99999"


@pytest.fixture
def fake_wnet(monkeypatch):
    calls: list[tuple[str, str, str]] = []
    state = {"code": 0, "disconnected": []}

    def connect(share, user, password):
        calls.append((share, user, password))
        return state["code"]

    monkeypatch.setattr(netshare, "connect", connect)
    monkeypatch.setattr(netshare, "disconnect", lambda share: state["disconnected"].append(share) or 0)
    return calls, state


def test_ensure_connected_uses_stored_credentials_once_and_only_for_shares(env, fake_wnet):
    calls, state = fake_wnet
    assert netshare.ensure_connected(r"E:\Backup") is None and netshare.ensure_connected(SHARE + r"\x") is None and calls == []  # lokal / keine Anmeldedaten
    credstore.save(SHARE, "nas\\benutzer", SECRET)
    assert netshare.ensure_connected(SHARE + r"\Ordner") is None
    assert netshare.ensure_connected(SHARE + r"\anderer") is None  # nur einmal je Prozess und Freigabe
    assert calls == [(r"\\nas.example\backup", "nas\\benutzer", SECRET)]
    netshare.reset(SHARE)
    assert state["disconnected"] == [r"\\nas.example\backup"]
    assert netshare.ensure_connected(SHARE) is None and len(calls) == 2  # nach reset neu verbinden


def test_ensure_connected_reports_failures_without_the_password(env, fake_wnet):
    calls, state = fake_wnet
    credstore.save(SHARE, "benutzer", SECRET)
    state["code"] = 1326
    message = netshare.ensure_connected(SHARE)
    assert message and "benutzer" in message and "Benutzername oder Kennwort" in message and SECRET not in message
    state["code"] = 1219  # bereits mit anderen Anmeldedaten verbunden, Zugriff aber nicht pruefbar: Fehler
    assert netshare.ensure_connected(SHARE) is not None


def test_backup_to_unreachable_share_fails_cleanly_and_logs_no_password(env, fake_wnet):
    calls, state = fake_wnet
    write_tree(env.src, {"a.txt": "inhalt"})
    credstore.save(SHARE, "benutzer", SECRET)
    state["code"] = 53
    result = env.run(config_overrides={"backup_root": SHARE + r"\Backup"})
    assert result.status == engine.FAILED and "benutzer" in result.message and "nicht gefunden" in result.message
    logs = "".join(p.read_text(encoding="utf-8") for p in (env.home / "Logs").glob("*.log"))
    assert SECRET not in logs and "benutzer" in logs


def test_connection_test_on_a_local_folder_checks_everything(env):
    result = netshare.test_target(str(env.dst))
    assert result.ok, result.text
    text = result.text
    assert "Lokaler Pfad" in text and "Ordner erreichbar" in text and "Testdatei" in text and "Freier Speicher" in text
    assert "Hardlinks werden unterstuetzt" in text  # NTFS
    assert not [p for p in env.dst.iterdir()]  # Testdatei wieder entfernt


def test_connection_test_failures(env, tmp_path):
    assert netshare.test_target("relativ").ok is False
    missing = netshare.test_target(str(tmp_path / "gibt-es-nicht"))
    assert missing.ok is False and "nicht erreichbar" in missing.text
    readonly_file = tmp_path / "datei.txt"
    readonly_file.write_text("x", encoding="utf-8")
    assert netshare.test_target(str(readonly_file)).ok is False  # kein Ordner


def test_connection_test_with_typed_credentials_connects_and_disconnects_again(env, fake_wnet, monkeypatch):
    calls, state = fake_wnet
    monkeypatch.setattr(os.path, "isdir", lambda path: True)  # Freigabe "erreichbar", das Schreiben prueft der naechste Schritt
    result = netshare.test_target(SHARE + r"\Backup", "tester", "eingegeben")
    assert calls == [(r"\\nas.example\backup", "tester", "eingegeben")]  # eingegebene Daten, nichts gespeichert
    assert "Anmeldung an" in result.text and "als tester erfolgreich" in result.text and "eingegeben" not in result.text
    assert state["disconnected"] == [r"\\nas.example\backup"]  # fuer den Test aufgebaute Verbindung wieder getrennt
    assert credstore.load(SHARE) is None
    state["code"] = 86
    failed = netshare.test_target(SHARE + r"\Backup", "tester", "falsch")
    assert failed.ok is False and "Das Kennwort ist falsch" in failed.text and "falsch\n" not in failed.text.replace("Das Kennwort ist falsch", "")


def run_cli(env, *args, stdin=None):
    environment = dict(os.environ, PYTHONPATH=str(ROOT / "src"), SUPERPYBACKUP_HOME=str(env.home), PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, "-m", "superpybackup", *args], capture_output=True, text=True, env=environment, input=stdin, timeout=120, encoding="utf-8")


def test_cli_credentials_set_list_remove_never_shows_the_password(env):
    env.write_config_file()
    done = run_cli(env, "credentials", "set", "--share", SHARE, "--user", "nas\\benutzer", "--password-stdin", "--no-test", stdin=SECRET + "\n")
    assert done.returncode == 0, done.stderr
    assert SECRET not in done.stdout + done.stderr and "verschluesselt" in done.stdout
    listing = run_cli(env, "credentials", "list")
    assert listing.returncode == 0 and r"\\nas.example\backup" in listing.stdout and "nas\\benutzer" in listing.stdout and SECRET not in listing.stdout
    assert SECRET.encode("utf-8") not in (env.home / "credentials.json").read_bytes()
    assert run_cli(env, "credentials", "set", "--share", r"E:\lokal", "--user", "u", "--password-stdin", "--no-test", stdin="p\n").returncode == 1
    assert run_cli(env, "credentials", "set", "--share", SHARE, "--password-stdin", stdin="p\n").returncode == 3  # --user fehlt
    removed = run_cli(env, "credentials", "remove", "--share", SHARE)
    assert removed.returncode == 0 and "entfernt" in removed.stdout
    assert "Keine Anmeldedaten" in run_cli(env, "credentials", "list").stdout


def test_cli_credentials_test_for_a_local_target_uses_the_configured_target(env):
    env.write_config_file()
    result = run_cli(env, "--config", str(env.root / "config.json"), "credentials", "test")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Ordner erreichbar" in result.stdout and "Test erfolgreich" in result.stdout
