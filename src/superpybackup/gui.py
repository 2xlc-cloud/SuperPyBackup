"""Grafische Oberfläche von SuperPyBackup (tkinter, nur Standardbibliothek).

Start: SuperPyBackup.exe ohne Argumente (Doppelklick), `SuperPyBackupCmd.exe gui` oder `python -m superpybackup gui`.

Alle langen Vorgaenge (Sicherung, Wiederherstellung, Prüfung, Update) laufen in Hintergrund-Threads; die Oberfläche
bleibt bedienbar und zeigt Fortschritt und Protokoll. Dialoge laufen über ask_yes_no/info/error und sind für Tests ersetzbar.
"""

from __future__ import annotations

import copy
import ctypes
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any, Callable

from . import __version__, cleanup as cleanupmod, config as cfgmod, engine, faq, fsutil, manifest as mf, notify as notifymod, restore as restoremod, scheduler, snapshots, updater, winapi
from .identity import get_identity
from .runlog import RunLog

KNOWN_LABELS = {"Desktop": "Desktop", "Documents": "Dokumente", "Pictures": "Bilder", "Music": "Musik", "Videos": "Videos", "Downloads": "Downloads", "Favorites": "Favoriten"}
EXISTING_LABELS = {"skip": "Nichts tun (Standard)", "new_snapshot": "Weiteren Stand anlegen"}
CHANGE_LABELS = {"metadata": "Größe und Zeitstempel (schnell)", "hash": "Inhalt per SHA-256 (gründlich)"}
DUMMY = "/__dummy__"
PALETTES = {
    "dark": {"bg": "#1b1e24", "panel": "#232830", "field": "#14171c", "fg": "#e6eaf2", "muted": "#9aa4b5", "accent": "#4c8dff", "border": "#3a4250", "select": "#2f5fb3", "button": "#2b313b", "button_hover": "#38404d", "ok": "#5ad17a", "bad": "#ff7b72", "disabled": "#6b7585"},
    "light": {"bg": "#f0f0f0", "panel": "#f0f0f0", "field": "#ffffff", "fg": "#1a1a1a", "muted": "#555555", "accent": "#0067c0", "border": "#adadad", "select": "#0063b1", "button": "#e1e1e1", "button_hover": "#e5f1fb", "ok": "#1b7a2b", "bad": "#b3261e", "disabled": "#8a8a8a"},
}


def _human(size: int | None) -> str:
    if size is None:
        return ""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return str(size)


class QueueWriter:
    """Dateiähnliches Objekt, das Protokollzeilen des Engines in die Oberfläche leitet."""

    def __init__(self, sink: queue.Queue) -> None:
        self.sink = sink

    def write(self, text: str) -> int:
        self.sink.put(("log", text))
        return len(text)

    def flush(self) -> None:
        pass


class ScrollFrame(ttk.Frame):
    """Scrollbarer Bereich für die Einstellungen."""

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent)
        canvas = tk.Canvas(self, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        self.body = ttk.Frame(canvas)
        self.body.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
        window = canvas.create_window((0, 0), window=self.body, anchor="nw")
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        canvas.bind_all("<MouseWheel>", lambda event: canvas.yview_scroll(int(-event.delta / 120), "units") if self.winfo_ismapped() else None)


class App(tk.Tk):
    def __init__(self, config_path: str | os.PathLike[str] | None = None, *, task_name: str | None = None, hidden: bool = False) -> None:
        super().__init__()
        self.title(f"SuperPyBackup {__version__}")
        self.geometry("980x720")
        self.minsize(860, 620)
        if hidden:
            self.withdraw()
        self.config_path = Path(config_path) if config_path else cfgmod.default_config_path()
        self.task_name = task_name or scheduler.default_task_name()
        self.update_task_name = scheduler.update_task_name(self.task_name)
        self.config_data: dict[str, Any] = cfgmod.template("")
        self.config: cfgmod.Config | None = None
        self.messages: queue.Queue = queue.Queue()
        self._busy = 0  # alle Hintergrundvorgänge
        self._ops = 0  # nur Benutzeraktionen (Sicherung, Wiederherstellung, Prüfung)
        self._cancel = threading.Event()
        self._progress: tuple[str, int, int, str] | None = None
        self._backup_running = False
        self.last_result: engine.RunResult | None = None
        self.last_restore: restoremod.RestoreResult | None = None
        self.last_update = None
        self.snapshot_refs: dict[str, snapshots.SnapshotRef] = {}
        self._tree_cache: dict | None = None
        self._tree_manifest: mf.Manifest | None = None
        self._closing = False
        # Dialoge: in Tests ersetzbar
        self.ask_yes_no: Callable[[str, str], bool] = lambda title, message: messagebox.askyesno(title, message, parent=self)
        self.info: Callable[[str, str], None] = lambda title, message: messagebox.showinfo(title, message, parent=self)
        self.error: Callable[[str, str], None] = lambda title, message: messagebox.showerror(title, message, parent=self)
        self.ask_folder: Callable[[str], str] = lambda title: filedialog.askdirectory(title=title, parent=self)
        self.ask_text: Callable[[str, str, str], str | None] = lambda title, prompt, initial: simpledialog.askstring(title, prompt, initialvalue=initial, parent=self)
        self.theme = "dark"  # Standard; die Konfiguration (gui.theme) kann es aendern
        self._style()
        self._build_ui()
        self.apply_theme(self.theme)  # nach dem Aufbau: auch tk-Widgets und Titelleiste einfaerben
        self.load_config()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self._poll)

    # ------------------------------------------------------------------ Aufbau
    def _style(self) -> None:
        """Legt die ttk-Stile an; die Farben setzt apply_theme (Standard: dunkel)."""
        self.style = ttk.Style(self)
        self.apply_theme(self.theme)

    def apply_theme(self, name: str) -> None:
        """Wendet ein Farbschema (dark/light) auf alle ttk- und tk-Widgets und die Windows-Titelleiste an."""
        if name not in PALETTES:
            name = "dark"
        self.theme = name
        p = PALETTES[name]
        style = self.style
        style.theme_use("clam")  # als einziges Windows-Theme vollstaendig einfaerbbar
        style.configure(".", background=p["bg"], foreground=p["fg"], fieldbackground=p["field"], bordercolor=p["border"], lightcolor=p["border"], darkcolor=p["border"],
                        troughcolor=p["panel"], insertcolor=p["fg"], selectbackground=p["select"], selectforeground="#ffffff", focuscolor=p["accent"], font=("Segoe UI", 9))
        style.configure("TFrame", background=p["bg"])
        style.configure("TLabel", background=p["bg"], foreground=p["fg"])
        style.configure("TLabelframe", background=p["bg"], bordercolor=p["border"])
        style.configure("TLabelframe.Label", background=p["bg"], foreground=p["accent"], font=("Segoe UI", 9, "bold"))
        style.configure("Title.TLabel", font=("Segoe UI", 18, "bold"), foreground=p["fg"])
        style.configure("Sub.TLabel", foreground=p["muted"])
        style.configure("Ok.TLabel", foreground=p["ok"])
        style.configure("Bad.TLabel", foreground=p["bad"])
        style.configure("TButton", background=p["button"], foreground=p["fg"], bordercolor=p["border"], padding=(10, 4), relief="flat")
        style.map("TButton", background=[("disabled", p["panel"]), ("pressed", p["select"]), ("active", p["button_hover"])], foreground=[("disabled", p["disabled"])], bordercolor=[("focus", p["accent"])])
        style.configure("Big.TButton", padding=(14, 8), font=("Segoe UI", 10, "bold"), background=p["accent"] if name == "dark" else p["button"], foreground="#ffffff" if name == "dark" else p["fg"])
        style.map("Big.TButton", background=[("disabled", p["panel"]), ("pressed", p["select"]), ("active", "#6aa0ff" if name == "dark" else p["button_hover"])], foreground=[("disabled", p["disabled"])])
        for widget in ("TCheckbutton", "TRadiobutton"):
            style.configure(widget, background=p["bg"], foreground=p["fg"], indicatorbackground=p["field"], indicatorforeground=p["fg"], indicatorcolor=p["field"])
            style.map(widget, background=[("active", p["bg"])], indicatorcolor=[("selected", p["accent"]), ("pressed", p["select"])], foreground=[("disabled", p["disabled"])])
        for widget in ("TEntry", "TSpinbox"):
            style.configure(widget, fieldbackground=p["field"], foreground=p["fg"], insertcolor=p["fg"], bordercolor=p["border"], arrowcolor=p["fg"], background=p["button"])
            style.map(widget, fieldbackground=[("disabled", p["panel"]), ("readonly", p["field"])], bordercolor=[("focus", p["accent"])], foreground=[("disabled", p["disabled"])])
        style.configure("TCombobox", fieldbackground=p["field"], foreground=p["fg"], background=p["button"], arrowcolor=p["fg"], bordercolor=p["border"], selectbackground=p["field"], selectforeground=p["fg"])
        style.map("TCombobox", fieldbackground=[("readonly", p["field"])], selectbackground=[("readonly", p["field"])], selectforeground=[("readonly", p["fg"])], bordercolor=[("focus", p["accent"])])
        self.option_add("*TCombobox*Listbox.background", p["field"])
        self.option_add("*TCombobox*Listbox.foreground", p["fg"])
        self.option_add("*TCombobox*Listbox.selectBackground", p["select"])
        self.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        style.configure("TNotebook", background=p["bg"], bordercolor=p["border"], tabmargins=(2, 4, 2, 0))
        style.configure("TNotebook.Tab", background=p["panel"], foreground=p["muted"], padding=(14, 6), bordercolor=p["border"])
        style.map("TNotebook.Tab", background=[("selected", p["bg"]), ("active", p["button_hover"])], foreground=[("selected", p["fg"])], expand=[("selected", (0, 0, 0, 2))])
        style.configure("Treeview", background=p["field"], fieldbackground=p["field"], foreground=p["fg"], bordercolor=p["border"], rowheight=24)
        style.map("Treeview", background=[("selected", p["select"])], foreground=[("selected", "#ffffff")])
        style.configure("Treeview.Heading", background=p["button"], foreground=p["fg"], bordercolor=p["border"], relief="flat", padding=4)
        style.map("Treeview.Heading", background=[("active", p["button_hover"])])
        style.configure("TScrollbar", background=p["button"], troughcolor=p["panel"], bordercolor=p["border"], arrowcolor=p["fg"])
        style.map("TScrollbar", background=[("active", p["button_hover"])])
        style.configure("TProgressbar", background=p["accent"], troughcolor=p["panel"], bordercolor=p["border"])
        style.configure("TSeparator", background=p["border"])
        self.configure(background=p["bg"])
        self._restyle_tk(self)
        self._dark_titlebar(name == "dark")
        if hasattr(self, "btn_theme"):
            self.btn_theme.configure(text="Hell" if name == "dark" else "Dunkel")

    def _restyle_tk(self, widget: tk.Misc) -> None:
        """Klassische tk-Widgets (Text, Canvas, Listbox) folgen nicht den ttk-Stilen und werden einzeln eingefaerbt."""
        p = PALETTES[self.theme]
        for child in widget.winfo_children():
            if isinstance(child, tk.Text):
                child.configure(background=p["field"], foreground=p["fg"], insertbackground=p["fg"], selectbackground=p["select"], selectforeground="#ffffff", highlightbackground=p["border"], highlightcolor=p["accent"], relief="flat", borderwidth=1)
            elif isinstance(child, tk.Canvas):
                child.configure(background=p["bg"])
            elif isinstance(child, tk.Listbox):
                child.configure(background=p["field"], foreground=p["fg"], selectbackground=p["select"], selectforeground="#ffffff")
            self._restyle_tk(child)

    def _dark_titlebar(self, dark: bool) -> None:
        """Dunkle Windows-Titelleiste (DWMWA_USE_IMMERSIVE_DARK_MODE), sofern das System sie unterstuetzt."""
        try:
            self.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id()) or self.winfo_id()
            value = ctypes.c_int(1 if dark else 0)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(value), ctypes.sizeof(value))
        except (AttributeError, OSError, tk.TclError):
            pass

    def toggle_theme(self) -> None:
        """Wechselt zwischen dunkel und hell und merkt sich die Wahl in der Konfiguration (falls eine vorhanden ist)."""
        new = "light" if self.theme == "dark" else "dark"
        self.apply_theme(new)
        self.config_data.setdefault("gui", {})["theme"] = new
        message = "Darstellung: " + ("dunkel" if new == "dark" else "hell")
        try:
            if self.config_path.is_file():
                data = copy.deepcopy(self.config.data) if self.config else copy.deepcopy(self.config_data)
                data.setdefault("gui", {})["theme"] = new
                self.config = cfgmod.save(self.config_path, data)
        except (cfgmod.ConfigError, OSError) as error:
            # Die Darstellung wird trotzdem gewechselt; gespeichert wird beim naechsten Speichern der Einstellungen.
            message += f" (nicht gespeichert: {error})"
        self.set_status(message)
    def _build_ui(self) -> None:
        header = ttk.Frame(self, padding=(16, 12, 16, 4))
        header.pack(fill="x")
        ttk.Label(header, text="SuperPyBackup", style="Title.TLabel").pack(side="left")
        self.header_status = ttk.Label(header, text="", style="Sub.TLabel")
        self.header_status.pack(side="right")
        self.btn_theme = ttk.Button(header, text="Hell", width=7, command=self.toggle_theme)
        self.btn_theme.pack(side="right", padx=(0, 14))
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=12, pady=6)
        self.tab_backup = ttk.Frame(self.notebook, padding=12)
        self.tab_settings = ttk.Frame(self.notebook)
        self.tab_restore = ttk.Frame(self.notebook, padding=12)
        self.tab_task = ttk.Frame(self.notebook, padding=12)
        self.tab_update = ttk.Frame(self.notebook, padding=12)
        self.tab_log = ttk.Frame(self.notebook, padding=12)
        self.tab_faq = ttk.Frame(self.notebook, padding=12)
        for tab, name in ((self.tab_backup, "Sicherung"), (self.tab_settings, "Einstellungen"), (self.tab_restore, "Wiederherstellung"), (self.tab_task, "Aufgabe"), (self.tab_update, "Update"), (self.tab_log, "Protokoll"), (self.tab_faq, "FAQ")):
            self.notebook.add(tab, text=name)
        self._build_backup_tab()
        self._build_settings_tab()
        self._build_restore_tab()
        self._build_task_tab()
        self._build_update_tab()
        self._build_log_tab()
        self._build_faq_tab()
        self.status = tk.StringVar(value="Bereit.")
        ttk.Label(self, textvariable=self.status, relief="sunken", anchor="w", padding=(8, 3)).pack(fill="x", side="bottom")

    def _text_area(self, parent: tk.Misc, height: int = 10) -> tk.Text:
        frame = ttk.Frame(parent)
        text = tk.Text(frame, height=height, wrap="none", font=("Consolas", 9), state="disabled")
        vertical = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=text.xview)
        text.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        text.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        text.container = frame  # type: ignore[attr-defined]
        return text

    def _append(self, widget: tk.Text, text: str, *, clear: bool = False) -> None:
        widget.configure(state="normal")
        if clear:
            widget.delete("1.0", "end")
        widget.insert("end", text)
        widget.see("end")
        widget.configure(state="disabled")

    def _build_backup_tab(self) -> None:
        tab = self.tab_backup
        summary = ttk.LabelFrame(tab, text="Übersicht", padding=10)
        summary.pack(fill="x")
        summary.columnconfigure(1, weight=1)
        self.summary_vars = {key: tk.StringVar(value="-") for key in ("target", "sources", "last", "task", "update")}
        for row, (key, label) in enumerate((("target", "Sicherungsziel"), ("sources", "Quellen"), ("last", "Letzter Stand"), ("task", "Windows-Aufgabe"), ("update", "Programm"))):
            ttk.Label(summary, text=label + ":").grid(row=row, column=0, sticky="nw", padx=(0, 10), pady=2)
            ttk.Label(summary, textvariable=self.summary_vars[key], wraplength=700, justify="left").grid(row=row, column=1, sticky="w", pady=2)
        buttons = ttk.Frame(tab)
        buttons.pack(fill="x", pady=10)
        self.btn_backup = ttk.Button(buttons, text="Jetzt sichern", style="Big.TButton", command=lambda: self.start_backup(False))
        self.btn_backup.pack(side="left")
        self.btn_backup_new = ttk.Button(buttons, text="Weiteren Stand anlegen", command=lambda: self.start_backup(True))
        self.btn_backup_new.pack(side="left", padx=8)
        self.btn_backup_full = ttk.Button(buttons, text="Vollsicherung", command=lambda: self.start_backup(True, engine.FULL))
        self.btn_backup_full.pack(side="left")
        self.btn_cancel = ttk.Button(buttons, text="Abbrechen", command=self.cancel_backup, state="disabled")
        self.btn_cancel.pack(side="left")
        self.btn_cleanup = ttk.Button(buttons, text="Bereinigen …", command=self.start_cleanup)
        self.btn_cleanup.pack(side="right")
        self.progress = ttk.Progressbar(tab, mode="determinate", maximum=100)
        self.progress.pack(fill="x")
        self.progress_label = ttk.Label(tab, text="", style="Sub.TLabel")
        self.progress_label.pack(fill="x", pady=(2, 6))
        self.result_label = ttk.Label(tab, text="")
        self.result_label.pack(fill="x")
        self.backup_log = self._text_area(tab, 12)
        self.backup_log.container.pack(fill="both", expand=True, pady=(6, 0))  # type: ignore[attr-defined]

    def _build_settings_tab(self) -> None:
        scroll = ScrollFrame(self.tab_settings)
        scroll.pack(fill="both", expand=True)
        body = scroll.body
        body.columnconfigure(0, weight=1)
        pad = {"padx": 12, "pady": 6}
        # Ziel
        box = ttk.LabelFrame(body, text="Sicherungsziel (lokales Laufwerk, USB oder \\\\Server\\Freigabe)", padding=10)
        box.grid(row=0, column=0, sticky="ew", **pad)
        box.columnconfigure(0, weight=1)
        self.var_target = tk.StringVar()
        ttk.Entry(box, textvariable=self.var_target).grid(row=0, column=0, sticky="ew")
        ttk.Button(box, text="Auswählen ...", command=lambda: self._browse_into(self.var_target, "Sicherungsziel wählen")).grid(row=0, column=1, padx=(8, 0))
        # Quellen
        box = ttk.LabelFrame(body, text="Quellen", padding=10)
        box.grid(row=1, column=0, sticky="ew", **pad)
        box.columnconfigure(0, weight=1)
        self.var_use_known = tk.BooleanVar(value=True)
        ttk.Checkbutton(box, text="Persönliche Windows-Ordner automatisch ermitteln (inkl. Umleitung/OneDrive)", variable=self.var_use_known).grid(row=0, column=0, columnspan=2, sticky="w")
        self.var_known = {name: tk.BooleanVar(value=True) for name in KNOWN_LABELS}
        known_row = ttk.Frame(box)
        known_row.grid(row=1, column=0, columnspan=2, sticky="w", pady=4)
        for name, label in KNOWN_LABELS.items():
            ttk.Checkbutton(known_row, text=label, variable=self.var_known[name]).pack(side="left", padx=(0, 12))
        ttk.Label(box, text="Zusätzliche Ordner:").grid(row=2, column=0, sticky="w", pady=(6, 2))
        self.extra_tree = ttk.Treeview(box, columns=("name", "path"), show="headings", height=4, selectmode="browse")
        self.extra_tree.heading("name", text="Name")
        self.extra_tree.heading("path", text="Ordner")
        self.extra_tree.column("name", width=140, stretch=False)
        self.extra_tree.column("path", width=520)
        self.extra_tree.grid(row=3, column=0, sticky="ew")
        extra_buttons = ttk.Frame(box)
        extra_buttons.grid(row=3, column=1, sticky="n", padx=(8, 0))
        ttk.Button(extra_buttons, text="Hinzufügen ...", command=self.add_extra_source).pack(fill="x")
        ttk.Button(extra_buttons, text="Entfernen", command=self.remove_extra_source).pack(fill="x", pady=(4, 0))
        # Ausschlüsse
        box = ttk.LabelFrame(body, text="Ausschlüsse (ein Muster pro Zeile, z. B. *.iso oder node_modules)", padding=10)
        box.grid(row=2, column=0, sticky="ew", **pad)
        box.columnconfigure(0, weight=1)
        self.text_exclude = tk.Text(box, height=4, font=("Consolas", 9))
        self.text_exclude.grid(row=0, column=0, sticky="ew")
        self.var_default_excludes = tk.BooleanVar(value=True)
        ttk.Checkbutton(box, text="Standard-Ausschlüsse verwenden (*.tmp, *.temp, desktop.ini, Thumbs.db, ~$*, Papierkorb)", variable=self.var_default_excludes).grid(row=1, column=0, sticky="w", pady=(4, 0))
        # Aufbewahrung
        box = ttk.LabelFrame(body, text="Aufbewahrung", padding=10)
        box.grid(row=3, column=0, sticky="ew", **pad)
        self.var_daily, self.var_monthly, self.var_yearly = tk.StringVar(value="7"), tk.StringVar(value="12"), tk.StringVar(value="5")
        for column, (label, var, upper) in enumerate((("Tage", self.var_daily, 36500), ("Monate", self.var_monthly, 1200), ("Jahre", self.var_yearly, 100))):
            ttk.Label(box, text=label + ":").grid(row=0, column=column * 2, sticky="e", padx=(0 if column == 0 else 18, 4))
            ttk.Spinbox(box, from_=1, to=upper, width=7, textvariable=var).grid(row=0, column=column * 2 + 1)
        # Verhalten
        box = ttk.LabelFrame(body, text="Verhalten", padding=10)
        box.grid(row=4, column=0, sticky="ew", **pad)
        box.columnconfigure(1, weight=1)
        self.var_existing = tk.StringVar()
        self.var_change = tk.StringVar()
        ttk.Label(box, text="Heute schon gesichert:").grid(row=0, column=0, sticky="w")
        ttk.Combobox(box, textvariable=self.var_existing, values=list(EXISTING_LABELS.values()), state="readonly", width=34).grid(row=0, column=1, sticky="w", padx=8, pady=2)
        ttk.Label(box, text="Änderungserkennung:").grid(row=1, column=0, sticky="w")
        ttk.Combobox(box, textvariable=self.var_change, values=list(CHANGE_LABELS.values()), state="readonly", width=34).grid(row=1, column=1, sticky="w", padx=8, pady=2)
        self.var_hardlinks = tk.BooleanVar(value=True)
        ttk.Checkbutton(box, text="Unveränderte Dateien per NTFS-Hardlink wiederverwenden (spart Speicher)", variable=self.var_hardlinks).grid(row=2, column=0, columnspan=2, sticky="w", pady=2)
        self.var_full_days = tk.StringVar(value="7")
        full_row = ttk.Frame(box)
        full_row.grid(row=3, column=0, columnspan=2, sticky="w", pady=2)
        ttk.Label(full_row, text="Vollsicherung automatisch alle").pack(side="left")
        ttk.Spinbox(full_row, from_=0, to=3650, width=6, textvariable=self.var_full_days).pack(side="left", padx=6)
        ttk.Label(full_row, text="Tage (0 = nur Inkremente; dazwischen wird nur Geändertes kopiert)").pack(side="left")
        # Protokoll + Update
        box = ttk.LabelFrame(body, text="Protokoll und Updates", padding=10)
        box.grid(row=5, column=0, sticky="ew", **pad)
        box.columnconfigure(1, weight=1)
        self.var_log_dir, self.var_log_days = tk.StringVar(), tk.StringVar(value="30")
        ttk.Label(box, text="Protokollordner (leer = Standard):").grid(row=0, column=0, sticky="w")
        ttk.Entry(box, textvariable=self.var_log_dir).grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Button(box, text="Auswählen ...", command=lambda: self._browse_into(self.var_log_dir, "Protokollordner wählen")).grid(row=0, column=2)
        ttk.Label(box, text="Protokolle behalten (Tage):").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Spinbox(box, from_=1, to=3650, width=7, textvariable=self.var_log_days).grid(row=1, column=1, sticky="w", padx=8)
        self.var_check_updates = tk.BooleanVar(value=True)
        ttk.Checkbutton(box, text="Regelmäßig auf neue Versionen prüfen (lädt nie ohne Bestätigung etwas herunter)", variable=self.var_check_updates).grid(row=2, column=0, columnspan=3, sticky="w")
        # Desktop-Meldungen
        box = ttk.LabelFrame(body, text="Desktop-Meldungen", padding=10)
        box.grid(row=6, column=0, sticky="ew", **pad)
        self.var_notify = tk.BooleanVar(value=False)
        self.var_notify_success, self.var_notify_update, self.var_notify_error = tk.BooleanVar(value=True), tk.BooleanVar(value=True), tk.BooleanVar(value=True)
        ttk.Checkbutton(box, text="Desktop-Meldungen einschalten (gilt auch für die geplante Aufgabe)", variable=self.var_notify).grid(row=0, column=0, columnspan=4, sticky="w")
        ttk.Checkbutton(box, text="Sicherung abgeschlossen", variable=self.var_notify_success).grid(row=1, column=0, sticky="w", padx=(18, 8))
        ttk.Checkbutton(box, text="Neue Version / Update", variable=self.var_notify_update).grid(row=1, column=1, sticky="w", padx=8)
        ttk.Checkbutton(box, text="Fehler", variable=self.var_notify_error).grid(row=1, column=2, sticky="w", padx=8)
        self.btn_notify_test = ttk.Button(box, text="Testmeldung", command=self.test_notification)
        self.btn_notify_test.grid(row=1, column=3, sticky="e", padx=8)
        # Schaltflächen
        actions = ttk.Frame(body)
        actions.grid(row=7, column=0, sticky="ew", padx=12, pady=(8, 14))
        self.btn_save = ttk.Button(actions, text="Speichern", style="Big.TButton", command=self.save_settings)
        self.btn_save.pack(side="left")
        ttk.Button(actions, text="Zurücksetzen (aus Datei laden)", command=self.load_config).pack(side="left", padx=8)
        self.settings_path_label = ttk.Label(actions, text="", style="Sub.TLabel")
        self.settings_path_label.pack(side="left", padx=8)
        self.settings_message = ttk.Label(body, text="", wraplength=860, justify="left")
        self.settings_message.grid(row=8, column=0, sticky="w", padx=12, pady=(0, 12))

    def _build_restore_tab(self) -> None:
        tab = self.tab_restore
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)
        tab.rowconfigure(3, weight=2)
        top = ttk.Frame(tab)
        top.grid(row=0, column=0, sticky="ew")
        ttk.Label(top, text="Sicherungsstände").pack(side="left")
        ttk.Button(top, text="Aktualisieren", command=self.refresh_snapshots).pack(side="left", padx=8)
        self.btn_verify = ttk.Button(top, text="Ausgewählten Stand prüfen", command=self.verify_selected)
        self.btn_verify.pack(side="left")
        self.snap_tree = ttk.Treeview(tab, columns=("date", "status", "files", "size"), show="tree headings", height=6, selectmode="browse")
        for column, title, width in (("#0", "Stand", 200), ("date", "Datum", 100), ("status", "Status", 90), ("files", "Dateien", 80), ("size", "Größe", 100)):
            self.snap_tree.heading(column, text=title)
            self.snap_tree.column(column, width=width, stretch=column == "#0")
        self.snap_tree.grid(row=1, column=0, sticky="nsew", pady=(4, 8))
        self.snap_tree.bind("<<TreeviewSelect>>", lambda event: self.load_snapshot_files())
        ttk.Label(tab, text="Inhalt des Standes (Auswahl = nur diese Dateien/Ordner; ohne Auswahl wird alles wiederhergestellt)").grid(row=2, column=0, sticky="w")
        self.file_tree = ttk.Treeview(tab, columns=("size",), show="tree headings", selectmode="extended")
        self.file_tree.heading("#0", text="Name")
        self.file_tree.heading("size", text="Größe")
        self.file_tree.column("#0", width=520)
        self.file_tree.column("size", width=100, anchor="e")
        self.file_tree.grid(row=3, column=0, sticky="nsew", pady=(4, 8))
        self.file_tree.bind("<<TreeviewOpen>>", self._open_node)
        options = ttk.Frame(tab)
        options.grid(row=4, column=0, sticky="ew")
        options.columnconfigure(2, weight=1)
        self.var_restore_mode = tk.StringVar(value="dest")
        ttk.Radiobutton(options, text="In Ordner:", variable=self.var_restore_mode, value="dest").grid(row=0, column=0, sticky="w")
        self.var_restore_dest = tk.StringVar()
        ttk.Entry(options, textvariable=self.var_restore_dest).grid(row=0, column=2, sticky="ew", padx=6)
        ttk.Button(options, text="Auswählen ...", command=lambda: self._browse_into(self.var_restore_dest, "Wiederherstellungsziel wählen")).grid(row=0, column=3)
        ttk.Radiobutton(options, text="Am ursprünglichen Ort", variable=self.var_restore_mode, value="original").grid(row=1, column=0, columnspan=2, sticky="w")
        self.var_overwrite = tk.BooleanVar(value=False)
        ttk.Checkbutton(options, text="Vorhandene Dateien ersetzen (wird vor dem Start extra bestätigt)", variable=self.var_overwrite).grid(row=1, column=2, columnspan=2, sticky="w", padx=6)
        self.btn_restore = ttk.Button(tab, text="Wiederherstellen", style="Big.TButton", command=self.start_restore)
        self.btn_restore.grid(row=5, column=0, sticky="w", pady=(8, 4))
        self.restore_log = self._text_area(tab, 5)
        self.restore_log.container.grid(row=6, column=0, sticky="nsew")  # type: ignore[attr-defined]
        tab.rowconfigure(6, weight=1)

    def _build_task_tab(self) -> None:
        tab = self.tab_task
        ttk.Label(tab, text="Automatische Sicherung über die Windows-Aufgabenplanung", style="Sub.TLabel").pack(anchor="w")
        form = ttk.Frame(tab)
        form.pack(fill="x", pady=10)
        self.var_delay = tk.StringVar(value="2")
        self.var_daily_on = tk.BooleanVar(value=False)
        self.var_daily_at = tk.StringVar(value="18:00")
        ttk.Label(form, text="Start nach der Anmeldung (Minuten):").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(form, from_=0, to=60, width=6, textvariable=self.var_delay).grid(row=0, column=1, padx=8, sticky="w")
        ttk.Checkbutton(form, text="Zusätzlich täglich um (HH:MM):", variable=self.var_daily_on).grid(row=1, column=0, sticky="w", pady=6)
        ttk.Entry(form, textvariable=self.var_daily_at, width=8).grid(row=1, column=1, padx=8, sticky="w")
        buttons = ttk.Frame(tab)
        buttons.pack(fill="x")
        self.btn_task_install = ttk.Button(buttons, text="Aufgabe einrichten / ändern", style="Big.TButton", command=self.install_task)
        self.btn_task_install.pack(side="left")
        ttk.Button(buttons, text="Prüfen", command=self.check_task).pack(side="left", padx=8)
        ttk.Button(buttons, text="Status aktualisieren", command=self.refresh_task).pack(side="left")
        ttk.Button(buttons, text="Aufgabe entfernen", command=self.remove_task).pack(side="left", padx=8)
        ttk.Label(tab, text="Die Aufgabe läuft im Benutzerkontext ohne Administratorrechte, ohne sichtbares Fenster, nie parallel, und sichert höchstens einmal pro Tag.", style="Sub.TLabel", wraplength=860).pack(anchor="w", pady=8)
        box = ttk.LabelFrame(tab, text="Updateprüfung (Meldung bei neuer Version)", padding=8)
        box.pack(fill="x", pady=(0, 8))
        self.var_upd_delay = tk.StringVar(value="5")
        self.var_upd_daily_on = tk.BooleanVar(value=True)
        self.var_upd_daily_at = tk.StringVar(value="12:00")
        ttk.Label(box, text="Start nach der Anmeldung (Minuten):").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(box, from_=0, to=60, width=6, textvariable=self.var_upd_delay).grid(row=0, column=1, padx=8, sticky="w")
        ttk.Checkbutton(box, text="Zusätzlich täglich um (HH:MM):", variable=self.var_upd_daily_on).grid(row=0, column=2, padx=(18, 0), sticky="w")
        ttk.Entry(box, textvariable=self.var_upd_daily_at, width=8).grid(row=0, column=3, padx=8, sticky="w")
        self.btn_update_task_install = ttk.Button(box, text="Prüfaufgabe einrichten / ändern", command=self.install_update_task)
        self.btn_update_task_install.grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Button(box, text="Status", command=self.refresh_update_task).grid(row=1, column=2, sticky="w", padx=(18, 0), pady=(8, 0))
        ttk.Button(box, text="Entfernen", command=self.remove_update_task).grid(row=1, column=3, sticky="w", padx=8, pady=(8, 0))
        ttk.Label(box, text="Die Aufgabe prüft still auf neue Versionen und meldet sie einmal je Version per Desktop-Meldung (unter Einstellungen einschalten). Es wird nie etwas installiert.", style="Sub.TLabel", wraplength=860).grid(row=2, column=0, columnspan=4, sticky="w", pady=(6, 0))
        self.task_text = self._text_area(tab, 12)
        self.task_text.container.pack(fill="both", expand=True)  # type: ignore[attr-defined]

    def _build_update_tab(self) -> None:
        tab = self.tab_update
        ttk.Label(tab, text=f"Installierte Version: {__version__}", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        buttons = ttk.Frame(tab)
        buttons.pack(fill="x", pady=10)
        self.btn_update_check = ttk.Button(buttons, text="Auf Updates prüfen", style="Big.TButton", command=self.check_update)
        self.btn_update_check.pack(side="left")
        self.btn_update_install = ttk.Button(buttons, text="Update herunterladen und installieren ...", command=self.install_update, state="disabled")
        self.btn_update_install.pack(side="left", padx=8)
        ttk.Button(buttons, text="Vorversion wiederherstellen", command=self.rollback_update).pack(side="left")
        self.update_label = ttk.Label(tab, text="", wraplength=860, justify="left")
        self.update_label.pack(anchor="w", pady=6)
        ttk.Label(tab, text="Es wird nichts ohne ausdrückliche Bestätigung heruntergeladen. Heruntergeladene Dateien werden vor der Installation per SHA-256 geprüft; bei jedem Fehler bleibt die bestehende Installation unverändert.", style="Sub.TLabel", wraplength=860).pack(anchor="w")

    def _build_log_tab(self) -> None:
        tab = self.tab_log
        top = ttk.Frame(tab)
        top.pack(fill="x")
        ttk.Label(top, text="Datei:").pack(side="left")
        self.var_log_file = tk.StringVar()
        self.log_combo = ttk.Combobox(top, textvariable=self.var_log_file, state="readonly", width=40)
        self.log_combo.pack(side="left", padx=6)
        self.log_combo.bind("<<ComboboxSelected>>", lambda event: self.show_log())
        ttk.Button(top, text="Aktualisieren", command=self.refresh_logs).pack(side="left")
        ttk.Button(top, text="Ordner öffnen", command=self.open_log_folder).pack(side="left", padx=6)
        self.log_view = self._text_area(tab, 20)
        self.log_view.container.pack(fill="both", expand=True, pady=(8, 0))  # type: ignore[attr-defined]
        self.notebook.bind("<<NotebookTabChanged>>", self._tab_changed)

    def _build_faq_tab(self) -> None:
        tab = self.tab_faq
        ttk.Label(tab, text=f"Alle Befehle von {faq.EXE}. Mit „Kopieren“ landet der Befehl in der Zwischenablage; Pfade und Daten bitte anpassen.", style="Sub.TLabel").pack(anchor="w")
        outer = ttk.Frame(tab)
        outer.pack(fill="both", expand=True, pady=(8, 0))
        canvas = tk.Canvas(outer, highlightthickness=0)
        scroll = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        body = ttk.Frame(canvas)
        window = canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        canvas.bind("<Enter>", lambda event: canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta / 120), "units")))
        canvas.bind("<Leave>", lambda event: canvas.unbind_all("<MouseWheel>"))
        self.faq_canvas = canvas
        self.faq_buttons: dict[str, ttk.Button] = {}
        body.columnconfigure(0, weight=1)
        row = 0
        for section, items in faq.SECTIONS:
            ttk.Label(body, text=section, style="Title.TLabel").grid(row=row, column=0, columnspan=2, sticky="w", pady=(14 if row else 0, 4))
            row += 1
            for title, text, arguments in items:
                command = faq.full_command(arguments)
                ttk.Label(body, text=title, font=("Segoe UI", 9, "bold")).grid(row=row, column=0, columnspan=2, sticky="w", pady=(6, 0))
                ttk.Label(body, text=text, style="Sub.TLabel", wraplength=760, justify="left").grid(row=row + 1, column=0, columnspan=2, sticky="w")
                entry = ttk.Entry(body, font=("Consolas", 9))
                entry.insert(0, command)
                entry.configure(state="readonly")
                entry.grid(row=row + 2, column=0, sticky="ew", pady=(2, 0), padx=(0, 8))
                button = ttk.Button(body, text="Kopieren", width=10, command=lambda c=command: self.copy_command(c))
                button.grid(row=row + 2, column=1, pady=(2, 0), padx=(0, 6))
                self.faq_buttons[command] = button
                row += 3

    def copy_command(self, command: str) -> None:
        """Legt den Befehl in die Zwischenablage und bestaetigt es kurz am Knopf und in der Statusleiste."""
        try:
            self.clipboard_clear()
            self.clipboard_append(command)
            self.update()  # die Zwischenablage bleibt auch nach dem Schliessen des Fensters erhalten
        except tk.TclError as error:
            self.set_status(f"Kopieren nicht moeglich: {error}")
            return
        self.set_status(f"In die Zwischenablage kopiert: {command}")
        button = self.faq_buttons.get(command)
        if button is not None:
            button.configure(text="Kopiert ✓")
            self.after(1500, lambda: button.winfo_exists() and button.configure(text="Kopieren"))

    # ------------------------------------------------------------------ Hilfen
    def _browse_into(self, variable: tk.StringVar, title: str) -> None:
        chosen = self.ask_folder(title)
        if chosen:
            variable.set(os.path.normpath(chosen))

    def set_status(self, text: str) -> None:
        self.status.set(text)

    def run_async(self, work: Callable[[], Any], done: Callable[[Any, BaseException | None], None], *, op: bool = False) -> None:
        self._busy += 1
        self._ops += 1 if op else 0

        def target() -> None:
            result, error = None, None
            try:
                result = work()
            except BaseException as caught:  # noqa: BLE001 - jeder Fehler wird an die Oberfläche gemeldet
                error = caught
            self.messages.put(("done", done, result, error, op))

        threading.Thread(target=target, daemon=True).start()

    def _poll(self) -> None:
        try:
            while True:
                message = self.messages.get_nowait()
                if message[0] == "log":
                    self._append(self.backup_log, message[1])
                elif message[0] == "restore-log":
                    self._append(self.restore_log, message[1])
                elif message[0] == "done":
                    self._busy -= 1
                    self._ops -= 1 if message[4] else 0
                    try:
                        message[1](message[2], message[3])
                    except Exception as error:  # noqa: BLE001 - Fehler im Rueckruf nie die Oberfläche beenden lassen
                        self.error("Interner Fehler", str(error))
        except queue.Empty:
            pass
        progress = self._progress
        if progress and self._backup_running:
            phase, done, total, text = progress
            if phase == "scan" or total == 0:
                self.progress.configure(mode="indeterminate")
                self.progress.step(4)
                self.progress_label.configure(text=text)
            else:
                self.progress.configure(mode="determinate", maximum=total, value=done)
                self.progress_label.configure(text=f"{done} von {total} Dateien: {text}")
        if not self._closing:
            self.after(100, self._poll)

    def wait_idle(self, timeout: float = 60.0) -> None:
        """Wartet (mit laufender Ereignisschleife), bis alle Hintergrundvorgänge beendet sind. Für Tests."""
        deadline = time.time() + timeout
        while (self._busy > 0 or not self.messages.empty()) and time.time() < deadline:
            self.update()
            time.sleep(0.02)
        self.update()
        if self._busy > 0:
            raise TimeoutError("Hintergrundvorgang nicht beendet")

    def _tab_changed(self, event: object) -> None:
        current = self.notebook.select()
        if current == str(self.tab_restore) and not self.snapshot_refs:
            self.refresh_snapshots()
        elif current == str(self.tab_task):
            self.refresh_task()
        elif current == str(self.tab_log):
            self.refresh_logs()

    def _set_busy_state(self, running: bool) -> None:
        state = "disabled" if running else "normal"
        for button in (self.btn_backup, self.btn_backup_new, self.btn_backup_full, self.btn_restore, self.btn_save, self.btn_verify, self.btn_cleanup):
            button.configure(state=state)
        self.btn_cancel.configure(state="normal" if running and self._backup_running else "disabled")

    # ------------------------------------------------------------------ Konfiguration
    def load_config(self) -> None:
        self.settings_path_label.configure(text=str(self.config_path))
        try:
            if self.config_path.is_file():
                self.config = cfgmod.load(self.config_path, require_target=False)
                self.config_data = copy.deepcopy(self.config.data)
                self.settings_message.configure(text="Einstellungen geladen.", style="Ok.TLabel")
            else:
                self.config = None
                self.config_data = cfgmod.template("")
                self.settings_message.configure(text="Noch keine Konfiguration: Bitte ein Sicherungsziel eintragen und speichern.", style="TLabel")
        except cfgmod.ConfigError as error:
            self.config = None
            self.config_data = cfgmod.template("")
            self.settings_message.configure(text=str(error), style="Bad.TLabel")
        self._fill_settings(self.config_data)
        configured = self.config_data.get("gui", {}).get("theme", "dark")
        if configured != self.theme:
            self.apply_theme(configured)
        self.refresh_summary()

    def _fill_settings(self, data: dict[str, Any]) -> None:
        self.var_target.set(data["backup_root"])
        self.var_use_known.set(data["use_known_folders"])
        for name, variable in self.var_known.items():
            variable.set(name in data["known_folders"])
        self.extra_tree.delete(*self.extra_tree.get_children())
        for index, item in enumerate(data["extra_sources"]):
            item = {"path": item} if isinstance(item, str) else item
            self.extra_tree.insert("", "end", iid=f"x{index}", values=(item.get("name") or os.path.basename(str(item.get("path", "")).rstrip("\\/")), item.get("path", "")))
        self.text_exclude.delete("1.0", "end")
        self.text_exclude.insert("1.0", "\n".join(data["exclude"]))
        self.var_default_excludes.set(data["use_default_excludes"])
        self.var_daily.set(str(data["retention"]["daily"]))
        self.var_monthly.set(str(data["retention"]["monthly"]))
        self.var_yearly.set(str(data["retention"]["yearly"]))
        self.var_existing.set(EXISTING_LABELS[data["backup"]["existing_daily"]])
        self.var_change.set(CHANGE_LABELS[data["backup"]["change_detection"]])
        self.var_hardlinks.set(data["backup"]["use_hardlinks"])
        self.var_full_days.set(str(data["backup"]["full_every_days"]))
        self.var_log_dir.set(data["logging"]["directory"])
        self.var_log_days.set(str(data["logging"]["keep_days"]))
        self.var_check_updates.set(data["update"]["check_for_updates"])
        self.var_notify.set(data["notifications"]["enabled"])
        self.var_notify_success.set(data["notifications"]["on_success"])
        self.var_notify_update.set(data["notifications"]["on_update"])
        self.var_notify_error.set(data["notifications"]["on_error"])

    def collect_settings(self) -> dict[str, Any]:
        data = copy.deepcopy(self.config_data)

        def number(variable: tk.StringVar, label: str) -> int:
            try:
                return int(variable.get().strip())
            except ValueError as error:
                raise cfgmod.ConfigError(f"Ungültige Konfiguration:\n  - {label} muss eine ganze Zahl sein (eingegeben: {variable.get()!r}).") from error

        data["backup_root"] = self.var_target.get().strip()
        data["use_known_folders"] = self.var_use_known.get()
        data["known_folders"] = [name for name in KNOWN_LABELS if self.var_known[name].get()]
        data["extra_sources"] = [{"name": self.extra_tree.set(iid, "name"), "path": self.extra_tree.set(iid, "path")} for iid in self.extra_tree.get_children()]
        data["exclude"] = [line.strip() for line in self.text_exclude.get("1.0", "end").splitlines() if line.strip()]
        data["use_default_excludes"] = self.var_default_excludes.get()
        data["retention"] = {"daily": number(self.var_daily, "Aufbewahrung Tage"), "monthly": number(self.var_monthly, "Aufbewahrung Monate"), "yearly": number(self.var_yearly, "Aufbewahrung Jahre")}
        data["backup"]["existing_daily"] = next(key for key, label in EXISTING_LABELS.items() if label == self.var_existing.get())
        data["backup"]["change_detection"] = next(key for key, label in CHANGE_LABELS.items() if label == self.var_change.get())
        data["backup"]["use_hardlinks"] = self.var_hardlinks.get()
        data["backup"]["full_every_days"] = number(self.var_full_days, "Vollsicherung alle (Tage)")
        data["logging"]["directory"] = self.var_log_dir.get().strip()
        data["logging"]["keep_days"] = number(self.var_log_days, "Protokolle behalten (Tage)")
        data["update"]["check_for_updates"] = self.var_check_updates.get()
        data["notifications"] = {"enabled": self.var_notify.get(), "on_success": self.var_notify_success.get(), "on_update": self.var_notify_update.get(), "on_error": self.var_notify_error.get()}
        return data

    def save_settings(self) -> bool:
        try:
            data = self.collect_settings()
            self.config = cfgmod.save(self.config_path, data)
        except cfgmod.ConfigError as error:
            self.settings_message.configure(text=str(error), style="Bad.TLabel")
            self.error("Einstellungen ungültig", str(error))
            self.set_status("Einstellungen wurden nicht gespeichert.")
            return False
        except OSError as error:
            self.settings_message.configure(text=f"Speichern nicht möglich: {error}", style="Bad.TLabel")
            self.error("Speichern nicht möglich", str(error))
            return False
        self.config_data = copy.deepcopy(self.config.data)
        self.settings_message.configure(text=f"Gespeichert: {self.config_path}" + ("" if self.config.backup_root else "  (Hinweis: Es ist noch kein Sicherungsziel eingetragen.)"), style="Ok.TLabel")
        self.set_status("Einstellungen gespeichert.")
        self.refresh_summary()
        return True

    def add_extra_source(self) -> None:
        folder = self.ask_folder("Zusätzlichen Ordner wählen")
        if not folder:
            return
        suggestion = re.sub(r"[^A-Za-z0-9_-]", "_", os.path.basename(folder.rstrip("\\/")) or "Quelle")
        if not suggestion[0].isalpha():
            suggestion = "Q_" + suggestion
        name = self.ask_text("Name der Quelle", "Name im Sicherungsstand (Buchstaben, Ziffern, _ und -):", suggestion)
        if not name:
            return
        self.extra_tree.insert("", "end", iid=f"x{fsutil.new_id(6)}", values=(name.strip(), os.path.normpath(folder)))

    def remove_extra_source(self) -> None:
        for iid in self.extra_tree.selection():
            self.extra_tree.delete(iid)

    # ------------------------------------------------------------------ Übersicht
    def refresh_summary(self) -> None:
        config = self.config
        self.summary_vars["target"].set(config.backup_root if config and config.backup_root else "nicht festgelegt (Registerkarte Einstellungen)")
        if config:
            try:
                sources, _ = cfgmod.resolve_sources(config)
                self.summary_vars["sources"].set(", ".join(f"{source.name}" for source in sources) or "keine")
            except OSError as error:
                self.summary_vars["sources"].set(f"Fehler: {error}")
        else:
            self.summary_vars["sources"].set("-")
        self.summary_vars["update"].set(f"SuperPyBackup {__version__}")
        if not (config and config.backup_root):
            self.summary_vars["last"].set("-")
            self.header_status.configure(text="Noch nicht eingerichtet")
        else:
            def work() -> tuple[str, str]:
                identity = get_identity()
                target = os.path.join(config.backup_root, identity.folder)
                refs = snapshots.list_snapshots(target, identity.sid, ("Daily",))
                complete = [ref for ref in refs if ref.manifest and ref.manifest.status == mf.COMPLETE]
                if not os.path.isdir(config.backup_root):
                    return "Sicherungsziel nicht erreichbar", "Ziel nicht erreichbar"
                if not complete:
                    return "Noch keine Sicherung vorhanden", "Bereit"
                newest = complete[0]
                return f"{newest.name} ({len(newest.manifest.files)} Dateien, {_human(newest.manifest.total_bytes)})", f"Letzter Stand: {newest.name}"

            def done(result: Any, error: BaseException | None) -> None:
                if error or result is None:
                    self.summary_vars["last"].set(f"nicht ermittelbar: {error}")
                    return
                self.summary_vars["last"].set(result[0])
                self.header_status.configure(text=result[1])

            self.run_async(work, done)

        def task_work() -> str:
            info = scheduler.status(self.task_name)
            if not info["exists"]:
                return "nicht eingerichtet"
            return f"eingerichtet ({info['delay_minutes']} Min. nach Anmeldung" + (f", täglich {info['daily_at']}" if info["daily_at"] else "") + (")" if info["enabled"] else ", DEAKTIVIERT)")

        self.run_async(task_work, lambda result, error: self.summary_vars["task"].set(result if not error else f"nicht ermittelbar: {error}"))

    # ------------------------------------------------------------------ Sicherung
    def _loaded_config_with_target(self) -> cfgmod.Config | None:
        try:
            config = cfgmod.load(self.config_path, require_target=True)
        except cfgmod.ConfigError as error:
            self.error("Sicherung nicht möglich", f"{error}\n\nBitte unter Einstellungen ein Sicherungsziel festlegen und speichern.")
            self.notebook.select(self.tab_settings)
            return None
        return config

    def start_backup(self, new_snapshot: bool, mode: str | None = None) -> None:
        if self._backup_running:
            return
        if self._ops:
            self.set_status("Bitte warten, ein anderer Vorgang läuft noch.")
            return
        config = self._loaded_config_with_target()
        if config is None:
            return
        self._cancel.clear()
        self._progress = ("scan", 0, 0, "Starte ...")
        self._backup_running = True
        self._set_busy_state(True)
        self._append(self.backup_log, "", clear=True)
        self.result_label.configure(text="Sicherung läuft ...", style="TLabel")
        self.set_status("Sicherung läuft ...")
        writer = QueueWriter(self.messages)

        def on_progress(phase: str, done: int, total: int, text: str) -> None:
            self._progress = (phase, done, total, text)

        self.run_async(lambda: engine.run_backup(config, new_snapshot=True if new_snapshot else None, mode=mode, echo=writer, progress=on_progress, cancel=self._cancel), self._backup_done, op=True)

    def _backup_done(self, result: engine.RunResult | None, error: BaseException | None) -> None:
        self._backup_running = False
        self._progress = None
        self.progress.configure(mode="determinate", value=0)
        self.progress_label.configure(text="")
        self._set_busy_state(False)
        if error or result is None:
            self.result_label.configure(text=f"Fehler: {error}", style="Bad.TLabel")
            self.set_status("Sicherung fehlgeschlagen.")
            self.notify(notifymod.ERROR, "Sicherung fehlgeschlagen", str(error or "Unbekannter Fehler"))
            return
        self.last_result = result
        stats = result.stats
        text = {
            engine.COMPLETE: "Sicherung erfolgreich abgeschlossen.",
            engine.ALREADY: "Heute wurde bereits erfolgreich gesichert (kein neuer Stand nötig).",
            engine.BUSY: "Es läuft bereits eine andere Sicherung.",
            engine.PARTIAL: "Nur teilweise erfolgreich: Einige Dateien konnten nicht gesichert werden (Teil-Stand, nicht als vollständig gewertet).",
            engine.FAILED: "Sicherung fehlgeschlagen.",
        }[result.status]
        if result.message:
            text += f"  {result.message}"
        if result.kind:
            text += " (" + ("Vollsicherung" if result.kind == engine.FULL else "Inkrement") + ")"
        text += f"\nNeu {stats.new}, geändert {stats.modified}, unverändert {stats.unchanged}, ausgelassen {stats.skipped}, Fehler {stats.errors} | übertragen {_human(stats.bytes_transferred)}, gesamt {_human(stats.total_bytes)} | {result.seconds:.1f} s"
        self.result_label.configure(text=text, style="Ok.TLabel" if result.status in (engine.COMPLETE, engine.ALREADY) else "Bad.TLabel" if result.status in (engine.FAILED, engine.PARTIAL) else "TLabel")
        self.set_status(text.splitlines()[0])
        message = notifymod.backup_message(result, {engine.FULL: "Vollsicherung", engine.INCREMENTAL: "Inkrement"}.get(result.kind, ""))
        if message:
            self.notify(*message)
        elif result.status == engine.ALREADY:
            self.notify(notifymod.SUCCESS, "Heute bereits gesichert", "Es war kein neuer Stand nötig.")
        elif result.status == engine.BUSY:
            self.notify(notifymod.ERROR, "Sicherung nicht gestartet", "Es läuft bereits eine andere Sicherung.")
        self.snapshot_refs = {}
        self.refresh_summary()

    def notify(self, event: str, title: str, message: str) -> None:
        """Desktop-Meldung zu einer Aktion in der Oberfläche; nur wenn in der gespeicherten Konfiguration eingeschaltet. Läuft im Hintergrund."""
        try:
            settings = cfgmod.load(self.config_path, require_target=False).notifications if self.config_path.is_file() else None
        except cfgmod.ConfigError:
            settings = None
        if notifymod.enabled_for(settings, event):
            threading.Thread(target=notifymod.show, args=(title, message), daemon=True).start()

    def test_notification(self) -> None:
        """Zeigt unabhaengig von den Einstellungen eine Testmeldung, damit man sieht, ob Windows sie anzeigt."""
        self.set_status("Testmeldung wird gesendet ...")

        def done(shown: bool | None, error: BaseException | None) -> None:
            if error or not shown:
                self.error("Testmeldung", "Die Meldung konnte nicht angezeigt werden. Bitte die Windows-Einstellung \"Benachrichtigungen\" und den Fokus-Assistenten prüfen.")
                self.set_status("Testmeldung nicht angezeigt.")
            else:
                self.set_status("Testmeldung gesendet.")

        self.run_async(lambda: notifymod.show("SuperPyBackup", "Testmeldung: Desktop-Meldungen funktionieren."), done)

    def start_cleanup(self) -> None:
        """Bereinigung: erst Vorschau (nichts wird geloescht), dann Rueckfrage, dann Loeschen unter der Sicherungssperre."""
        if self._backup_running or self._ops:
            return
        config = self._loaded_config_with_target()
        if config is None:
            return
        options = cleanupmod.Options()
        self._set_busy_state(True)
        self.set_status("Bereinigung wird vorbereitet ...")

        def planned(plan: cleanupmod.Plan | None, error: BaseException | None) -> None:
            self._set_busy_state(False)
            if error:
                self.error("Bereinigung nicht möglich", str(error))
                self.set_status("Bereinigung nicht möglich.")
                return
            if not plan.items:
                self.info("Bereinigung", "Es gibt nichts zu bereinigen." + "".join("\n\n" + note for note in plan.notes))
                self.set_status("Nichts zu bereinigen.")
                return
            counts: dict[str, int] = {}
            for item in plan.items:
                counts[item.kind] = counts.get(item.kind, 0) + 1
            lines = [f"{count} × {cleanupmod.KIND_LABELS[kind]}" for kind, count in counts.items()]
            text = "Folgendes wird endgültig gelöscht:\n\n" + "\n".join(lines)
            text += f"\n\nGröße der Stände (logisch): {_human(plan.total_bytes)}\nDer tatsächlich frei werdende Platz ist meist kleiner (gemeinsame Dateien)."
            if plan.notes:
                text += "\n\nHinweise:\n" + "\n".join(plan.notes)
            if not self.ask_yes_no("Bereinigen", text + "\n\nJetzt löschen?"):
                self.set_status("Bereinigung abgebrochen.")
                return
            self._set_busy_state(True)
            self.set_status("Bereinigung läuft ...")
            self.run_async(lambda: cleanupmod.execute(config, options), executed, op=True)

        def executed(result: cleanupmod.Result | None, error: BaseException | None) -> None:
            self._set_busy_state(False)
            if error:
                self.error("Bereinigung nicht möglich", str(error))
                self.set_status("Bereinigung nicht möglich.")
                self.notify(notifymod.ERROR, "Bereinigung nicht möglich", str(error))
                return
            text = f"Bereinigung: {len(result.removed)} entfernt, {len(result.failed)} fehlgeschlagen."
            if result.failed:
                details = "\n".join(f"{item.label}: {reason}" for item, reason in result.failed[:8])
                self.error("Bereinigung", text + "\n\n" + details)
            self.set_status(text)
            self.notify(notifymod.ERROR if result.failed else notifymod.SUCCESS, "Bereinigung abgeschlossen" if not result.failed else "Bereinigung mit Fehlern", text)
            self.refresh_summary()

        self.run_async(lambda: cleanupmod.build_plan(config, options), planned, op=True)

    def cancel_backup(self) -> None:
        if self._backup_running:
            self._cancel.set()
            self.set_status("Sicherung wird abgebrochen ...")

    # ------------------------------------------------------------------ Wiederherstellung
    def _target_dir(self) -> tuple[str, str] | None:
        config = self._loaded_config_with_target()
        if config is None:
            return None
        identity = get_identity()
        return os.path.join(config.backup_root, identity.folder), identity.sid

    def refresh_snapshots(self) -> None:
        located = self._target_dir()
        if located is None:
            return
        target, sid = located
        self.set_status("Sicherungsstände werden gelesen ...")

        def done(refs: list[snapshots.SnapshotRef] | None, error: BaseException | None) -> None:
            self.snap_tree.delete(*self.snap_tree.get_children())
            self.snapshot_refs = {}
            if error:
                self.error("Stände nicht lesbar", str(error))
                return
            labels = {"Daily": "Tagesstände", "Monthly": "Monatsstände", "Yearly": "Jahresstände"}
            for tier in snapshots.TIERS:
                group = [ref for ref in refs or [] if ref.tier == tier]
                parent = self.snap_tree.insert("", "end", iid=f"tier-{tier}", text=f"{labels[tier]} ({len(group)})", open=tier == "Daily")
                for ref in sorted(group, key=lambda r: r.name, reverse=True):
                    manifest = ref.manifest
                    status = "Defekt" if manifest is None else ("Vollständig" if manifest.status == mf.COMPLETE else "Teil-Stand")
                    self.snapshot_refs[ref.spec] = ref
                    self.snap_tree.insert(parent, "end", iid=ref.spec, text=ref.name, values=(manifest.date if manifest else "-", status, len(manifest.files) if manifest else "-", _human(manifest.total_bytes) if manifest else "-"))
            self.set_status(f"{len(self.snapshot_refs)} Sicherungsstände gefunden.")

        self.run_async(lambda: snapshots.list_snapshots(target, sid), done)

    def selected_snapshot(self) -> snapshots.SnapshotRef | None:
        selection = self.snap_tree.selection()
        return self.snapshot_refs.get(selection[0]) if selection else None

    def load_snapshot_files(self) -> None:
        ref = self.selected_snapshot()
        self.file_tree.delete(*self.file_tree.get_children())
        self._tree_cache, self._tree_manifest = None, None
        if ref is None or ref.manifest is None:
            return
        manifest = ref.manifest
        root: dict = {}
        for directory in manifest.directories:
            node = root
            for part in directory.split("/"):
                node = node.setdefault(part, {})
        for category in manifest.categories:
            root.setdefault(category["name"], {})
        for entry in manifest.files:
            parts = entry.path.split("/")
            node = root
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = entry.size
        self._tree_cache, self._tree_manifest = root, manifest
        self._insert_children("", "", root)

    def _insert_children(self, parent_iid: str, prefix: str, subtree: dict) -> None:
        for name in sorted(subtree, key=lambda item: (not isinstance(subtree[item], dict), item.lower())):
            value = subtree[name]
            iid = f"{prefix}{name}"
            if isinstance(value, dict):
                self.file_tree.insert(parent_iid, "end", iid=iid, text=name, values=("",), open=False)
                if value:
                    self.file_tree.insert(iid, "end", iid=iid + DUMMY, text="")
            else:
                self.file_tree.insert(parent_iid, "end", iid=iid, text=name, values=(_human(value),))

    def _open_node(self, event: object) -> None:
        iid = self.file_tree.focus()
        children = self.file_tree.get_children(iid)
        if len(children) == 1 and children[0].endswith(DUMMY) and self._tree_cache is not None:
            self.file_tree.delete(children[0])
            node = self._tree_cache
            for part in iid.split("/"):
                node = node[part]
            self._insert_children(iid, iid + "/", node)

    def selected_paths(self) -> list[str]:
        return [iid for iid in self.file_tree.selection() if not iid.endswith(DUMMY)]

    def start_restore(self) -> None:
        if self._ops:
            self.set_status("Bitte warten, ein anderer Vorgang läuft noch.")
            return
        ref = self.selected_snapshot()
        located = self._target_dir()
        if ref is None or ref.manifest is None or located is None:
            self.info("Wiederherstellung", "Bitte zuerst einen gültigen Sicherungsstand auswählen.")
            return
        paths = self.selected_paths()
        original = self.var_restore_mode.get() == "original"
        destination = self.var_restore_dest.get().strip()
        if not original and not destination:
            self.info("Wiederherstellung", "Bitte einen Zielordner wählen oder 'Am ursprünglichen Ort' einstellen.")
            return
        what = f"{len(paths)} ausgewählte Einträge" if paths else "den gesamten Stand"
        where = "an den ursprünglichen Ort" if original else f"nach {destination}"
        if not self.ask_yes_no("Wiederherstellen", f"{what} aus {ref.spec} {where} wiederherstellen?"):
            return
        overwrite = self.var_overwrite.get()
        if overwrite and not self.ask_yes_no("Vorhandene Dateien ersetzen", "Vorhandene Dateien im Ziel werden durch die gesicherte Fassung ersetzt.\nWirklich ersetzen?"):
            return
        _, sid = located
        config = self._loaded_config_with_target()
        log = RunLog(config.log_dir, user=sid, target=config.backup_root, max_bytes=config.log_max_bytes, echo=_PrefixWriter(self.messages, "restore-log"))
        self._append(self.restore_log, "", clear=True)
        self._set_busy_state(True)
        self.set_status("Wiederherstellung läuft ...")

        def work() -> restoremod.RestoreResult:
            return restoremod.restore(ref.path, None if original else destination, log, paths=paths or None, original=original, overwrite=overwrite, confirm_overwrite=lambda count: True, sid=sid)

        def done(result: restoremod.RestoreResult | None, error: BaseException | None) -> None:
            self._set_busy_state(False)
            if error:
                self._append(self.restore_log, f"FEHLER: {error}\n")
                self.error("Wiederherstellung fehlgeschlagen", str(error))
                self.set_status("Wiederherstellung fehlgeschlagen.")
                self.notify(notifymod.ERROR, "Wiederherstellung fehlgeschlagen", str(error))
                return
            self.last_restore = result
            summary = f"Wiederhergestellt: {result.copied} Dateien ({_human(result.bytes)}), ausgelassen: {result.skipped}, Fehler: {result.errors}"
            self._append(self.restore_log, summary + "\n")
            self.set_status(summary)
            self.notify(notifymod.ERROR if result.errors else notifymod.SUCCESS, "Wiederherstellung abgeschlossen" if not result.errors else "Wiederherstellung mit Fehlern", summary)

        self.run_async(work, done, op=True)

    def verify_selected(self) -> None:
        ref = self.selected_snapshot()
        located = self._target_dir()
        if ref is None or located is None or self._ops:
            return
        _, sid = located
        config = self._loaded_config_with_target()
        log = RunLog(config.log_dir, user=sid, target=config.backup_root, max_bytes=config.log_max_bytes, echo=_PrefixWriter(self.messages, "restore-log"))
        self._append(self.restore_log, "", clear=True)
        self._set_busy_state(True)
        self.set_status(f"{ref.spec} wird geprüft ...")

        def done(result: tuple[int, list[str]] | None, error: BaseException | None) -> None:
            self._set_busy_state(False)
            if error:
                self.error("Prüfung nicht möglich", str(error))
                self.notify(notifymod.ERROR, "Prüfung nicht möglich", str(error))
                return
            checked, bad = result
            text = f"{ref.spec}: {checked} Dateien geprüft, {len(bad)} fehlerhaft" + ("" if not bad else ": " + ", ".join(bad[:5]))
            self._append(self.restore_log, text + "\n")
            self.set_status(text)
            self.notify(notifymod.ERROR if bad else notifymod.SUCCESS, "Prüfung mit Fehlern" if bad else "Prüfung erfolgreich", text)

        self.run_async(lambda: restoremod.verify_snapshot(ref.path, log, sid), done, op=True)

    # ------------------------------------------------------------------ Aufgabe
    def _task_form(self) -> tuple[int, str | None]:
        try:
            delay = int(self.var_delay.get())
        except ValueError as error:
            raise scheduler.SchedulerError("Die Verzögerung muss eine ganze Zahl sein.") from error
        return delay, (self.var_daily_at.get().strip() if self.var_daily_on.get() else None)

    def install_task(self) -> None:
        try:
            delay, daily = self._task_form()
            config = cfgmod.load(self.config_path, require_target=True)
            program, arguments = scheduler.program_command(str(self.config_path))
            scheduler.install(self.task_name, program, arguments, delay_minutes=delay, daily_at=daily)
        except (cfgmod.ConfigError, scheduler.SchedulerError) as error:
            self.error("Aufgabe nicht eingerichtet", str(error))
            return
        self.set_status("Aufgabe eingerichtet.")
        self._append(self.task_text, f"Eingerichtet: {self.task_name}\nProgramm: {program} {arguments}\n", clear=True)
        self.refresh_summary()

    def refresh_task(self) -> None:
        def done(info: dict | None, error: BaseException | None) -> None:
            if error:
                self._append(self.task_text, f"Fehler: {error}\n", clear=True)
                return
            if not info["exists"]:
                self._append(self.task_text, f"Die Aufgabe {self.task_name} ist nicht eingerichtet.\n", clear=True)
                return
            self.var_delay.set(str(info["delay_minutes"] if info["delay_minutes"] is not None else 2))
            self.var_daily_on.set(bool(info["daily_at"]))
            if info["daily_at"]:
                self.var_daily_at.set(info["daily_at"])
            lines = [f"Aufgabe: {info['name']}", f"Aktiv: {'ja' if info['enabled'] else 'NEIN'}", f"Start nach Anmeldung: {info['delay_minutes']} Min.", f"Täglich: {info['daily_at'] or 'nein'}", f"Versteckt: {'ja' if info['hidden'] else 'nein'}", f"Rechte: {info['run_level']}", f"Parallele Instanzen: {info['multiple_instances']}", f"Programm: {info['command']}", f"Argumente: {info['arguments']}"]
            self._append(self.task_text, "\n".join(lines) + "\n", clear=True)

        self.run_async(lambda: scheduler.status(self.task_name), done)

    def check_task(self) -> None:
        def done(problems: list[str] | None, error: BaseException | None) -> None:
            text = f"Fehler: {error}" if error else ("Die Aufgabe ist in Ordnung." if not problems else "Abweichungen:\n  - " + "\n  - ".join(problems))
            self._append(self.task_text, text + "\n", clear=True)
            self.set_status(text.splitlines()[0])

        self.run_async(lambda: scheduler.check(self.task_name), done)

    def remove_task(self) -> None:
        if not self.ask_yes_no("Aufgabe entfernen", "Die Windows-Aufgabe entfernen? Sicherungen bleiben erhalten."):
            return
        try:
            removed = scheduler.uninstall(self.task_name)
        except scheduler.SchedulerError as error:
            self.error("Aufgabe nicht entfernt", str(error))
            return
        self._append(self.task_text, "Aufgabe entfernt.\n" if removed else "Die Aufgabe war nicht eingerichtet.\n", clear=True)
        self.set_status("Aufgabe entfernt." if removed else "Keine Aufgabe vorhanden.")
        self.refresh_summary()

    # ---- Aufgabe fuer die Updatepruefung
    def install_update_task(self) -> None:
        try:
            try:
                delay = int(self.var_upd_delay.get())
            except ValueError as error:
                raise scheduler.SchedulerError("Die Verzögerung muss eine ganze Zahl sein.") from error
            daily = self.var_upd_daily_at.get().strip() if self.var_upd_daily_on.get() else None
            config = cfgmod.load(self.config_path, require_target=False)
            program, arguments = scheduler.program_command(str(self.config_path), "update check --quiet")
            scheduler.install(self.update_task_name, program, arguments, delay_minutes=delay, daily_at=daily, description="SuperPyBackup: stille Pruefung auf neue Versionen mit Desktop-Meldung.")
        except (cfgmod.ConfigError, scheduler.SchedulerError) as error:
            self.error("Prüfaufgabe nicht eingerichtet", str(error))
            return
        self.set_status("Prüfaufgabe eingerichtet.")
        self._append(self.task_text, f"Eingerichtet: {self.update_task_name}\nProgramm: {program} {arguments}\n", clear=True)
        if not (config.notifications["enabled"] and config.notifications["on_update"]):
            self.info("Prüfaufgabe eingerichtet", "Die Meldungen bei neuen Versionen sind noch ausgeschaltet. Bitte unter Einstellungen → Desktop-Meldungen „Desktop-Meldungen einschalten“ und „Neue Version / Update“ aktivieren und speichern.")

    def refresh_update_task(self) -> None:
        def done(info: dict | None, error: BaseException | None) -> None:
            if error:
                self._append(self.task_text, f"Fehler: {error}\n", clear=True)
            elif not info["exists"]:
                self._append(self.task_text, f"Die Aufgabe {self.update_task_name} ist nicht eingerichtet.\n", clear=True)
            else:
                lines = [f"Aufgabe: {info['name']}", f"Aktiv: {'ja' if info['enabled'] else 'NEIN'}", f"Start nach Anmeldung: {info['delay_minutes']} Min.", f"Täglich: {info['daily_at'] or 'nein'}", f"Programm: {info['command']}", f"Argumente: {info['arguments']}"]
                self._append(self.task_text, "\n".join(lines) + "\n", clear=True)

        self.run_async(lambda: scheduler.status(self.update_task_name), done)

    def remove_update_task(self) -> None:
        if not self.ask_yes_no("Prüfaufgabe entfernen", "Die Windows-Aufgabe für die Updateprüfung entfernen?"):
            return
        try:
            removed = scheduler.uninstall(self.update_task_name)
        except scheduler.SchedulerError as error:
            self.error("Prüfaufgabe nicht entfernt", str(error))
            return
        self._append(self.task_text, "Prüfaufgabe entfernt.\n" if removed else "Die Prüfaufgabe war nicht eingerichtet.\n", clear=True)
        self.set_status("Prüfaufgabe entfernt." if removed else "Keine Prüfaufgabe vorhanden.")

    # ------------------------------------------------------------------ Update
    def check_update(self) -> None:
        try:
            update_settings = (cfgmod.load(self.config_path, require_target=False) if self.config_path.is_file() else cfgmod.from_dict({}, require_target=False)).update
        except cfgmod.ConfigError as error:
            self.error("Update", str(error))
            return
        self.update_label.configure(text="Prüfe ...", style="TLabel")
        self._update_settings = update_settings

        def done(info: updater.UpdateInfo | None, error: BaseException | None) -> None:
            if error:
                self.update_label.configure(text=f"Prüfung nicht möglich: {error}", style="Bad.TLabel")
                self.notify(notifymod.ERROR, "Updateprüfung nicht möglich", str(error))
                self.btn_update_install.configure(state="disabled")
                return
            self.last_update = info
            if info is None:
                self.update_label.configure(text=f"SuperPyBackup {__version__} ist aktuell.", style="Ok.TLabel")
                self.notify(notifymod.UPDATE, "Keine neue Version", f"SuperPyBackup {__version__} ist aktuell.")
                self.btn_update_install.configure(state="disabled")
            else:
                self.update_label.configure(text=f"Neue Version verfügbar: {info.version} (installiert: {__version__}).  {info.page}", style="TLabel")
                self.btn_update_install.configure(state="normal")
                self.notify(notifymod.UPDATE, "Neue Version von SuperPyBackup", f"Version {info.version} ist verfügbar (installiert: {__version__}).")

        self.run_async(lambda: updater.check_for_update(update_settings), done)

    def _install_dir(self) -> str | None:
        if getattr(sys, "frozen", False):
            return str(Path(sys.executable).parent)
        self.info("Update", "Die Programmaktualisierung ist nur in der installierten EXE-Version verfügbar.")
        return None

    def install_update(self, install_dir: str | None = None, smoke_test: Callable[[str, str], None] | None = None) -> None:
        info = self.last_update
        if info is None:
            return
        install_dir = install_dir or self._install_dir()
        if not install_dir:
            return
        if not self.ask_yes_no("Update installieren", f"Version {info.version} jetzt herunterladen, prüfen und installieren?\nDas Programm sollte danach neu gestartet werden."):
            return
        kwargs = {"smoke_test": smoke_test} if smoke_test else {}

        def done(names: list[str] | None, error: BaseException | None) -> None:
            if error:
                self.update_label.configure(text=f"Update nicht installiert (bestehende Installation unverändert): {error}", style="Bad.TLabel")
                self.notify(notifymod.ERROR, "Update nicht installiert", str(error))
                return
            self.notify(notifymod.UPDATE, "SuperPyBackup aktualisiert", f"Version {info.version} ist installiert. Bitte neu starten.")
            self.update_label.configure(text=f"Version {info.version} installiert ({', '.join(names)}). Bitte das Programm neu starten.", style="Ok.TLabel")
            self.btn_update_install.configure(state="disabled")
            self.info("Update installiert", "Das Update wurde installiert. Bitte starten Sie SuperPyBackup neu.")

        self.run_async(lambda: updater.install_update(info, install_dir, self._update_settings, confirm=lambda i: True, **kwargs), done)

    def rollback_update(self) -> None:
        install_dir = self._install_dir()
        if not install_dir or not self.ask_yes_no("Vorversion wiederherstellen", "Die Vorversion (.old) wiederherstellen?"):
            return
        try:
            restored = updater.rollback(install_dir)
        except updater.UpdateError as error:
            self.error("Update", str(error))
            return
        self.update_label.configure(text="Vorversion wiederhergestellt: " + ", ".join(restored), style="Ok.TLabel")
        self.notify(notifymod.UPDATE, "Vorversion wiederhergestellt", ", ".join(restored))

    # ------------------------------------------------------------------ Protokoll
    def _log_dir(self) -> str:
        try:
            return cfgmod.load(self.config_path, require_target=False).log_dir if self.config_path.is_file() else str(cfgmod.app_home() / "Logs")
        except cfgmod.ConfigError:
            return str(cfgmod.app_home() / "Logs")

    def refresh_logs(self) -> None:
        folder = self._log_dir()
        try:
            names = sorted((name for name in os.listdir(folder) if name.endswith(".log")), reverse=True)
        except OSError:
            names = []
        self.log_combo.configure(values=names)
        if names and self.var_log_file.get() not in names:
            self.var_log_file.set(names[0])
        self.show_log()

    def show_log(self) -> None:
        name = self.var_log_file.get()
        if not name:
            self._append(self.log_view, "Noch keine Protokolle vorhanden.\n", clear=True)
            return
        try:
            with open(os.path.join(self._log_dir(), name), "r", encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()[-600:]
            self._append(self.log_view, "".join(lines), clear=True)
        except OSError as error:
            self._append(self.log_view, f"Nicht lesbar: {error}\n", clear=True)

    def open_log_folder(self) -> None:
        folder = self._log_dir()
        os.makedirs(folder, exist_ok=True)
        subprocess.Popen(["explorer.exe", folder])

    # ------------------------------------------------------------------ Beenden
    def on_close(self) -> None:
        if self._backup_running:
            if not self.ask_yes_no("Beenden", "Eine Sicherung läuft noch. Abbrechen und beenden?\n(Es entsteht dabei kein unvollständiger Stand.)"):
                return
            self._cancel.set()
            deadline = time.time() + 30
            while self._ops and time.time() < deadline:
                self.update()
                time.sleep(0.05)
        self._closing = True
        self.destroy()


class _PrefixWriter:
    def __init__(self, sink: queue.Queue, kind: str) -> None:
        self.sink, self.kind = sink, kind

    def write(self, text: str) -> int:
        self.sink.put((self.kind, text))
        return len(text)

    def flush(self) -> None:
        pass


def main_gui(config_path: str | None = None) -> int:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # scharfe Darstellung auf Bildschirmen mit Skalierung
    except (AttributeError, OSError):
        pass
    app = App(config_path)
    app.mainloop()
    return 0
