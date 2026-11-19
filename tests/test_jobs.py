"""Mehrere Sicherungen: Verwaltung (jobs), eigener Zielordner je Sicherung, backup --job / --all, Aufgaben je Sicherung und fuer alle."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest

from conftest import write_tree
from superpybackup import config as cfgmod, engine, jobs, scheduler
from superpybackup.identity import get_identity


def make_job(env, name: str, files: dict[str, str]) -> Path:
    """Legt eine weitere Sicherung mit eigenem Quellordner an (Ziel und Einstellungen von der Hauptsicherung uebernommen)."""
    base = env.write_config_file()
    job = jobs.create(base, name, base)
    source = env.root / f"src-{name}"
    write_tree(source, files)
    data = json.loads(job.path.read_text(encoding="utf-8"))
    data["extra_sources"] = [{"name": "Arbeit", "path": str(source)}]
    job.path.write_text(json.dumps(data), encoding="utf-8")
    return job.path


def folders(env) -> list[str]:
    return sorted(p.name for p in env.dst.iterdir() if p.is_dir())


def test_names_paths_and_listing(env):
    base = env.write_config_file()
    assert jobs.path_for(base, None) == base and jobs.path_for(base, "standard") == base
    assert jobs.path_for(base, "Arbeit") == base.parent / "jobs" / "Arbeit.json"
    for bad in ("", "a b", "../x", "Standard", "STANDARD", "-x", "x" * 41, "a/b"):
        with pytest.raises(cfgmod.ConfigError):
            jobs.validate_name(bad)
    assert [j.name for j in jobs.list_jobs(base)] == ["Standard"]
    jobs.create(base, "Zeta")
    jobs.create(base, "alpha")
    (base.parent / "jobs" / "kaputt name.json").write_text("{}", encoding="utf-8")  # ungueltiger Name wird uebergangen
    assert [j.name for j in jobs.list_jobs(base)] == ["Standard", "alpha", "Zeta"]
    with pytest.raises(cfgmod.ConfigError, match="bereits"):
        jobs.create(base, "ALPHA")  # Windows unterscheidet Gross-/Kleinschreibung nicht
    with pytest.raises(cfgmod.ConfigError, match="keine Sicherung"):
        jobs.find(base, "gibt-es-nicht")


def test_task_names_per_job_and_for_all():
    assert jobs.task_name("SuperPyBackup-S-1", None) == "SuperPyBackup-S-1" and jobs.task_name("SuperPyBackup-S-1", "Standard") == "SuperPyBackup-S-1"
    assert jobs.task_name("SuperPyBackup-S-1", "Foto") == "SuperPyBackup-S-1-Job-Foto"
    assert jobs.all_task_name("SuperPyBackup-S-1") == "SuperPyBackup-S-1-Alle"
    for name in (jobs.task_name("SuperPyBackup-S-1", "Foto"), jobs.all_task_name("SuperPyBackup-S-1")):
        scheduler.validate_name(name)


def test_each_job_has_its_own_target_folder_and_remove_keeps_backups(env):
    write_tree(env.src, {"a.txt": "standard"})
    base = env.write_config_file()
    job_path = make_job(env, "arbeit", {"b.txt": "arbeit"})
    identity = get_identity()
    standard, other = cfgmod.load(base), cfgmod.load(job_path)
    assert (standard.job, other.job) == ("", "arbeit")
    assert standard.target_dir(identity) == os.path.join(standard.backup_root, identity.folder)
    assert other.target_dir(identity) == os.path.join(other.backup_root, identity.folder + "__arbeit")
    assert engine.run_backup(standard).status == engine.COMPLETE
    assert engine.run_backup(other).status == engine.COMPLETE
    assert folders(env) == sorted([identity.folder, identity.folder + "__arbeit"])  # getrennte Staende
    assert (Path(other.target_dir(identity)) / "Daily").is_dir()
    assert jobs.remove(base, "arbeit") is True and not job_path.exists()
    assert (Path(other.target_dir(identity)) / "Daily").is_dir()  # gesicherte Daten bleiben
    with pytest.raises(cfgmod.ConfigError):
        jobs.remove(base, "Standard")


def test_invalid_job_file_name_is_rejected_on_load(env):
    base = env.write_config_file()
    bad = base.parent / "jobs" / "Standard.json"
    bad.parent.mkdir(exist_ok=True)
    bad.write_text(json.dumps(cfgmod.template(str(env.dst))), encoding="utf-8")
    with pytest.raises(cfgmod.ConfigError, match="Ungueltiger Name"):
        cfgmod.load(bad, require_target=False)


def test_cli_job_add_list_remove_and_backup_job(env):
    write_tree(env.src, {"a.txt": "standard"})
    env.write_config_file()
    assert env.cli("job", "add", "arbeit", "--copy-from", "Standard").returncode == 0
    listing = env.cli("job", "list").stdout
    assert "Standard" in listing and "arbeit" in listing
    assert env.cli("job", "add", "arbeit").returncode == 1  # doppelt
    result = env.cli("--job", "arbeit", "backup")
    assert result.returncode == 0, result.stdout + result.stderr
    identity = get_identity()
    assert identity.folder + "__arbeit" in folders(env) and identity.folder not in folders(env)  # nur die gewaehlte Sicherung lief
    assert env.cli("--job", "gibt-es-nicht", "backup").returncode == 1
    removed = env.cli("job", "remove", "arbeit")
    assert removed.returncode == 0 and "bleiben erhalten" in removed.stdout
    assert env.cli("job", "remove", "Standard").returncode == 1
    assert identity.folder + "__arbeit" in folders(env)  # Daten sind geblieben


def test_cli_backup_all_runs_every_job_and_a_broken_one_does_not_stop_the_rest(env):
    write_tree(env.src, {"a.txt": "standard"})
    base = env.write_config_file()
    make_job(env, "arbeit", {"b.txt": "arbeit"})
    broken = base.parent / "jobs" / "kaputt.json"
    broken.write_text("{ kein json", encoding="utf-8")
    result = env.cli("backup", "--all")
    identity = get_identity()
    assert result.returncode != 0  # die fehlerhafte Sicherung wird gemeldet ...
    assert identity.folder in folders(env) and identity.folder + "__arbeit" in folders(env)  # ... die uebrigen laufen trotzdem
    assert "=== Sicherung Standard ===" in result.stdout and "=== Sicherung arbeit ===" in result.stdout and "kaputt" in result.stderr
    broken.unlink()
    second = env.cli("backup", "--all")
    assert second.returncode == 0, second.stdout + second.stderr
    assert env.cli("--job", "arbeit", "backup", "--all").returncode == 3  # schliessen sich aus


def test_cli_task_for_a_job_and_for_all(env):
    env.write_config_file()
    make_job(env, "arbeit", {"b.txt": "x"})
    name_all, name_job = "SuperPyBackupTest-" + uuid.uuid4().hex[:8], "SuperPyBackupTest-" + uuid.uuid4().hex[:8]
    try:
        installed = env.cli("task", "install", "--all", "--name", name_all, "--daily-at", "21:15")
        assert installed.returncode == 0, installed.stdout + installed.stderr
        info = scheduler.status(name_all)
        assert info["exists"] and info["daily_at"] == "21:15"
        assert info["arguments"].endswith("backup --all --quiet") and str(env.root / "config.json") in info["arguments"]
        installed = env.cli("--job", "arbeit", "task", "install", "--name", name_job)
        assert installed.returncode == 0, installed.stdout + installed.stderr
        arguments = scheduler.status(name_job)["arguments"]
        assert "jobs" in arguments and "arbeit.json" in arguments and arguments.endswith("backup --quiet")  # die Aufgabe startet genau diese Sicherung
        assert env.cli("--job", "arbeit", "task", "install", "--all", "--name", name_job).returncode == 3
    finally:
        for name in (name_all, name_job):
            scheduler.uninstall(name)
