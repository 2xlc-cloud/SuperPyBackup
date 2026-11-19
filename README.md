# SuperPyBackup

Inkrementelle, versionierte Sicherung der persönlichen Windows-Ordner – die eigenständige **Python-Version** des
PowerShell-Projekts [SuperPSBackup](https://github.com/2xlc-cloud/SuperPSBackup). Beide Programme sind unabhängig
voneinander installier- und betreibbar; SuperPyBackup verändert SuperPSBackup nicht.

* Windows 10/11, keine Administratorrechte, **keine Python-Installation beim Anwender** (fertige EXE-Dateien)
* Sicherung nach `Desktop`, `Dokumente`, `Bilder`, `Musik`, `Videos`, `Downloads`, `Favoriten` (Windows-Known-Folders, inkl. Umleitung/OneDrive) plus eigene Ordner
* Normale Dateien im Ziel (kein Archivformat): Jeder Stand ist ein Ordner, den man im Explorer öffnen kann
* Unveränderte Dateien werden per NTFS-Hardlink wiederverwendet – nur Neues/Geändertes belegt Speicher (**Inkrement**)
* **Programmsymbol „Datenträger“**, das seine **Farbe nach dem Zustand wechselt**: blau = bereit, orange = Sicherung/Arbeit läuft, rot = letzter Lauf fehlgeschlagen (Infobereich, Fenster und Taskleiste)
* **Desktop-Verknüpfung auf Bedarf** (Einstellungen → Start und Infobereich oder `shortcut create`)
* **Netzwerkziel (UNC)**: Als Sicherungsziel geht auch `\\Server\Freigabe\Ordner`. Unter Einstellungen → Sicherungsziel hinterlegt „Anmeldedaten …“ Benutzer und Kennwort für die Freigabe, „Verbindung testen“ prüft Anmeldung, Ordner, Schreiben/Lesen/Löschen, Hardlinks und freien Speicher. Das Kennwort wird mit Windows (DPAPI) nur für das eigene Benutzerkonto verschlüsselt in `credentials.json` im Programmordner (`%LOCALAPPDATA%\SuperPyBackup`) abgelegt, nie in `config.json`, nie in Protokollen, Aufgaben oder auf GitHub. Ohne hinterlegte Anmeldedaten gilt das Windows-Konto. Kommandozeile: `credentials set|list|remove|test`
* **Suchen und Vorschau beim Wiederherstellen**: Im Reiter Wiederherstellung sucht „In allen Ständen suchen“ nach einem Teil des Pfades oder einem Muster (`rechnung`, `*.docx`) in allen Tages-, Monats- und Jahresständen und zeigt jede unterschiedliche Fassung einer Datei (neueste oben, mit der Zahl weiterer Stände, die dieselbe Fassung enthalten). „Vorschau“ zeigt den Anfang von Textdateien, PNG/GIF-Bilder oder die Eckdaten, ohne etwas zu kopieren. „Zum Wiederherstellen auswählen“ wählt Stand und Datei im Baum aus; danach Zielordner wählen und „Wiederherstellen“. Kommandozeile: `find MUSTER`
* **Warnung bei ausbleibender Sicherung**: Liegt die letzte erfolgreiche Sicherung einer Sicherung länger als `backup.warn_after_days` (Standard 3, 0 = aus) zurück, erscheint eine Desktop-Meldung „Sicherung überfällig“ (höchstens einmal pro Tag und Sicherung; bei eingeschalteten Meldungen). Geprüft wird nach jedem nicht erfolgreichen Lauf, in der täglichen Updateprüfung der Aufgabe (`update check --quiet`) und in der Oberfläche (Start, danach alle 6 Stunden). Der letzte Erfolg steht lokal im Programmordner, nicht im Sicherungsziel.
* **Zielordner**: Fehlt der Ordner des Sicherungsziels, aber der übergeordnete Ordner existiert, wird er (eine Ebene) automatisch angelegt. Fehlt auch der übergeordnete (Laufwerk/Freigabe nicht da, Tippfehler), bleibt es beim Fehler.
* **Mehrere Sicherungen**: Oben in der Oberfläche wählt „Sicherung:“ zwischen der Hauptsicherung „Standard“ und weiteren Sicherungen (Neu … / Entfernen). Jede hat eigene Quellen, eigenes Ziel, eigene Aufbewahrung und Einstellungen (Datei `jobs\<Name>.json` neben der `config.json`) und einen eigenen Unterordner im Ziel (`<Benutzerordner>__<Name>`). Geplant wird je Sicherung (Reiter Aufgabe, Aufgabe `…-Job-<Name>`) oder für alle zusammen (Aufgabe `…-Alle`, führt die Sicherungen nacheinander aus). Entfernen löscht nie gesicherte Daten
* **Sicherungen anderer Prozesse sichtbar**: Läuft eine Sicherung, die per Aufgabe oder Konsole gestartet wurde, färbt sich das Symbol orange und das Protokoll im Reiter „Sicherung“ läuft live mit und **listet jede einzelne Datei** (Aktualisierung alle 5 Sekunden; die Zeilen kommen aus der Live-Datei `aktuell.log` im Protokollordner, die mit jedem Lauf neu begonnen wird, das Tagesprotokoll bleibt schlank); am Ende wechselt das Symbol zurück (rot bei Fehlern)
* **Symbol im Infobereich** (rechts unten in der Taskleiste): Schließen blendet das Fenster nur aus, die Oberfläche läuft weiter. Rechtsklick auf das Symbol: öffnen, sichern, weiteren Stand anlegen, Vollsicherung, Sicherung abbrechen, Autostart umschalten, beenden. Optional **Autostart beim Anmelden** (nur als Symbol, ohne Fenster); eine zweite Instanz holt das vorhandene Fenster nach vorn
* Einschaltbare **Desktop-Meldungen** (Windows-Toast, Absender „SuperPyBackup“) bei abgeschlossener Sicherung (auch „Heute bereits gesichert“, wenn ein weiterer Start keinen neuen Stand braucht), Wiederherstellung, Prüfung, Bereinigung, neuer Version/Update und Fehlern – in der Oberfläche und auch wenn die Sicherung als geplante Aufgabe unsichtbar läuft
* Eigene **Aufgabe für die Updateprüfung** (Reiter „Aufgabe“): prüft still auf neue Versionen und meldet sie einmal je Version; installiert wird nie ohne Bestätigung
* Zusätzlich **Vollsicherungen**: alle Dateien werden neu kopiert, der Stand ist eigenständig und hängt an keinem Vorgänger – automatisch alle 7 Tage (einstellbar) oder auf Wunsch
* Aufbewahrung 7 Tage / 12 abgeschlossene Monate / 5 abgeschlossene Jahre (Monats- und Jahresstände entstehen erst nach Monats- bzw. Jahresende, nicht schon beim ersten Stand), Wiederherstellung (Datei, Ordner, ursprünglicher Ort), Windows-Aufgabe, GitHub-Update

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
| `shortcut create\|remove\|status` | Verknüpfung „SuperPyBackup“ auf dem Desktop anlegen, entfernen oder anzeigen (nur diese Datei, nur auf Wunsch) |
| `gui [--tray]` | Oberfläche öffnen; mit `--tray` nur als Symbol im Infobereich (so startet der Autostart) |
| `update check --quiet` | Stille Updateprüfung für die geplante Aufgabe: Desktop-Meldung bei neuer Version (einmal je Version), Ergebnis im Protokoll |
| `notify-test` | Testmeldung auf dem Desktop anzeigen und den Schaltzustand nennen |
| `backup [--full \| --incremental] [--new-snapshot] [--quiet]` | Sicherung ausführen. `--full` erzwingt eine eigenständige Vollsicherung (legt auch bei vorhandenem Tagesstand einen weiteren Stand an), `--incremental` ein Inkrement; ohne Option entscheidet `backup.full_every_days`. Exit-Code 0 = vollständig/bereits vorhanden/anderer Lauf aktiv, **2 = Teil-Stand**, 1 = Fehler |
| `list [--json]` | Alle Stände mit Datum, Status (Complete/Partial/Defekt), Dateizahl, Größe |
| `restore --dest ZIEL \| --original [--snapshot S] [--category K] [--path P] [--overwrite [--yes]]` | Wiederherstellung (siehe unten) |
| `find MUSTER [--limit N]` | Datei in allen Sicherungsständen suchen (Teil des Pfades oder Muster mit `*` `?`); zeigt jede unterschiedliche Fassung |
| `verify [--snapshot S]` | Jede Datei eines Standes gegen die gespeicherte SHA-256-Summe prüfen |
| `cleanup [--dry-run] [--yes] [--partial] [--skip-logs]` | Sicherungsziel bereinigen: Reste abgebrochener Läufe, abgelaufene Stände (Aufbewahrung), alte Protokolle; mit `--partial` auch Teil-Stände. Zeigt erst die Vorschau und löscht nur nach Bestätigung (`--yes` überspringt die Rückfrage, `--dry-run` löscht nie). Der neueste vollständige Stand und defekte Stände bleiben unangetastet; während einer Sicherung passiert nichts |
| `config init \| show \| validate` | Konfiguration anlegen (überschreibt nie), anzeigen, prüfen |
| `task install \| set \| status \| check \| uninstall [--name N] [--all]` | Windows-Aufgabe verwalten (Name muss mit `SuperPyBackup` beginnen); `--all` = Aufgabe für alle Sicherungen |
| `credentials set \| list \| remove \| test [--share S] [--user U] [--password-stdin]` | Anmeldedaten für ein Netzwerkziel (UNC) verschlüsselt speichern, anzeigen, entfernen und die Verbindung testen; das Kennwort nie als Argument |
| `job list \| add NAME [--copy-from X] \| remove NAME` | Mehrere Sicherungen verwalten; mit `--job NAME` (vor dem Befehl) wählt jeder Befehl eine weitere Sicherung, `backup --all` sichert alle nacheinander |
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
    "free_space_margin_percent": 5, "min_free_bytes": 0,
    "warn_after_days": 3                    // Desktop-Meldung, wenn so viele Tage keine erfolgreiche Sicherung lief; 0 = aus
  },
  "logging": { "directory": "", "keep_days": 30, "max_file_mb": 20 },   // leer = %LOCALAPPDATA%\SuperPyBackup\Logs
  "update": { "check_for_updates": true, "check_interval_hours": 24, "force_after_versions": 5, "require_signature": true, "repository": "2xlc-cloud/SuperPyBackup", "api_url": "https://api.github.com" },
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
    Monthly\2026-10\             letzter vollständiger Stand eines abgeschlossenen Monats (entsteht erst, wenn der Monat vorbei ist)
    Yearly\2025\                 letzter Monatsstand eines abgeschlossenen Jahres (entsteht erst, wenn das Jahr vorbei ist)
        .snapshot.json           Manifest (Status, Dateiliste mit SHA-256, Zeitstempel, Attribute)
        Desktop\...  Documents\...  Projekte\...     ← die gesicherten Dateien (je Quelle ein Ordner)
```

Der Monatsstand wird beim ersten Lauf nach Monatsende aus dem letzten vollständigen Tagesstand dieses Monats angelegt (sofern er noch vorhanden ist, Tagesstände werden 7 Tage aufbewahrt), der Jahresstand beim ersten Lauf nach Jahresende aus dem letzten Monatsstand des Jahres. Ein Stand kann gleichzeitig Tages-, Monats- und Jahresstand sein, ohne Speicher zu verdoppeln (Hardlinks). Bereits vorhandene Monats-/Jahresstände bleiben unverändert.
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

**Signierte Veröffentlichungen:** Jede Release enthält neben `SHA256SUMS.txt` die Datei `SHA256SUMS.txt.sig`, eine Ed25519-Signatur über die Prüfsummen. Der öffentliche Schlüssel steckt fest im Programm (`updater.RELEASE_PUBLIC_KEYS`), der geheime liegt nur beim Herausgeber (`%USERPROFILE%\.superpybackup\release-signing.key`, nie im Repository). Ein Update wird nur installiert, wenn die Signatur zu den Prüfsummen passt (`update.require_signature`, Standard `true`) – so reicht ein manipuliertes Repository oder ein übernommenes GitHub-Konto nicht, um Schadsoftware einzuspielen, selbst wenn Programmdateien und Prüfsummen zusammen ausgetauscht werden. Das gilt für normale Updates und das Pflichtupdate gleichermaßen; ohne gültige Signatur bleibt die Installation unverändert. Versionen vor 1.13.0 prüfen noch keine Signatur. Veröffentlichen: `python build\sign_release.py keygen` (einmalig), danach signiert `python build\build.py` automatisch; den Schlüssel unbedingt sichern.

**Pflichtupdate:** Ist die installierte Version mehr als `update.force_after_versions` (Standard **5**) veröffentlichte Versionen hinter der neuesten, installieren die **automatischen** Prüfungen das Update **ohne Rückfrage**: nach einer Sicherung, bei der geplanten Updateprüfung (`update check --quiet`) und in der Oberfläche (beim Start, danach alle 6 Stunden, und bei „Nach Updates suchen“). Gezählt werden die Versionen auf GitHub, die neuer sind als die installierte (Entwürfe und Vorabversionen zählen nicht); bei genau 5 ist es noch kein Pflichtupdate, bei 6 schon. Es gelten alle Sicherungen der normalen Installation (nur dieses Repository, Größe, SHA-256, Probelauf, Rollback, Vorversion als `.old`); schlägt es fehl, bleibt die Installation unverändert, wird protokolliert (und bei eingeschalteten Meldungen gemeldet) und beim nächsten Prüfabstand erneut versucht. Eine Sicherung wird nie dadurch aufgehalten: Das Update kommt erst danach. Eine laufende Oberfläche muss danach neu gestartet werden. Aus: `"force_after_versions": 0` oder `"check_for_updates": false`; beim Start aus dem Quellcode wird nur protokolliert. Der Befehl `update check` zeigt nur an, dass ein Pflichtupdate fällig ist.

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
