"""Erzeugt aus bench\\results\\results.json die Markdown-Tabellen fuer PowerShell-vs-Python.md (keine Handeingabe von Messwerten)."""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STEPS = [("full", "Erster Lauf (vollständig)"), ("unchanged", "Zweiter Lauf, unverändert"), ("modified10", "10 % verändert"), ("deleted10", "10 % gelöscht")]
SCENARIO_LABEL = {"small1k": "1.000 kleine Dateien (je 4 KiB)", "small10k": "10.000 kleine Dateien (je 4 KiB)", "large100": "100 größere Dateien (je 2 MiB)"}
TOOLS = [("ps", "PowerShell"), ("py-metadata", "Python (Metadaten)"), ("py-hash", "Python (Hash)")]


def med(values: list[float]) -> float:
    return statistics.median(values) if values else float("nan")


def cell(rows: list[dict], key: str, fmt: str = "{:.2f}") -> str:
    values = [row[key] for row in rows]
    return fmt.format(med(values))


def spread(rows: list[dict], key: str) -> str:
    values = [row[key] for row in rows]
    return f"{min(values):.2f}–{max(values):.2f}"


def render(data: dict) -> str:
    rows = data["rows"]
    out: list[str] = []
    system = data["system"]
    out.append(f"**Messumgebung:** {system['cpu']} ({system['logical_cores']} logische Kerne), {system['ram_gb']} GB RAM, Datenträger: {system['disk']}, {system['os']}, Windows PowerShell {system['powershell']}, Python {system['python']}, Defender-Echtzeitschutz: {system['defender']}.  ")
    out.append(f"**Durchläufe je Szenario:** {data['iterations']} (Median; in Klammern Minimum–Maximum der Laufzeit). Programmstart (Median aus 5): PowerShell {data['startup_s']['ps']:.2f} s, SuperPyBackup-EXE {data['startup_s']['py']:.2f} s.\n")
    failures = [row for row in rows if row.get("error") or row.get("exit_code") not in (0, None)]
    out.append(f"**Fehlgeschlagene Läufe:** {len(failures)} von {len(rows)}.\n")
    verified = [row for row in rows if "same_content_as_reference" in row]
    if verified:
        out.append(f"**Inhaltsvergleich** der jeweils neuesten Stände (Durchlauf 1, alle Schritte): {'identisch bei allen Programmen' if all(row['same_content_as_reference'] for row in verified) else 'ABWEICHUNGEN – siehe Rohdaten'}.\n")
    for scenario, label in SCENARIO_LABEL.items():
        subset = [row for row in rows if row["scenario"] == scenario]
        if not subset:
            continue
        out.append(f"### {label}\n")
        out.append("| Schritt | Programm | Laufzeit s (min–max) | davon ohne Programmstart s | CPU-Zeit s | Spitzen-Speicher MB | Mehrverbrauch im Ziel MB | E/A-Operationen (Lesen+Schreiben+Sonst.) | E/A-Daten MB (Lesen / Schreiben) |")
        out.append("|---|---|---:|---:|---:|---:|---:|---:|---:|")
        for step, step_label in STEPS:
            for key, tool_label in TOOLS:
                selected = [row for row in subset if row["step"] == step and row["tool"] == key]
                if not selected:
                    continue
                startup = data["startup_s"]["ps"] if key == "ps" else data["startup_s"]["py"]
                ops = med([row["io_read_ops"] + row["io_write_ops"] + row["io_other_ops"] for row in selected])
                out.append(f"| {step_label} | {tool_label} | {cell(selected, 'wall_s')} ({spread(selected, 'wall_s')}) | {max(med([r['wall_s'] for r in selected]) - startup, 0):.2f} | {cell(selected, 'cpu_s')} | {cell(selected, 'peak_commit_mb', '{:.1f}')} | {cell(selected, 'extra_physical_mb')} | {ops:,.0f} | {cell(selected, 'io_read_mb', '{:.1f}')} / {cell(selected, 'io_write_mb', '{:.1f}')} |")
        total = {key: sum(med([row["wall_s"] for row in subset if row["step"] == step and row["tool"] == key]) for step, _ in STEPS) for key, _ in TOOLS}
        base = total["ps"]
        out.append("")
        out.append("Summe der Median-Laufzeiten aller vier Schritte: " + ", ".join(f"{label} **{total[key]:.1f} s**" + ("" if key == "ps" else f" ({base / total[key]:.1f}× {'schneller' if total[key] < base else 'langsamer'} als PowerShell)" if total[key] and base else "") for key, label in TOOLS) + ".\n")
    return "\n".join(out)


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "bench" / "results" / "results.json"
    print(render(json.loads(path.read_text(encoding="utf-8"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
