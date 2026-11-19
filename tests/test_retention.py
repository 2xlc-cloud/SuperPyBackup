"""Tests 7-10: Aufbewahrung 7 Tage / 12 Monate / 5 Jahre."""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta

from conftest import daily_names, tier_names, write_tree
from superpybackup import engine, manifest as mf, restore, retention, snapshots
from superpybackup.runlog import RunLog


def run_on(env, day: date, content: str | None = None, **kwargs):
    if content is not None:
        write_tree(env.src, {"a.txt": content})
    return env.run(f"{day:%Y-%m-%d} 12:00:00", **kwargs)


# --- reine Fensterberechnung -------------------------------------------------------------------
def test_cutoffs_are_calendar_windows():
    window = retention.cutoffs(date(2026, 3, 10), 7, 12, 5)
    assert window.daily == date(2026, 3, 4)  # heute + 6 Vortage = 7 Kalendertage
    assert window.monthly == date(2025, 3, 1)  # 12 abgeschlossene Monate (der laufende hat noch keinen Monatsstand)
    assert window.yearly == date(2021, 1, 1)  # 5 abgeschlossene Jahre
    assert retention.cutoffs(date(2026, 1, 31), 1, 1, 1).daily == date(2026, 1, 31)
    assert retention.cutoffs(date(2024, 3, 1), 7, 12, 5).daily == date(2024, 2, 24)  # Schaltjahr
    assert retention.cutoffs(date(2026, 1, 15), 7, 3, 5).monthly == date(2025, 10, 1)  # Jahreswechsel


# 7 ------------------------------------------------------------------------------------------
def test_daily_retention_keeps_last_seven_calendar_days(env):
    start = date(2026, 3, 1)
    for offset in range(12):
        result = run_on(env, start + timedelta(days=offset), f"inhalt {offset}")
        assert result.status == engine.COMPLETE
    assert daily_names(env) == [f"2026-03-{day:02d}" for day in range(6, 13)]
    for name in daily_names(env):  # verbleibende Staende sind vollstaendig lesbar, trotz entfernter Vorgaenger
        assert restore.verify_snapshot(str(env.snapshot("Daily", name)), RunLog(str(env.home / "Logs")), env.identity.sid)[1] == []


def test_gaps_in_daily_runs_do_not_extend_the_window(env):
    for day in (date(2026, 3, 1), date(2026, 3, 2), date(2026, 3, 20)):
        run_on(env, day, f"x{day}")
    assert daily_names(env) == ["2026-03-20"]  # nur Kalendertage zaehlen, nicht die Anzahl der Staende
    assert tier_names(env, "Monthly") == []  # der Monat ist noch nicht vorbei


# 8 ------------------------------------------------------------------------------------------
def test_monthly_retention_keeps_last_twelve_calendar_months(env):
    months = [(2025 + (index // 12), index % 12 + 1) for index in range(15)]  # 2025-01 .. 2026-03
    for year, month in months:
        assert run_on(env, date(year, month, 15), f"m{year}{month}").status == engine.COMPLETE
    assert tier_names(env, "Monthly") == [f"2025-{m:02d}" for m in range(3, 13)] + ["2026-01", "2026-02"]  # 12 abgeschlossene Monate, der laufende (2026-03) fehlt noch
    # Monatsstaende bleiben unabhaengig von der taeglichen Rotation lesbar
    first = env.snapshot("Monthly", "2025-03") / "Test" / "a.txt"
    assert first.read_text() == "m20253"


def test_monthly_and_yearly_stands_appear_only_when_the_period_is_over(env):
    run_on(env, date(2026, 3, 3), "erster")
    assert tier_names(env, "Monthly") == [] and tier_names(env, "Yearly") == []  # nicht schon beim ersten Stand
    run_on(env, date(2026, 3, 20), "spaeter")
    run_on(env, date(2026, 3, 31), "monatsende")
    assert tier_names(env, "Monthly") == [] and tier_names(env, "Yearly") == []  # auch am letzten Tag des Monats noch nicht
    run_on(env, date(2026, 4, 2), "april")
    assert tier_names(env, "Monthly") == ["2026-03"] and tier_names(env, "Yearly") == []  # Monat vorbei: der Stand vom Monatsende
    assert (env.snapshot("Monthly", "2026-03") / "Test" / "a.txt").read_text() == "monatsende"
    run_on(env, date(2026, 4, 20), "spaeter im april")
    assert (env.snapshot("Monthly", "2026-03") / "Test" / "a.txt").read_text() == "monatsende"  # vorhandener Monatsstand bleibt unveraendert
    run_on(env, date(2026, 12, 31), "jahresende")
    assert tier_names(env, "Yearly") == []
    run_on(env, date(2027, 1, 3), "neues jahr")
    assert tier_names(env, "Monthly") == ["2026-03", "2026-04", "2026-12"] and tier_names(env, "Yearly") == ["2026"]
    assert (env.snapshot("Yearly", "2026") / "Test" / "a.txt").read_text() == "jahresende"  # letzter Monatsstand des Jahres
    assert os.path.samefile(env.snapshot("Yearly", "2026") / "Test" / "a.txt", env.snapshot("Monthly", "2026-12") / "Test" / "a.txt")  # gleiche Dateien, kein Duplikat


# 9 ------------------------------------------------------------------------------------------
def test_yearly_retention_keeps_last_five_calendar_years(env):
    for year in range(2019, 2027):
        assert run_on(env, date(year, 6, 1), f"j{year}").status == engine.COMPLETE
    assert tier_names(env, "Yearly") == ["2021", "2022", "2023", "2024", "2025"]  # 5 abgeschlossene Jahre, 2026 laeuft noch
    assert (env.snapshot("Yearly", "2021") / "Test" / "a.txt").read_text() == "j2021"


# 10 -----------------------------------------------------------------------------------------
def test_combined_retention_over_six_years(env):
    runs = [date(year, month, 1) for year in range(2020, 2027) for month in range(1, 13) if (year, month) <= (2026, 3)]
    for index, day in enumerate(runs):
        assert run_on(env, day, f"stand {index}").status == engine.COMPLETE
    assert daily_names(env) == ["2026-03-01"]
    assert tier_names(env, "Monthly") == [f"2025-{m:02d}" for m in range(3, 13)] + ["2026-01", "2026-02"]
    assert tier_names(env, "Yearly") == ["2021", "2022", "2023", "2024", "2025"]
    # Ein Stand ist gleichzeitig Tages-, Monats- und Jahresstand und teilt sich dieselben Dateien
    log = RunLog(str(env.home / "Logs"))
    for ref in snapshots.list_snapshots(str(env.target), env.identity.sid):
        assert ref.manifest and ref.manifest.status == mf.COMPLETE
        assert restore.verify_snapshot(ref.path, log, env.identity.sid)[1] == []
    daily, yearly = env.snapshot("Daily", "2026-03-01") / "Test" / "a.txt", env.snapshot("Yearly", "2025") / "Test" / "a.txt"
    assert daily.read_text() == "stand 74" and yearly.read_text() == "stand 71"  # Jahresstand = letzter Monatsstand von 2025 (Dezember)
    dec = env.snapshot("Monthly", "2025-12") / "Test" / "a.txt"
    assert os.path.samefile(dec, yearly)  # Dezember-Stand ist zugleich Monats- und Jahresstand (keine Duplikate)


def test_custom_retention_values_are_respected(env):
    for offset in range(6):
        run_on(env, date(2026, 3, 1) + timedelta(days=offset), f"x{offset}", config_overrides={"retention": {"daily": 3}})
    assert daily_names(env) == ["2026-03-04", "2026-03-05", "2026-03-06"]


def test_no_cleanup_without_successful_new_stand(env):
    run_on(env, date(2026, 3, 1), "alt")
    # Quelle fehlt -> Lauf scheitert; der alte Stand ausserhalb des Fensters darf nicht entfernt werden
    result = env.run("2026-03-20 12:00:00", config_overrides={"extra_sources": [{"name": "Test", "path": str(env.root / "fehlt")}]})
    assert result.status == engine.FAILED and daily_names(env) == ["2026-03-01"]


def test_partial_run_triggers_no_rotation(env):
    import ctypes
    from test_backup_core import _lock_exclusively

    run_on(env, date(2026, 3, 1), "alt")
    write_tree(env.src, {"gesperrt.txt": "x"})
    handle = _lock_exclusively(str(env.src / "gesperrt.txt"))
    try:
        result = env.run("2026-03-20 12:00:00")
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)
    assert result.status == engine.PARTIAL
    assert "2026-03-01" in daily_names(env)  # trotz Ablauf des 7-Tage-Fensters noch vorhanden
    assert result.stats.retention_removed == []


def test_invalid_snapshot_dirs_are_never_deleted(env):
    run_on(env, date(2026, 3, 1), "alt")
    broken = env.target / "Daily" / "2026-01-01"
    broken.mkdir()
    (broken / "wichtig.txt").write_text("nicht loeschen")
    unknown = env.target / "Daily" / "manuell-angelegt"
    unknown.mkdir()
    run_on(env, date(2026, 3, 20), "neu")
    assert (broken / "wichtig.txt").exists() and unknown.exists()
    log_text = "".join(p.read_text(encoding="utf-8") for p in (env.home / "Logs").glob("*.log"))
    assert "kein gueltiges Manifest" in log_text


def test_retention_never_removes_the_newest_complete_stand(env):
    run_on(env, date(2026, 3, 10), "neu")
    removed = retention.apply_retention(str(env.target), env.identity.sid, date(2030, 1, 1), 7, 12, 5, RunLog(str(env.home / "Logs")))
    assert "daily/2026-03-10" not in removed and daily_names(env) == ["2026-03-10"]  # neuester vollstaendiger Tagesstand bleibt immer
