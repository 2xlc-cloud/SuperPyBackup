"""Gemeinsame Testhilfen. Alle Tests arbeiten ausschliesslich in temporaeren Ordnern mit kuenstlichen Daten."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from superpybackup import config as cfgmod, engine, snapshots  # noqa: E402
from superpybackup.identity import get_identity  # noqa: E402


@dataclass
class Env:
    root: Path
    src: Path
    dst: Path
    home: Path

    @property
    def identity(self):
        return get_identity()

    @property
    def target(self) -> Path:
        return self.dst / self.identity.folder

    def config(self, **overrides):
        data = cfgmod.template(str(self.dst))
        data["use_known_folders"] = False
        data["extra_sources"] = [{"name": "Test", "path": str(self.src)}]
        data["logging"]["directory"] = str(self.home / "Logs")
        data["update"]["check_for_updates"] = False
        for key, value in overrides.items():
            if isinstance(value, dict) and isinstance(data.get(key), dict):
                data[key].update(value)
            else:
                data[key] = value
        return cfgmod.from_dict(data, path=self.root / "config.json")

    def write_config_file(self, **overrides) -> Path:
        import json

        config = self.config(**overrides)
        path = self.root / "config.json"
        path.write_text(json.dumps(config.data, indent=2), encoding="utf-8")
        return path

    def run(self, when: str = "2026-03-10 12:00:00", **kwargs):
        overrides = kwargs.pop("config_overrides", {})
        return engine.run_backup(self.config(**overrides), as_of=datetime.strptime(when, "%Y-%m-%d %H:%M:%S"), **kwargs)

    def snapshot(self, tier: str, name: str) -> Path:
        return self.target / tier / name

    def cli(self, *args: str, extra_env: dict | None = None, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), SUPERPYBACKUP_HOME=str(self.home))
        if extra_env:
            env.update(extra_env)
        return subprocess.run([sys.executable, "-m", "superpybackup", "--config", str(self.root / "config.json"), *args], capture_output=True, text=True, env=env, timeout=timeout, stdin=subprocess.DEVNULL)


@pytest.fixture
def env(tmp_path, monkeypatch) -> Env:
    home = tmp_path / "home"
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    for folder in (home, src, dst):
        folder.mkdir()
    monkeypatch.setenv("SUPERPYBACKUP_HOME", str(home))
    monkeypatch.delenv("SUPERPYBACKUP_TEST_DELAY_MS", raising=False)
    return Env(tmp_path, src, dst, home)


def write_tree(root: Path, files: dict[str, str | bytes]) -> None:
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            path.write_text(content, encoding="utf-8")
        else:
            path.write_bytes(content)


def read_tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file() and not p.name.startswith(".snapshot")}


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        digest.update(path.relative_to(root).as_posix().encode())
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def daily_names(env: Env) -> list[str]:
    return sorted(ref.name for ref in snapshots.list_snapshots(str(env.target), env.identity.sid, ("Daily",)))


def tier_names(env: Env, tier: str) -> list[str]:
    return sorted(ref.name for ref in snapshots.list_snapshots(str(env.target), env.identity.sid, (tier,)))


@pytest.fixture(autouse=True)
def _never_show_real_notifications(tmp_path, monkeypatch):
    """Meldungen landen in einer Datei statt auf dem Desktop (einzelne Tests ueberschreiben das bewusst)."""
    monkeypatch.setenv("SUPERPYBACKUP_NOTIFY_FILE", str(tmp_path / "unbeachtet.jsonl"))


@pytest.fixture(autouse=True)
def _never_touch_the_real_desktop(tmp_path, monkeypatch):
    """Desktop-Verknuepfungen entstehen in Tests in einem Temp-Ordner, nie auf dem echten Desktop."""
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    monkeypatch.setenv("SUPERPYBACKUP_DESKTOP", str(desktop))


@pytest.fixture(autouse=True)
def _isolate_network_shares(tmp_path, monkeypatch):
    """Tests nutzen nie die echten Anmeldedaten und merken sich keine Verbindungen aus anderen Tests."""
    from superpybackup import netshare

    if not os.environ.get("SUPERPYBACKUP_HOME"):
        monkeypatch.setenv("SUPERPYBACKUP_HOME", str(tmp_path / "home-isolated"))
    monkeypatch.setattr(netshare, "_connected", {})


TEST_SEED = bytes(range(1, 33))  # Testschluessel der nachgebildeten GitHub-Releases; der echte Schluessel liegt ausserhalb des Repositorys


@pytest.fixture(autouse=True)
def _test_release_key(monkeypatch):
    """Updates der Tests sind mit dem Testschluessel signiert; der eingebettete echte Schluessel wird dafuer ersetzt."""
    from superpybackup import ed25519, updater

    monkeypatch.setattr(updater, "RELEASE_PUBLIC_KEYS", (ed25519.public_key(TEST_SEED).hex(),))
