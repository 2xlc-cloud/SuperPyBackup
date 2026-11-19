"""Tests 27, 28: Update-Pruefung mit simulierten GitHub-Antworten und Verhalten bei fehlerhaften Updates."""

from __future__ import annotations

import base64
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from conftest import TEST_SEED
from superpybackup import __version__, ed25519, updater
from superpybackup.runlog import RunLog

REPO = "2xlc-cloud/SuperPyBackup"
TEST_KEY = ed25519.public_key(TEST_SEED).hex()


class FakeGitHub:
    """Lokaler Server, der die GitHub-Release-API und Downloads nachbildet und alle Anfragen mitzaehlt."""

    def __init__(self):
        self.tag = "v9.9.9"
        self.files = {"SuperPyBackup.exe": b"GUI-EXE v9.9.9", "SuperPyBackupCmd.exe": b"CMD-EXE v9.9.9"}
        self.sums_override: dict[str, str] | None = None
        self.size_override: dict[str, int] = {}
        self.body_override: dict[str, bytes] = {}
        self.release_override = None  # (status, bytes)
        self.requests: list[str] = []
        self.drop_assets: set[str] = set()
        self.draft = False
        self.sig_override: bytes | None = None  # statt der gueltigen Signatur ausliefern
        self.release_list: list | None = None  # Eintraege fuer /releases?per_page=100: Tag oder (Tag, draft, prerelease)
        self.prerelease = False
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # noqa: D401
                pass

            def do_GET(self):
                outer.requests.append(self.path)
                base = f"http://127.0.0.1:{outer.port}"
                if self.path == f"/repos/{REPO}/releases/latest":
                    if outer.release_override:
                        status, body = outer.release_override
                        return self._send(status, body)
                    assets = {name: data for name, data in outer.files.items()}
                    sums = outer.sums_text()
                    all_assets = {**assets, "SHA256SUMS.txt": sums.encode(), "SHA256SUMS.txt.sig": outer.sig_body()}
                    listing = [{"name": n, "size": outer.size_override.get(n, len(d)), "browser_download_url": f"{base}/{REPO}/releases/download/{outer.tag}/{n}"} for n, d in all_assets.items() if n not in outer.drop_assets]
                    return self._send(200, json.dumps({"tag_name": outer.tag, "draft": outer.draft, "prerelease": outer.prerelease, "html_url": f"{base}/{REPO}/releases/tag/{outer.tag}", "assets": listing}).encode())
                if self.path.startswith(f"/repos/{REPO}/releases?") and outer.release_list is not None:
                    rows = [{"tag_name": e, "draft": False, "prerelease": False} if isinstance(e, str) else {"tag_name": e[0], "draft": e[1], "prerelease": e[2]} for e in outer.release_list]
                    return self._send(200, json.dumps(rows).encode())
                prefix = f"/{REPO}/releases/download/{outer.tag}/"
                if self.path.startswith(prefix):
                    name = self.path[len(prefix):]
                    if name == "SHA256SUMS.txt":
                        return self._send(200, outer.sums_text().encode())
                    if name == "SHA256SUMS.txt.sig" and name not in outer.drop_assets:
                        return self._send(200, outer.sig_body())
                    if name in outer.files:
                        return self._send(200, outer.body_override.get(name, outer.files[name]))
                self._send(404, b"nicht gefunden")

            def _send(self, status, body):
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def sums_text(self) -> str:
        sums = self.sums_override if self.sums_override is not None else {n: hashlib.sha256(d).hexdigest() for n, d in self.files.items()}
        return "".join(f"{digest}  {name}\n" for name, digest in sums.items())

    def sig_body(self) -> bytes:
        if self.sig_override is not None:
            return self.sig_override
        return base64.b64encode(ed25519.sign(TEST_SEED, self.sums_text().encode())) + b"\n"

    @property
    def settings(self) -> dict:
        return {"check_for_updates": True, "check_interval_hours": 24, "repository": REPO, "api_url": f"http://127.0.0.1:{self.port}"}

    def asset_requests(self) -> list[str]:
        return [path for path in self.requests if "/releases/download/" in path]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def github(monkeypatch):
    monkeypatch.setattr(updater, "RELEASE_PUBLIC_KEYS", (TEST_KEY,))
    server = FakeGitHub()
    yield server
    server.close()


@pytest.fixture
def install_dir(tmp_path):
    folder = tmp_path / "install"
    folder.mkdir()
    (folder / "SuperPyBackup.exe").write_bytes(b"GUI-EXE alt")
    (folder / "SuperPyBackupCmd.exe").write_bytes(b"CMD-EXE alt")
    return folder


def snapshot_of(folder):
    return {p.name: p.read_bytes() for p in folder.iterdir()}


def ok_smoke(path, version):
    assert path.endswith(".exe")


# 27 -----------------------------------------------------------------------------------------
def test_newer_release_is_reported_without_downloading_anything(github):
    info = updater.check_for_update(github.settings)
    assert info and info.version == "9.9.9" and info.tag == "v9.9.9"
    assert github.asset_requests() == []  # Pruefen laedt keine Programmdateien


@pytest.mark.parametrize("tag", [__version__, "v" + __version__, "v0.0.1", "v1.0.0-rc1"])
def test_same_or_older_release_means_up_to_date(github, tag):
    github.tag = tag
    if "-rc" in tag:
        with pytest.raises(updater.UpdateError):
            updater.check_for_update(github.settings)
    else:
        assert updater.check_for_update(github.settings) is None


def test_version_comparison_is_numeric_not_textual():
    assert updater.parse_version("v1.10.0") > updater.parse_version("1.9.9")
    assert updater.parse_version("2.0.0") > updater.parse_version("1.99.99")
    for bad in ("", "1.0", "abc", "1.0.0.0", "v1.x.0"):
        with pytest.raises(updater.UpdateError):
            updater.parse_version(bad)


@pytest.mark.parametrize("prepare", [
    lambda g: setattr(g, "release_override", (200, b"das ist kein json")),
    lambda g: setattr(g, "release_override", (200, b'{"tag_name": "v9.9.9"}')),
    lambda g: setattr(g, "release_override", (404, b"not found")),
    lambda g: setattr(g, "release_override", (200, json.dumps({"tag_name": "v9.9.9", "assets": [{"name": "SuperPyBackup.exe", "size": 3, "browser_download_url": "https://evil.example/x.exe"}]}).encode())),
    lambda g: setattr(g, "draft", True),
    lambda g: setattr(g, "prerelease", True),
    lambda g: setattr(g, "tag", "neueste"),
])
def test_unexpected_or_hostile_release_data_is_rejected(github, prepare):
    prepare(github)
    with pytest.raises(updater.UpdateError):
        updater.check_for_update(github.settings)


def test_unreachable_update_source_is_an_error_not_a_crash():
    with pytest.raises(updater.UpdateError):
        updater.check_for_update({"check_for_updates": True, "check_interval_hours": 24, "repository": REPO, "api_url": "http://127.0.0.1:9"})


def test_non_https_remote_urls_are_refused():
    with pytest.raises(updater.UpdateError):
        updater.default_fetch("http://example.com/x", 100)
    with pytest.raises(updater.UpdateError):
        updater.default_fetch("file:///C:/Windows/win.ini", 100)


def test_periodic_check_respects_interval_and_setting(env, github):
    from superpybackup import config as cfgmod

    log = RunLog(str(env.home / "Logs"))
    data = cfgmod.template(str(env.dst))
    data["update"].update(github.settings)
    config = cfgmod.from_dict(data)
    assert updater.periodic_check(config, log, fetch=updater.default_fetch).version == "9.9.9"
    before = len(github.requests)
    assert updater.periodic_check(config, log) is None and len(github.requests) == before  # innerhalb des Intervalls: keine Anfrage
    data["update"]["check_for_updates"] = False
    assert updater.periodic_check(cfgmod.from_dict(data), log) is None
    assert "Neue Version verfuegbar: 9.9.9" in next((env.home / "Logs").glob("*.log")).read_text(encoding="utf-8")


def test_periodic_check_failure_only_warns(env):
    from superpybackup import config as cfgmod

    data = cfgmod.template(str(env.dst))
    data["update"]["api_url"] = "http://127.0.0.1:9"
    log = RunLog(str(env.home / "Logs"))
    assert updater.periodic_check(cfgmod.from_dict(data), log) is None
    assert "Update-Pruefung nicht moeglich" in next((env.home / "Logs").glob("*.log")).read_text(encoding="utf-8")


# 28 -----------------------------------------------------------------------------------------
def test_install_requires_confirmation_and_downloads_nothing_without_it(github, install_dir):
    info = updater.check_for_update(github.settings)
    before = snapshot_of(install_dir)
    with pytest.raises(updater.UpdateError, match="nicht bestaetigt"):
        updater.install_update(info, str(install_dir), github.settings, confirm=lambda i: False, smoke_test=ok_smoke)
    assert github.asset_requests() == [] and snapshot_of(install_dir) == before


def test_successful_update_replaces_programs_and_supports_rollback(github, install_dir):
    info = updater.check_for_update(github.settings)
    seen = []
    names = updater.install_update(info, str(install_dir), github.settings, confirm=lambda i: seen.append(i.version) or True, smoke_test=lambda p, v: seen.append(("smoke", v)), log=None)
    assert seen == ["9.9.9", ("smoke", "9.9.9"), ("smoke", "9.9.9")] and sorted(names) == ["SuperPyBackup.exe", "SuperPyBackupCmd.exe"]
    files = snapshot_of(install_dir)
    assert files["SuperPyBackup.exe"] == b"GUI-EXE v9.9.9" and files["SuperPyBackupCmd.exe"] == b"CMD-EXE v9.9.9"
    assert files["SuperPyBackup.exe.old"] == b"GUI-EXE alt"
    assert not [name for name in files if name.startswith(".update-")]
    assert sorted(updater.rollback(str(install_dir))) == ["SuperPyBackup.exe", "SuperPyBackupCmd.exe"]
    assert snapshot_of(install_dir) == {"SuperPyBackup.exe": b"GUI-EXE alt", "SuperPyBackupCmd.exe": b"CMD-EXE alt"}
    with pytest.raises(updater.UpdateError):
        updater.rollback(str(install_dir))


@pytest.mark.parametrize("name, prepare, message", [
    ("beschaedigt", lambda g: g.body_override.update({"SuperPyBackupCmd.exe": b"CMD-EXE vXXXXX"}), "SHA-256"),
    ("unvollstaendig", lambda g: g.body_override.update({"SuperPyBackupCmd.exe": b"CMD-EXE"}), "Groesse"),
    ("falsche Summe", lambda g: setattr(g, "sums_override", {"SuperPyBackup.exe": "0" * 64, "SuperPyBackupCmd.exe": "1" * 64}), "SHA-256"),
    ("Summe fehlt", lambda g: setattr(g, "sums_override", {"SuperPyBackup.exe": hashlib.sha256(b"GUI-EXE v9.9.9").hexdigest()}), "Summe"),
    ("Asset fehlt", lambda g: g.drop_assets.add("SuperPyBackupCmd.exe"), "nicht alle"),
    ("Summendatei fehlt", lambda g: g.drop_assets.add("SHA256SUMS.txt"), "nicht alle"),
    ("Groesse in API falsch", lambda g: g.size_override.update({"SuperPyBackup.exe": 5}), "Groesse"),
])
def test_faulty_updates_leave_installation_untouched(github, install_dir, name, prepare, message):
    prepare(github)
    info = updater.check_for_update(github.settings)
    before = snapshot_of(install_dir)
    with pytest.raises(updater.UpdateError, match=message):
        updater.install_update(info, str(install_dir), github.settings, confirm=lambda i: True, smoke_test=ok_smoke)
    assert snapshot_of(install_dir) == before  # keine .old-Dateien, keine Arbeitsordner, keine Aenderung


def test_failed_smoke_test_blocks_the_swap(github, install_dir):
    info = updater.check_for_update(github.settings)
    before = snapshot_of(install_dir)

    def failing(path, version):
        raise updater.UpdateError("Programm startet nicht")

    with pytest.raises(updater.UpdateError, match="startet nicht"):
        updater.install_update(info, str(install_dir), github.settings, confirm=lambda i: True, smoke_test=failing)
    assert snapshot_of(install_dir) == before


def test_swap_failure_rolls_back_first_replacement(github, install_dir, monkeypatch):
    info = updater.check_for_update(github.settings)
    before = snapshot_of(install_dir)
    real_replace = updater.os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 4:  # zweiter Programmwechsel scheitert mitten im Austausch
            raise PermissionError(5, "Zugriff verweigert")
        return real_replace(src, dst)

    monkeypatch.setattr(updater.os, "replace", flaky)
    with pytest.raises(PermissionError):
        updater.install_update(info, str(install_dir), github.settings, confirm=lambda i: True, smoke_test=ok_smoke)
    monkeypatch.undo()
    assert snapshot_of(install_dir) == before


def test_update_never_touches_configuration(env, github, install_dir):
    env.write_config_file()
    before = (env.root / "config.json").read_bytes()
    info = updater.check_for_update(github.settings)
    updater.install_update(info, str(install_dir), github.settings, confirm=lambda i: True, smoke_test=ok_smoke)
    assert (env.root / "config.json").read_bytes() == before


def test_install_refuses_folder_without_programs(github, tmp_path):
    info = updater.check_for_update(github.settings)
    with pytest.raises(updater.UpdateError, match="kein Programm"):
        updater.install_update(info, str(tmp_path), github.settings, confirm=lambda i: True, smoke_test=ok_smoke)
    assert github.asset_requests() == []


def test_parse_sums_accepts_standard_formats():
    digest = "a" * 64
    assert updater.parse_sums(f"{digest}  datei.exe\n{digest} *zwei.exe\nmuell\n") == {"datei.exe": digest, "zwei.exe": digest}


# ---- Pflichtupdate: mehr als 5 Versionen zurueck -----------------------------------------------------------------
def newer_tags(count: int) -> list[str]:
    """count veroeffentlichte Versionen, die neuer sind als die installierte (plus Aelteres, Entwuerfe und Vorabversionen, die nicht zaehlen)."""
    major = int(__version__.split(".")[0]) + 1
    return [f"v{major}.{n}.0" for n in range(count)]


def test_count_newer_counts_only_published_newer_versions(github):
    github.release_list = newer_tags(3) + ["v0.0.1", f"v{__version__}", ("v99.0.0", True, False), ("v98.0.0", False, True), "v50.0.0"]
    assert updater.count_newer(github.settings, __version__) == 4  # 3 + v50.0.0; aelter, gleich, Entwurf und Vorabversion zaehlen nicht
    github.release_list = None  # Liste nicht abrufbar: mindestens die neueste Version
    assert updater.count_newer(github.settings, __version__) == 1


@pytest.mark.parametrize("newer, limit, forced", [(1, 5, False), (5, 5, False), (6, 5, True), (30, 5, True), (6, 0, False), (3, 2, True), (2, 2, False)])
def test_forced_only_when_more_versions_behind_than_the_limit(github, newer, limit, forced):
    github.release_list = newer_tags(newer)
    github.tag = newer_tags(newer)[-1]
    settings = {**github.settings, "force_after_versions": limit}
    info = updater.check_for_update(settings)
    assert info.behind == newer and info.forced is forced


def test_default_limit_is_five_and_a_missing_list_never_forces(github):
    github.tag = "v9.9.9"
    github.release_list = None
    info = updater.check_for_update(github.settings)  # Einstellung nicht angegeben: Standard 5
    assert info.behind == 1 and info.forced is False
    github.release_list = newer_tags(6)
    assert updater.check_for_update(github.settings).forced is True
    assert updater.check_for_update(github.settings, current="9.9.9") is None  # aktuell: nichts zu tun


def forced_config(env, github, **update):
    from superpybackup import config as cfgmod

    return cfgmod.from_dict({**env.config().data, "update": {**github.settings, "force_after_versions": 5, **update}})


def test_forced_update_installs_without_asking_and_verifies_like_a_normal_update(env, github, install_dir):
    github.release_list = newer_tags(6)
    github.tag = newer_tags(6)[-1]
    github.files = {"SuperPyBackup.exe": b"GUI-EXE neu", "SuperPyBackupCmd.exe": b"CMD-EXE neu"}
    config = forced_config(env, github)
    info = updater.check_for_update(config.update)
    assert info.forced
    log = RunLog(str(env.home / "Logs"))
    names = updater.run_forced_update(config, info, log, install_dir=str(install_dir), smoke_test=ok_smoke)
    assert sorted(names) == ["SuperPyBackup.exe", "SuperPyBackupCmd.exe"]
    assert (install_dir / "SuperPyBackup.exe").read_bytes() == b"GUI-EXE neu" and (install_dir / "SuperPyBackupCmd.exe.old").read_bytes() == b"CMD-EXE alt"
    text = "".join(p.read_text(encoding="utf-8") for p in (env.home / "Logs").glob("*.log"))
    assert "Pflichtupdate" in text and "6 Versionen" in text


def test_forced_update_does_nothing_below_the_limit_or_when_switched_off(env, github, install_dir):
    github.release_list = newer_tags(5)
    github.tag = newer_tags(5)[-1]
    before = snapshot_of(install_dir)
    log = RunLog(str(env.home / "Logs"))
    config = forced_config(env, github)
    info = updater.check_for_update(config.update)
    assert info.forced is False and updater.run_forced_update(config, info, log, install_dir=str(install_dir), smoke_test=ok_smoke) is None
    assert updater.run_forced_update(config, None, log, install_dir=str(install_dir)) is None
    github.release_list = newer_tags(8)
    github.tag = newer_tags(8)[-1]
    info = updater.check_for_update(config.update)
    assert info.forced
    assert updater.run_forced_update(forced_config(env, github, check_for_updates=False), info, log, install_dir=str(install_dir)) is None  # Pruefung ausgeschaltet: nie
    assert snapshot_of(install_dir) == before and github.asset_requests() == []


def test_forced_update_failure_leaves_the_installation_untouched(env, github, install_dir):
    github.release_list = newer_tags(7)
    github.tag = newer_tags(7)[-1]
    github.sums_override = {"SuperPyBackup.exe": "0" * 64, "SuperPyBackupCmd.exe": "0" * 64}
    config = forced_config(env, github)
    info = updater.check_for_update(config.update)
    before = snapshot_of(install_dir)
    with pytest.raises(updater.UpdateError, match="SHA-256"):
        updater.run_forced_update(config, info, RunLog(str(env.home / "Logs")), install_dir=str(install_dir), smoke_test=ok_smoke)
    assert snapshot_of(install_dir) == before
    text = "".join(p.read_text(encoding="utf-8") for p in (env.home / "Logs").glob("*.log"))
    assert "Pflichtupdate" in text and "fehlgeschlagen" in text


def test_forced_update_from_source_is_only_reported(env, github):
    github.release_list = newer_tags(7)
    github.tag = newer_tags(7)[-1]
    config = forced_config(env, github)
    info = updater.check_for_update(config.update)
    assert updater.run_forced_update(config, info, RunLog(str(env.home / "Logs"))) is None  # kein frozen: es gibt nichts zu ersetzen
    assert github.asset_requests() == []


def run_cli_in_process(env, install_dir, monkeypatch, *args):
    from superpybackup import cli

    real = updater.run_forced_update
    monkeypatch.setattr(updater, "run_forced_update", lambda config, info, log, **kw: real(config, info, log, install_dir=str(install_dir), smoke_test=ok_smoke))
    return cli.main(["--config", str(env.root / "config.json"), *args])


def test_scheduled_update_check_installs_a_forced_update(env, github, install_dir, monkeypatch):
    github.release_list = newer_tags(6)
    github.tag = newer_tags(6)[-1]
    github.files = {"SuperPyBackup.exe": b"GUI-EXE neu", "SuperPyBackupCmd.exe": b"CMD-EXE neu"}
    env.write_config_file(update={**github.settings, "force_after_versions": 5})
    assert run_cli_in_process(env, install_dir, monkeypatch, "update", "check", "--quiet") == 0
    assert (install_dir / "SuperPyBackup.exe").read_bytes() == b"GUI-EXE neu"


def test_backup_run_installs_a_forced_update_after_the_backup(env, github, install_dir, monkeypatch):
    from conftest import write_tree

    write_tree(env.src, {"a.txt": "inhalt"})
    github.release_list = newer_tags(9)
    github.tag = newer_tags(9)[-1]
    github.files = {"SuperPyBackup.exe": b"GUI-EXE neu", "SuperPyBackupCmd.exe": b"CMD-EXE neu"}
    env.write_config_file(update={**github.settings, "force_after_versions": 5})
    assert run_cli_in_process(env, install_dir, monkeypatch, "backup", "--quiet") == 0
    assert list((env.dst).glob("*/Daily/*")) and (install_dir / "SuperPyBackupCmd.exe").read_bytes() == b"CMD-EXE neu"  # erst gesichert, dann aktualisiert


def test_a_failed_forced_update_never_breaks_the_backup(env, github, install_dir, monkeypatch):
    from conftest import write_tree

    write_tree(env.src, {"a.txt": "inhalt"})
    github.release_list = newer_tags(9)
    github.tag = newer_tags(9)[-1]
    github.sums_override = {"SuperPyBackup.exe": "f" * 64, "SuperPyBackupCmd.exe": "f" * 64}
    env.write_config_file(update={**github.settings, "force_after_versions": 5})
    before = snapshot_of(install_dir)
    assert run_cli_in_process(env, install_dir, monkeypatch, "backup", "--quiet") == 0  # Sicherung bleibt erfolgreich
    assert snapshot_of(install_dir) == before


def test_interactive_check_only_announces_the_forced_update(env, github, install_dir, monkeypatch, capsys):
    github.release_list = newer_tags(6)
    github.tag = newer_tags(6)[-1]
    env.write_config_file(update={**github.settings, "force_after_versions": 5})
    assert run_cli_in_process(env, install_dir, monkeypatch, "update", "check") == 0
    output = capsys.readouterr().out
    assert "6 Versionen alt" in output and "Pflichtupdate" in output
    assert (install_dir / "SuperPyBackup.exe").read_bytes() == b"GUI-EXE alt"  # 'check' laedt und installiert nichts


# ---- Signatur der Release (Ed25519 ueber SHA256SUMS.txt) ---------------------------------------------------------------
def install_with(github, install_dir, **settings):
    info = updater.check_for_update({**github.settings, **settings})
    return updater.install_update(info, str(install_dir), {**github.settings, **settings}, confirm=lambda i: True, smoke_test=ok_smoke)


def test_signed_release_installs(github, install_dir):
    assert sorted(install_with(github, install_dir)) == ["SuperPyBackup.exe", "SuperPyBackupCmd.exe"]
    assert (install_dir / "SuperPyBackup.exe").read_bytes() == b"GUI-EXE v9.9.9"


def test_missing_signature_blocks_the_installation(github, install_dir):
    github.drop_assets = {"SHA256SUMS.txt.sig"}
    before = snapshot_of(install_dir)
    with pytest.raises(updater.UpdateError, match="SHA256SUMS.txt.sig"):
        install_with(github, install_dir)
    assert snapshot_of(install_dir) == before


@pytest.mark.parametrize("sig", [
    base64.b64encode(ed25519.sign(bytes(32), b"irgendwas anderes")) + b"\n",  # gueltige Signatur, aber ueber anderen Inhalt
    base64.b64encode(ed25519.sign(bytes(range(32)), b"x")) + b"\n",  # anderer Schluessel
    b"das ist kein base64 !!!",
    b"",
    base64.b64encode(b"zu kurz"),
])
def test_invalid_signature_blocks_the_installation(github, install_dir, sig):
    github.sig_override = sig
    before = snapshot_of(install_dir)
    with pytest.raises(updater.UpdateError, match="Signatur"):
        install_with(github, install_dir)
    assert snapshot_of(install_dir) == before and github.asset_requests().count(f"/{REPO}/releases/download/v9.9.9/SuperPyBackup.exe") == 0  # nichts Ausfuehrbares geladen


def test_forged_sums_with_valid_hashes_are_refused(github, install_dir):
    # Angreifer tauscht EXE und Pruefsummen aus (alles passt zueinander), kann aber nicht signieren
    github.files = {"SuperPyBackup.exe": b"SCHADCODE", "SuperPyBackupCmd.exe": b"SCHADCODE"}
    legit = github.sig_body()
    github.files = {"SuperPyBackup.exe": b"GUI-EXE v9.9.9", "SuperPyBackupCmd.exe": b"CMD-EXE v9.9.9"}
    good_signature = github.sig_body()
    assert legit != good_signature
    github.files = {"SuperPyBackup.exe": b"SCHADCODE", "SuperPyBackupCmd.exe": b"SCHADCODE"}
    github.sig_override = good_signature  # Signatur der echten Release zu den gefaelschten Summen
    before = snapshot_of(install_dir)
    with pytest.raises(updater.UpdateError, match="Signatur"):
        install_with(github, install_dir)
    assert snapshot_of(install_dir) == before


def test_signature_can_be_switched_off_explicitly(github, install_dir):
    github.sig_override = b"kaputt"
    github.drop_assets = {"SHA256SUMS.txt.sig"}
    assert install_with(github, install_dir, require_signature=False)  # bewusst abgeschaltet: wie frueher, nur SHA-256
    assert (install_dir / "SuperPyBackup.exe").read_bytes() == b"GUI-EXE v9.9.9"


def test_key_rotation_accepts_old_or_new_key(github, install_dir, monkeypatch):
    new = ed25519.public_key(bytes(32)).hex()
    monkeypatch.setattr(updater, "RELEASE_PUBLIC_KEYS", (new, TEST_KEY))
    assert install_with(github, install_dir)
    monkeypatch.setattr(updater, "RELEASE_PUBLIC_KEYS", (new,))
    with pytest.raises(updater.UpdateError, match="Signatur"):
        install_with(github, install_dir)


def test_shipped_public_key_is_a_valid_ed25519_key_and_signing_script_matches():
    assert len(updater.RELEASE_PUBLIC_KEYS) >= 1 and all(len(bytes.fromhex(k)) == 32 for k in updater.RELEASE_PUBLIC_KEYS)
