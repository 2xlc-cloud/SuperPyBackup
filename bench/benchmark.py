"""Reproduzierbarer Benchmark: SuperPSBackup (PowerShell 5.1) gegen SuperPyBackup (EXE).

    python bench\\benchmark.py --ps-project C:\\Users\\ben\\SuperPSBackup [--iterations 3] [--scenarios small1k,small10k,large100]

Ablauf je Datensatz und Durchlauf (frisches Ziel fuer jedes Programm, identische Quelle fuer alle):
  1. vollstaendige Sicherung   2. zweiter Lauf ohne Aenderung   3. 10 % der Dateien veraendert   4. 10 % der Dateien geloescht
Gemessen wird der gesamte Prozessbaum ueber ein Windows-Job-Objekt (die PyInstaller-EXE startet einen Kindprozess):
Wanduhrzeit, CPU-Zeit (User+Kernel), maximaler Commit-Speicher, E/A-Operationen und -Bytes laut Windows, physischer Mehrverbrauch im Ziel.
Das PowerShell-Projekt wird nur gelesen (eine Kopie von Backup-UserProfile.ps1 laeuft im Arbeitsordner).
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import platform
import random
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from superpybackup import __version__, config as cfgmod  # noqa: E402

CREATE_SUSPENDED = 0x4
CREATE_NO_WINDOW = 0x08000000
SCENARIOS = {
    "small1k": {"files": 1000, "size": 4096, "dirs": 20, "label": "1.000 kleine Dateien (4 KiB)"},
    "small10k": {"files": 10000, "size": 4096, "dirs": 100, "label": "10.000 kleine Dateien (4 KiB)"},
    "large100": {"files": 100, "size": 2 * 1024 * 1024, "dirs": 5, "label": "100 groessere Dateien (2 MiB)"},
}
STEPS = ["full", "unchanged", "modified10", "deleted10"]
STEP_LABEL = {"full": "Erster Lauf (vollstaendig)", "unchanged": "Zweiter Lauf, unveraendert", "modified10": "10 % veraendert", "deleted10": "10 % geloescht"}


# --- Windows Job Object: Messung des gesamten Prozessbaums -------------------------------------
class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class BASIC_ACCOUNTING(ctypes.Structure):
    _fields_ = [("TotalUserTime", ctypes.c_longlong), ("TotalKernelTime", ctypes.c_longlong), ("ThisPeriodTotalUserTime", ctypes.c_longlong), ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD), ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD)]


class BASIC_AND_IO(ctypes.Structure):
    _fields_ = [("Basic", BASIC_ACCOUNTING), ("Io", IO_COUNTERS)]


class BASIC_LIMIT(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong), ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD), ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]


class EXTENDED_LIMIT(ctypes.Structure):
    _fields_ = [("Basic", BASIC_LIMIT), ("Io", IO_COUNTERS), ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


kernel32 = ctypes.windll.kernel32
ntdll = ctypes.windll.ntdll
kernel32.CreateJobObjectW.restype = wintypes.HANDLE
kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
kernel32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]


def measure(command: list[str], env: dict[str, str] | None = None, cwd: str | None = None) -> dict:
    """Startet command angehalten, haengt den Prozess in ein Job-Objekt, setzt fort und liefert die Messwerte."""
    job = kernel32.CreateJobObjectW(None, None)
    started = time.perf_counter()
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, cwd=cwd, creationflags=CREATE_SUSPENDED | CREATE_NO_WINDOW)
    handle = wintypes.HANDLE(int(process._handle))  # noqa: SLF001 - einzige Moeglichkeit, das Prozesshandle zu erhalten
    if not kernel32.AssignProcessToJobObject(job, handle):
        process.kill()
        raise ctypes.WinError()
    ntdll.NtResumeProcess(handle)
    out, err = process.communicate()
    wall = time.perf_counter() - started
    accounting, extended = BASIC_AND_IO(), EXTENDED_LIMIT()
    kernel32.QueryInformationJobObject(job, 8, ctypes.byref(accounting), ctypes.sizeof(accounting), None)
    kernel32.QueryInformationJobObject(job, 9, ctypes.byref(extended), ctypes.sizeof(extended), None)
    kernel32.CloseHandle(job)
    cpu = (accounting.Basic.TotalUserTime + accounting.Basic.TotalKernelTime) / 1e7
    return {
        "exit_code": process.returncode, "wall_s": wall, "cpu_s": cpu,
        "peak_commit_mb": extended.PeakJobMemoryUsed / 1024 / 1024,
        "io_read_ops": accounting.Io.ReadOperationCount, "io_write_ops": accounting.Io.WriteOperationCount, "io_other_ops": accounting.Io.OtherOperationCount,
        "io_read_mb": accounting.Io.ReadTransferCount / 1024 / 1024, "io_write_mb": accounting.Io.WriteTransferCount / 1024 / 1024,
        "processes": accounting.Basic.TotalProcesses, "stdout": out, "stderr": err,
    }


# --- Testdaten ----------------------------------------------------------------------------------
def make_dataset(root: Path, spec: dict, seed: int) -> list[Path]:
    rng = random.Random(seed)
    files = []
    for index in range(spec["files"]):
        folder = root / f"ordner{index % spec['dirs']:03d}"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"datei{index:05d}.dat"
        path.write_bytes(rng.randbytes(spec["size"]))
        files.append(path)
    return files


def modify_ten_percent(files: list[Path], seed: int) -> int:
    rng = random.Random(seed)
    changed = 0
    for index, path in enumerate(files):
        if index % 10 == 3 and path.exists():
            size = path.stat().st_size
            path.write_bytes(rng.randbytes(size))  # gleiche Groesse, neuer Inhalt, neuer Zeitstempel
            changed += 1
    return changed


def delete_ten_percent(files: list[Path]) -> int:
    removed = 0
    for index, path in enumerate(files):
        if index % 10 == 7 and path.exists():
            path.unlink()
            removed += 1
    return removed


def physical_bytes(root: Path) -> tuple[int, int]:
    """Belegter Speicher im Ziel: jede Datei (Volume+Datei-ID) wird nur einmal gezaehlt. Liefert (Bytes, Dateieintraege)."""
    seen: set[tuple[int, int]] = set()
    total = entries = 0
    for current, _, names in os.walk(root):
        for name in names:
            info = os.stat(os.path.join(current, name))
            entries += 1
            key = (info.st_dev, info.st_ino)
            if key not in seen:
                seen.add(key)
                total += info.st_size
    return total, entries


def newest_snapshot(target: Path) -> Path | None:
    daily = target / "Daily"
    names = sorted((p for p in daily.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime) if daily.exists() else []
    return names[-1] if names else None


def tree_hashes(snapshot: Path) -> dict[str, str]:
    result = {}
    for path in snapshot.rglob("*"):
        if path.is_file() and not path.name.startswith(".snapshot"):
            result[path.relative_to(snapshot).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


# --- Programme ----------------------------------------------------------------------------------
class Contender:
    def __init__(self, key: str, label: str) -> None:
        self.key, self.label = key, label
        self.target: Path
        self.logs: Path

    def prepare(self, work: Path, source: Path) -> None:
        self.target = work / f"ziel-{self.key}"
        self.logs = work / f"logs-{self.key}"
        self.target.mkdir()
        self.logs.mkdir()
        self.source = source

    def user_folder(self) -> str:
        from superpybackup.identity import get_identity

        return get_identity().folder

    def snapshot_root(self) -> Path:
        return self.target / self.user_folder()


class PowerShellRef(Contender):
    def __init__(self, script_copy: Path) -> None:
        super().__init__("ps", "SuperPSBackup (PowerShell 5.1)")
        self.script_copy = script_copy

    def command(self) -> tuple[list[str], dict | None]:
        runner = ROOT / "bench" / "ps_runner.ps1"
        powershell = Path(os.environ["SystemRoot"]) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        env = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}  # sauberer Modulpfad fuer Windows PowerShell 5.1
        return [str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(runner), "-Script", str(self.script_copy), "-BackupRoot", str(self.target), "-Source", str(self.source), "-LogRoot", str(self.logs)], env


class PythonExe(Contender):
    def __init__(self, exe: Path, mode: str) -> None:
        super().__init__(f"py-{mode}", f"SuperPyBackup {__version__} (EXE, {'Metadaten' if mode == 'metadata' else 'Hash'})")
        self.exe, self.mode = exe, mode

    def command(self) -> tuple[list[str], dict | None]:
        data = cfgmod.template(str(self.target))
        data["use_known_folders"] = False
        data["extra_sources"] = [{"name": "Test", "path": str(self.source)}]
        data["backup"]["change_detection"] = self.mode
        data["logging"]["directory"] = str(self.logs)
        data["update"]["check_for_updates"] = False
        config_path = self.target.parent / f"config-{self.key}.json"
        config_path.write_text(json.dumps(data), encoding="utf-8")
        env = dict(os.environ, SUPERPYBACKUP_HOME=str(self.target.parent / f"home-{self.key}"))
        env.pop("PSModulePath", None)
        return [str(self.exe), "--config", str(config_path), "backup", "--quiet", "--new-snapshot"], env


def system_info() -> dict:
    def ps(expression: str) -> str:
        return subprocess.run(["powershell", "-NoProfile", "-Command", expression], capture_output=True, text=True, env={k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}).stdout.strip()

    return {
        "os": platform.platform(), "python": sys.version.split()[0],
        "cpu": ps("(Get-CimInstance Win32_Processor | Select -First 1).Name"), "logical_cores": os.cpu_count(),
        "ram_gb": round(int(ps("(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory")) / 1024**3, 1),
        "disk": ps("(Get-PhysicalDisk | Select -First 1 | % { $_.FriendlyName + ' (' + $_.MediaType + ')' })"),
        "powershell": ps("$PSVersionTable.PSVersion.ToString()"),
        "defender": ps("try { (Get-MpComputerStatus).RealTimeProtectionEnabled } catch { 'unbekannt' }"),
    }


def run_iteration(scenario: str, spec: dict, iteration: int, contenders: list[Contender], base: Path, verify: bool) -> list[dict]:
    work = base / f"{scenario}-{iteration}"
    source = work / "quelle"
    work.mkdir(parents=True)
    files = make_dataset(source, spec, seed=1000 + iteration)
    for contender in contenders:
        contender.prepare(work, source)
    rows = []
    order = contenders[iteration % len(contenders):] + contenders[: iteration % len(contenders)]  # Reihenfolge rotieren (Cache-Effekte)
    for step in STEPS:
        if step == "modified10":
            modify_ten_percent(files, seed=2000 + iteration)
        elif step == "deleted10":
            delete_ten_percent(files)
        trees = {}
        for contender in order:
            before_bytes, before_entries = physical_bytes(contender.target)
            command, env = contender.command()
            result = measure(command, env)
            after_bytes, after_entries = physical_bytes(contender.target)
            row = {"scenario": scenario, "iteration": iteration, "step": step, "tool": contender.key, "label": contender.label,
                   "extra_physical_mb": (after_bytes - before_bytes) / 1024 / 1024, "target_physical_mb": after_bytes / 1024 / 1024, "file_entries": after_entries}
            row.update({k: v for k, v in result.items() if k not in ("stdout", "stderr")})
            if result["exit_code"] != 0:
                row["error"] = (result["stderr"] or result["stdout"])[-400:]
            rows.append(row)
            print(f"  [{scenario} #{iteration}] {STEP_LABEL[step]:<30} {contender.key:<12} {row['wall_s']:7.2f}s  cpu {row['cpu_s']:6.2f}s  mem {row['peak_commit_mb']:6.1f} MB  +{row['extra_physical_mb']:8.2f} MB" + ("  FEHLER" if "error" in row else ""), flush=True)
            if verify:
                snapshot = newest_snapshot(contender.snapshot_root())
                trees[contender.key] = tree_hashes(snapshot) if snapshot else {}
        if verify:
            reference = trees[contenders[0].key]
            same = all(tree == reference for tree in trees.values())
            for row in rows[-len(contenders):]:
                row["same_content_as_reference"] = same
            print(f"    Inhaltsvergleich der neuesten Staende ({len(reference)} Dateien): {'identisch' if same else 'ABWEICHEND'}", flush=True)
    shutil.rmtree(work, ignore_errors=True)
    return rows


def baseline(command: list[str], env: dict | None, repeats: int = 5) -> float:
    return statistics.median(measure(command, env)["wall_s"] for _ in range(repeats))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ps-project", required=True, help="Ordner von SuperPSBackup (nur lesend)")
    parser.add_argument("--exe", default=str(ROOT / "dist" / "SuperPyBackupCmd.exe"))
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--scenarios", default="small1k,small10k,large100")
    parser.add_argument("--output", default=str(ROOT / "bench" / "results" / "results.json"))
    parser.add_argument("--base", default=None, help="Arbeitsordner (Standard: temporaer, gleiches Laufwerk wie TEMP)")
    args = parser.parse_args()
    exe = Path(args.exe)
    if not exe.is_file():
        print("EXE fehlt: python build\\build.py", file=sys.stderr)
        return 1
    base = Path(args.base) if args.base else Path(tempfile.mkdtemp(prefix="spb-bench-"))
    base.mkdir(parents=True, exist_ok=True)
    script_copy = base / "Backup-UserProfile.ps1"
    shutil.copy2(Path(args.ps_project) / "Backup-UserProfile.ps1", script_copy)
    contenders: list[Contender] = [PowerShellRef(script_copy), PythonExe(exe, "metadata"), PythonExe(exe, "hash")]
    info = system_info()
    print("System:", json.dumps(info, ensure_ascii=False))
    powershell = str(Path(os.environ["SystemRoot"]) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")
    clean_env = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}
    startup = {"ps": baseline([powershell, "-NoProfile", "-NonInteractive", "-Command", "exit 0"], clean_env), "py": baseline([str(exe), "--version"], clean_env)}
    print("Startzeit (Median aus 5): PowerShell", f"{startup['ps']:.3f}s", "| EXE", f"{startup['py']:.3f}s")
    rows: list[dict] = []
    try:
        for scenario in args.scenarios.split(","):
            spec = SCENARIOS[scenario]
            print(f"\n=== {spec['label']} ===", flush=True)
            for iteration in range(args.iterations):
                rows += run_iteration(scenario, spec, iteration, contenders, base, verify=(iteration == 0))
                Path(args.output).parent.mkdir(parents=True, exist_ok=True)
                Path(args.output).write_text(json.dumps({"system": info, "startup_s": startup, "iterations": args.iterations, "version": __version__, "rows": rows}, indent=1), encoding="utf-8")
    finally:
        shutil.rmtree(base, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
