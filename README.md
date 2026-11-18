# SuperPyBackup

Inkrementelle, versionierte Sicherung der persönlichen Windows-Ordner – die eigenständige **Python-Version** des
PowerShell-Projekts [SuperPSBackup](https://github.com/2xlc-cloud/SuperPSBackup). Beide Programme sind unabhängig
voneinander installier- und betreibbar; SuperPyBackup verändert SuperPSBackup nicht.

* Windows 10/11, keine Administratorrechte, **keine Python-Installation beim Anwender** (fertige EXE-Dateien)
* Sicherung nach `Desktop`, `Dokumente`, `Bilder`, `Musik`, `Videos`, `Downloads`, `Favoriten` (Windows-Known-Folders, inkl. Umleitung/OneDrive) plus eigene Ordner
* Normale Dateien im Ziel (kein Archivformat): Jeder Stand ist ein Ordner, den man im Explorer öffnen kann
* Unveränderte Dateien werden per NTFS-Hardlink wiederverwendet – nur Neues/Geändertes belegt Speicher (**Inkrement**)
* **Symbol im Infobereich** (rechts unten in der Taskleiste): Schließen blendet das Fenster nur aus, die Oberfläche läuft weiter. Rechtsklick auf das Symbol: öffnen, sichern, weiteren Stand anlegen, Vollsicherung, Sicherung abbrechen, Autostart umschalten, beenden. Optional **Autostart beim Anmelden** (nur als Symbol, ohne Fenster); eine zweite Instanz holt das vorhandene Fenster nach vorn
* Einschaltbare **Desktop-Meldungen** (Windows-Toast, Absender „SuperPyBackup“) bei abgeschlossener Sicherung, Wiederherstellung, Prüfung, Bereinigung, neuer Version/Update und Fehlern – in der Oberfläche und auch wenn die Sicherung als geplante Aufgabe unsichtbar läuft
* Eigene **Aufgabe für die Updateprüfung** (Reiter „Aufgabe“): prüft still auf neue Versionen und meldet sie einmal je Version; installiert wird nie ohne Bestätigung
* Zusätzlich **Vollsicherungen**: alle Dateien werden neu kopiert, der Stand ist eigenständig und hängt an keinem Vorgänger – automatisch alle 7 Tage (einstellbar) oder auf Wunsch
* Aufbewahrung 7 Tage / 12 Monate / 5 Jahre, Wiederherstellung (Datei, Ordner, ursprünglicher Ort), Windows-Aufgabe, GitHub-Update

## Programme

| Datei | Zweck |
|---|---|
| `SuperPyBackup.exe` | **ohne Konsolenfenster** – Doppelklick öffnet die **grafische Oberfläche**; mit Befehlen (`backup --quiet`) läuft sie unsichtbar für die geplante Aufgabe (Fehler gehen ins Protokoll) |
| `SuperPyBackupCmd.exe` | Konsolenversion – Einrichtung, Wiederherstellung, Prüfung, Update (`SuperPyBackupCmd.exe gui` öffnet ebenfalls die Oberfläche) |

Beide enthalten denselben Code und akzeptieren dieselben Befehle.

## Installation

1. Beide EXE-Dateien (aus dem [GitHub-Release](https://github.com/2xlc-cloud/SuperPyBackup/releases/latest)) in einen festen Ordner kopieren, z. B. `C:\Tools\SuperPyBackup`.
   Prüfsummen stehen in `SHA256SUMS.txt` (`Get-FileHash .\SuperPyBackupCmd.exe`).
2. Konfiguration anlegen und bearbeiten:
   ```powershell
   .\SuperPyBackupCmd.exe config init --backup-root "E:\Backups"
   notepad $env:LOCALAPPDATA\SuperPyBackup\config.json
   .\SuperPyBackupCmd.exe config validate
   ```
3. Ersten Lauf von Hand starten (zeigt Fortschritt und Ergebnis):
   ```powershell
   .\SuperPyBackupCmd.exe backup
   ```
4. Automatischen Start einrichten (2 Minuten nach der Anmeldung, im Benutzerkontext, versteckt):
   ```powershell
   .\SuperPyBackupCmd.exe task install                    # optional: --delay 5 --daily-at 18:30
   .\SuperPyBackupCmd.exe task check
   ```

Die Konfiguration liegt unter `%LOCALAPPDATA%\SuperPyBackup` (außerhalb des Programmordners) und wird von Updates nie angefasst.

### Deinstallation

```powershell
.\SuperPyBackupCmd.exe task uninstall
# danach den Programmordner löschen; optional %LOCALAPPDATA%\SuperPyBackup (Konfiguration, Protokolle)
```
Die Sicherungsstände im Ziel werden dabei **nie** gelöscht.

## Grafische Oberfläche

`SuperPyBackup.exe` per Doppelklick starten (oder `SuperPyBackupCmd.exe gui`). Die Oberfläche nutzt dieselbe Engine und dieselbe `config.json` wie die Kommandozeile.

| Registerkarte | Funktion |
|---|---|
| **Sicherung** | Übersicht (Ziel, Quellen, letzter Stand, Windows-Aufgabe), **Jetzt sichern**, **Weiteren Stand anlegen**, **Abbrechen**, **Bereinigen …** (Vorschau, Rückfrage, dann löschen); Fortschrittsbalken und Live-Protokoll. Die Oberfläche bleibt dabei bedienbar; ein Abbruch hinterlässt keinen unvollständigen Stand |
| **Einstellungen** | Sicherungsziel (Ordner-Auswahl oder UNC-Pfad), Windows-Ordner an/aus, zusätzliche Ordner, Ausschlussmuster, Aufbewahrung, Verhalten, Protokoll, Update-Prüfung. **Speichern** prüft alles und zeigt Fehler verständlich an; die vorige Datei bleibt als `config.json.bak` erhalten |
| **Wiederherstellung** | Alle Tages-/Monats-/Jahresstände mit Status, Inhalt des Standes als aufklappbarer Baum, Auswahl einzelner Dateien/Ordner, Ziel = Ordner oder ursprünglicher Ort, optional Überschreiben (zweifach bestätigt), **Stand prüfen** |
| **Aufgabe** | Windows-Aufgabe einrichten/ändern (Verzögerung, tägliche Uhrzeit), prüfen, Status, entfernen |
| **Update** | Auf Updates prüfen; Installation erst nach Bestätigung; Vorversion wiederherstellen |
| **Protokoll** | Protokolldateien anzeigen, Ordner öffnen |
| **FAQ** | Alle Konsolenbefehle (`SuperPyBackupCmd.exe …`) mit kurzer Erklärung und Beispiel; **Kopieren** legt den Befehl in die Zwischenablage. Die Beispiele werden im Test mit dem echten Befehlsparser geprüft |

## Befehle

| Befehl | Wirkung |
|---|---|
| `gui [--tray]` | Oberfläche öffnen; mit `--tray` nur als Symbol im Infobereich (so startet der Autostart) |
| `update check --quiet` | Stille Updateprüfung für die geplante Aufgabe: Desktop-Meldung bei neuer Version (einmal je Version), Ergebnis im Protokoll |
| `notify-test` | Testmeldung auf dem Desktop anzeigen und den Schaltzustand nennen |
| `backup [--full \| --incremental] [--new-snapshot] [--quiet]` | Sicherung ausführen. `--full` erzwingt eine eigenständige Vollsicherung (legt auch bei vorhandenem Tagesstand einen weiteren Stand an), `--incremental` ein Inkrement; ohne Option entscheidet `backup.full_every_days`. Exit-Code 0 = vollständig/bereits vorhanden/anderer Lauf aktiv, **2 = Teil-Stand**, 1 = Fehler |
| `list [--json]` | Alle Stände mit Datum, Status (Complete/Partial/Defekt), Dateizahl, Größe |
| `restore --dest ZIEL \| --original [--snapshot S] [--category K] [--path P] [--overwrite [--yes]]` | Wiederherstellung (siehe unten) |
| `verify [--snapshot S]` | Jede Datei eines Standes gegen die gespeicherte SHA-256-Summe prüfen |
| `cleanup [--dry-run] [--yes] [--partial] [--skip-logs]` | Sicherungsziel bereinigen: Reste abgebrochener Läufe, abgelaufene Stände (Aufbewahrung), alte Protokolle; mit `--partial` auch Teil-Stände. Zeigt erst die Vorschau und löscht nur nach Bestätigung (`--yes` überspringt die Rückfrage, `--dry-run` löscht nie). Der neueste vollständige Stand und defekte Stände bleiben unangetastet; während einer Sicherung passiert nichts |
| `config init \| show \| validate` | Konfiguration anlegen (überschreibt nie), anzeigen, prüfen |
| `task install \| set \| status \| check \| uninstall [--name N]` | Windows-Aufgabe verwalten (Name muss mit `SuperPyBackup` beginnen) |
| `update check \| install [--yes] \| rollback` | Updates prüfen/installieren/zurücknehmen |
| `gui` | Grafische Oberfläche öffnen |

Globale Option: `--config PFAD` (Standard `%LOCALAPPDATA%\SuperPyBackup\config.json`, überschreibbar mit der Umgebungsvariable `SUPERPYBACKUP_CONFIG`).

## Konfiguration (`config.json`)

```jsonc
{
  "format": 1,
  "backup_root": "E:\\Backups",            // Pflicht: lokaler Pfad, USB-Laufwerk oder UNC (\\\\NAS\\Backup)
  "use_known_folders": true,                // Windows-Benutzerordner automatisch ermitteln
  "known_folders": ["Desktop", "Documents", "Pictures", "Music", "Videos", "Downloads", "Favorites"],
  "extra_sources": [ { "name": "Projekte", "path": "D:\\Projekte" } ],   // zusätzliche Ordner
  "exclude": ["*.iso", "node_modules", "Projekte/build/*"],              // Muster auf Namen oder relativen Pfad
  "use_default_excludes": true,             // *.tmp, *.temp, desktop.ini, Thumbs.db, ~$*, $RECYCLE.BIN, System Volume Information
  "retention": { "daily": 7, "monthly": 12, "yearly": 5 },
  "backup": {
    "existing_daily": "skip",               // "skip": heute schon gesichert -> nichts tun | "new_snapshot": weiteren Stand anlegen
    "change_detection": "metadata",         // "metadata": Größe+Zeitstempel (schnell) | "hash": jede Datei lesen und SHA-256 vergleichen
    "use_hardlinks": true,                  // false: unveränderte Dateien werden kopiert (dann ist jeder Stand eine Vollsicherung)
    "full_every_days": 7,                   // alle N Tage eine Vollsicherung statt Inkrement; 0 = nie automatisch
    "free_space_margin_percent": 5, "min_free_bytes": 0
  },
  "logging": { "directory": "", "keep_days": 30, "max_file_mb": 20 },   // leer = %LOCALAPPDATA%\SuperPyBackup\Logs
  "update": { "check_for_updates": true, "check_interval_hours": 24, "repository": "2xlc-cloud/SuperPyBackup", "api_url": "https://api.github.com" },
  "notifications": { "enabled": false, "on_success": true, "on_update": true, "on_error": true },   // Desktop-Meldungen (Standard: aus); die drei weiteren Schalter wählen die Ereignisse
  "gui": { "theme": "dark", "close_to_tray": true }   // Design "dark" (Standard) oder "light"; close_to_tray: Schließen blendet nur aus (Infobereich)
}
```

Alle Werte werden vor der Verwendung geprüft; unbekannte Schlüssel (Tippfehler), falsche Typen und Wertebereiche werden **zusammen** mit
verständlichen Meldungen abgewiesen. Systemdateien, temporäre Dateien, Verknüpfungen (Junctions/Symlinks) und nur online vorhandene
OneDrive-Dateien (werden nicht heruntergeladen) werden nicht gesichert. Liegt eine Quelle in einer anderen (z. B. Bilder in Dokumenten),
wird sie nur einmal gesichert.

## Aufbau des Sicherungsziels

```
<backup_root>\<Benutzer>-<SID>\
    Daily\2026-11-17\            ein regulärer Stand pro Kalendertag (weitere: 2026-11-17_143015_ab12cd34)
    Monthly\2026-11\             erster vollständige Stand des Monats
    Yearly\2026\                 erster vollständige Stand des Jahres
        .snapshot.json           Manifest (Status, Dateiliste mit SHA-256, Zeitstempel, Attribute)
        Desktop\...  Documents\...  Projekte\...     ← die gesicherten Dateien (je Quelle ein Ordner)
```

Ein Stand kann gleichzeitig Tages-, Monats- und Jahresstand sein, ohne Speicher zu verdoppeln (Hardlinks).
Gesicherte Dateien sind **schreibgeschützt**, damit eine versehentliche Änderung nicht über Hardlinks mehrere Stände verändert.
Zum Löschen eines Standes den Ordner im Explorer löschen (Schreibschutz-Rückfrage bestätigen) oder die Aufbewahrung arbeiten lassen.

### Transaktionssicherheit

* Ein neuer Lauf schreibt in `.incomplete-<id>`; erst ein einziges, atomares Umbenennen macht ihn sichtbar. Absturz oder Stromausfall beschädigen nie vorhandene Stände – Reste werden beim nächsten Lauf entfernt.
* **Teil-Stände:** Gibt es Dateifehler (z. B. gesperrte Dateien), wird der Rest trotzdem gesichert, der Stand aber als `Partial` markiert (Exit-Code 2). Er wird nie als vollständig gewertet, nie als Basis für Hardlinks genutzt, nie zum Monats-/Jahresstand befördert und löst keine Aufbewahrung aus.
* Abgelaufene Stände werden erst nach einem **erfolgreichen** neuen Stand entfernt; defekte oder unbekannte Ordner werden nie gelöscht.
* Parallele Läufe sind ausgeschlossen (Windows-Mutex plus Sperrdatei im Ziel – wirkt auch bei NAS/UNC über mehrere Rechner).

## Wiederherstellung

```powershell
.\SuperPyBackupCmd.exe list
# einzelne Datei aus einem früheren Stand in einen Ordner (je Kategorie ein Unterordner):
.\SuperPyBackupCmd.exe restore --snapshot daily/2026-11-10 --path Documents/Arbeit/bericht.docx --dest D:\Wiederherstellung
# ganzer Ordner an den ursprünglichen Ort:
.\SuperPyBackupCmd.exe restore --snapshot monthly/2026-09 --path Documents/Arbeit --original
# vorhandene Dateien ersetzen (nur nach ausdrücklicher Bestätigung):
.\SuperPyBackupCmd.exe restore --snapshot latest --category Desktop --original --overwrite
```

Stände: `latest`, `daily/JJJJ-MM-TT`, `monthly/JJJJ-MM`, `yearly/JJJJ` oder ein Ordnerpfad. Vorhandene Dateien werden standardmäßig **ausgelassen**;
`--overwrite` fragt einmal nach (`--yes` bestätigt ohne Rückfrage). Jede Datei wird vor dem Schreiben gegen ihre SHA-256-Summe geprüft, in eine
temporäre Datei geschrieben und atomar ausgetauscht; Zeitstempel und Attribute (versteckt/Archiv) werden wiederhergestellt. Verknüpfungen im
Zielpfad werden verweigert. Die Wiederherstellung funktioniert auch, während ein Backup läuft. Alles wird protokolliert.

## Protokolle

`%LOCALAPPDATA%\SuperPyBackup\Logs\JJJJ-MM-TT.log` (30 Tage, je Datei max. `max_file_mb` + eine Vorgängerdatei). Jeder Lauf protokolliert Benutzer (SID), Quellen, Ziel,
Laufzeit, gescannte/neue/geänderte/unveränderte/ausgelassene/fehlerhafte Dateien, übertragene und gesamte Datenmenge, tatsächlichen Mehrbedarf an Speicher,
Aufbewahrungsbereinigung, Warnungen/Fehler und den Status. Fehler vor dem Start (z. B. ungültige Konfiguration) landen auch ohne Konsole in `errors.log`, unerwartete Abstürze in `crash.log`.

## Updates

`update check` fragt nur die neueste GitHub-Release ab. `update install` lädt erst **nach ausdrücklicher Bestätigung** `SuperPyBackup.exe`, `SuperPyBackupCmd.exe` und `SHA256SUMS.txt`
herunter, prüft Download-Adresse (nur dieses Repository), Größe und SHA-256, startet die neue Datei erst danach einmal mit `--version`, tauscht die Programme aus und behält die Vorversion als `.old`
(`update rollback`). Jeder Fehler lässt die bestehende Installation unverändert. Hinweis: Die Summen schützen vor beschädigten Downloads, nicht vor einem kompromittierten Repository
(es gibt keine Codesignatur). Der Backup-Lauf prüft höchstens alle 24 Stunden auf Updates und meldet eine neue Version nur im Protokoll.

## Entwicklung

```powershell
python -m pip install -r build\requirements-build.txt
python -m pytest                 # alle Tests (die EXE-Tests laufen, sobald dist\ gebaut ist)
python build\build.py            # erzeugt dist\SuperPyBackup.exe, dist\SuperPyBackupCmd.exe, dist\SHA256SUMS.txt
python bench\benchmark.py --ps-project C:\Pfad\zu\SuperPSBackup      # Vergleich mit der PowerShell-Version
```

Nur die Python-Standardbibliothek wird zur Laufzeit verwendet (`ctypes` für Windows-Schnittstellen). PyInstaller und pytest sind reine Entwicklungswerkzeuge.
Modulübersicht: `engine` (Lauf), `snapshots`/`manifest` (Stände), `retention`, `restore`, `scheduler`, `updater`, `config`, `winapi`/`fsutil`, `runlog`, `cli`.
Die Funktionsabbildung zur PowerShell-Referenz steht in [docs/FUNKTIONSUEBERSICHT.md](docs/FUNKTIONSUEBERSICHT.md), der Vergleich in [PowerShell-vs-Python.md](PowerShell-vs-Python.md).

## Bekannte Einschränkungen

* Hardlinks setzen NTFS (oder ein anderes Dateisystem mit Hardlink-Unterstützung) im Ziel voraus; sonst wird kopiert (mehr Speicher, gleiches Ergebnis). NTFS erlaubt max. 1023 Links je Datei.
* Änderungserkennung `metadata` übersieht Änderungen, die Größe und Zeitstempel exakt erhalten; `change_detection: "hash"` schließt das aus (langsamer).
* Es wird der Änderungszeitstempel gesichert, nicht das Erstellungsdatum. Zugriffsrechte (ACL), alternative Datenströme und Verknüpfungen werden nicht gesichert.
* Gesperrte Dateien werden nicht gesichert (kein Schattenkopie-Dienst), sondern führen zu einem Teil-Stand.
* Die Oberfläche (tkinter) startet im **Dunkelmodus**; die Schaltfläche „Hell“/„Dunkel“ oben rechts wechselt (gespeichert in `gui.theme`). Systemdialoge (Ordnerauswahl, Rückfragen) bleiben im Windows-Design.
