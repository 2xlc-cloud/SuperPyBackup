"""Tests 27, 28: Update-Pruefung mit simulierten GitHub-Antworten und Verhalten bei fehlerhaften Updates."""

from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from superpybackup import __version__, updater
from superpybackup.runlog import RunLog

REPO = "2xlc-cloud/SuperPyBackup"


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
                    all_assets = {**assets, "SHA256SUMS.txt": sums.encode()}
                    listing = [{"name": n, "size": outer.size_override.get(n, len(d)), "browser_download_url": f"{base}/{REPO}/releases/download/{outer.tag}/{n}"} for n, d in all_assets.items() if n not in outer.drop_assets]
                    return self._send(200, json.dumps({"tag_name": outer.tag, "draft": outer.draft, "prerelease": outer.prerelease, "html_url": f"{base}/{REPO}/releases/tag/{outer.tag}", "assets": listing}).encode())
                prefix = f"/{REPO}/releases/download/{outer.tag}/"
                if self.path.startswith(prefix):
                    name = self.path[len(prefix):]
                    if name == "SHA256SUMS.txt":
                        return self._send(200, outer.sums_text().encode())
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

    @property
    def settings(self) -> dict:
        return {"check_for_updates": True, "check_interval_hours": 24, "repository": REPO, "api_url": f"http://127.0.0.1:{self.port}"}

    def asset_requests(self) -> list[str]:
        return [path for path in self.requests if "/releases/download/" in path]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def github():
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
