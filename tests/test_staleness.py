"""Warnung bei ausbleibender Sicherung (backup.warn_after_days)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from conftest import write_tree
from superpybackup import config as cfgmod
from superpybackup import engine, staleness

NOW = datetime(2026, 11, 20, 12, 0, 0)


@pytest.fixture()
def sink(env, tmp_path, monkeypatch):
    path = tmp_path / "meldungen.jsonl"
    monkeypatch.setenv("SUPERPYBACKUP_NOTIFY_FILE", str(path))
    return path


def make(env, **backup):
    data = dict(env.config().data)
    data["notifications"] = {**data["notifications"], "enabled": True}
    data["backup"] = {**data["backup"], **backup}
    return cfgmod.from_dict(data)


def messages(sink):
    return [json.loads(line) for line in sink.read_text(encoding="utf-8").splitlines()] if sink.exists() else []


def test_default_is_three_days_and_zero_switches_off(env):
    assert env.config().backup["warn_after_days"] == 3
    with pytest.raises(cfgmod.ConfigError, match="warn_after_days"):
        make(env, warn_after_days=-1)
    config = make(env, warn_after_days=0)
    staleness.record_success(config, NOW - timedelta(days=100))
    assert staleness.overdue_days(config, NOW) is None


def test_no_warning_without_history_until_the_limit_since_first_check(env, sink):
    config = make(env)
    assert staleness.check(config, now=NOW) is False  # erste Pruefung: nur den Beginn merken
    assert staleness.check(config, now=NOW + timedelta(days=2)) is False
    assert staleness.check(config, now=NOW + timedelta(days=3)) is True
    assert [m["title"] for m in messages(sink)] == ["Sicherung überfällig"]


def test_warns_after_the_limit_and_only_once_a_day(env, sink):
    config = make(env)
    staleness.record_success(config, NOW - timedelta(days=2, hours=23))
    assert staleness.check(config, now=NOW) is False  # knapp unter 3 Tagen
    staleness.record_success(config, NOW - timedelta(days=5))
    assert staleness.check(config, now=NOW) is True
    assert staleness.check(config, now=NOW + timedelta(hours=3)) is False  # am selben Tag nicht noch einmal
    assert staleness.check(config, now=NOW + timedelta(days=1)) is True
    assert len(messages(sink)) == 2 and "5 Tage" in messages(sink)[0]["message"] and "15.11.2026" in messages(sink)[0]["message"]


def test_success_resets_the_warning(env, sink):
    config = make(env)
    staleness.record_success(config, NOW - timedelta(days=10))
    assert staleness.check(config, now=NOW) is True
    staleness.record_success(config, NOW)
    assert staleness.check(config, now=NOW + timedelta(days=1)) is False
    assert staleness.check(config, now=NOW + timedelta(days=4)) is True


def test_a_successful_backup_records_the_success(env):
    write_tree(env.src, {"a.txt": "a"})
    assert env.run().status == engine.COMPLETE
    first = staleness.last_success(env.config())
    assert first is not None and abs((datetime.now() - first).total_seconds()) < 60
    assert env.run().status == engine.ALREADY  # auch "heute bereits gesichert" zaehlt als Erfolg


def test_a_failed_backup_does_not_count(env):
    result = env.run(config_overrides={"backup_root": str(env.root / "fehlt" / "ziel")})
    assert result.status == engine.FAILED and staleness.last_success(env.config()) is None


def test_jobs_are_tracked_separately(env):
    data = dict(env.config().data)
    other = cfgmod.from_dict(data, path=env.root / "jobs" / "Eigenes.json")
    staleness.record_success(other, NOW)
    assert staleness.last_success(other) == NOW and staleness.last_success(env.config()) is None


def test_failed_scheduled_backup_warns_when_overdue(env, sink):
    from superpybackup import cli

    write_tree(env.src, {"a.txt": "a"})
    env.write_config_file(notifications={"enabled": True, "on_error": True}, backup={"warn_after_days": 1}, backup_root=str(env.root / "fehlt" / "ziel"))
    config = cfgmod.load(str(env.root / "config.json"), require_target=False)
    staleness.record_success(config, datetime.now() - timedelta(days=4))
    assert cli.main(["--config", str(env.root / "config.json"), "backup", "--quiet"]) == 1
    titles = [m["title"] for m in messages(sink)]
    assert "Sicherung fehlgeschlagen" in titles and "Sicherung überfällig" in titles
