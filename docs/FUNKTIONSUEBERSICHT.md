# Funktionsübersicht SuperPSBackup (Referenz) und Abbildung in SuperPyBackup

Grundlage ist die Analyse des PowerShell-Projekts `SuperPSBackup` (Stand 1.1.0, unverändert gelassen):
`Backup-UserProfile.ps1`, `Restore-UserProfile.ps1`, `Install-BackupTask.ps1`, `Update-UserProfileBackup.ps1`,
`Start-BackupGUI.ps1`, `FirstInstall.ps1`, Testsuiten und README.

## 1. Funktionen der Referenz und ihre Umsetzung

| # | Funktion in SuperPSBackup | SuperPyBackup | Modul |
|---|---|---|---|
| 1 | Quellordner aus `User Shell Folders` (Desktop, Dokumente, Bilder, Musik, Videos, Downloads, Favoriten), unterstützt Umleitung/OneDrive | Windows-Known-Folders-API (`SHGetKnownFolderPath`) statt Registry-Parsing; zusätzlich frei konfigurierbare Quellen | `winapi`, `config` |
| 2 | Pro Benutzer eigener Zielordner `<Name>-<SID>` | identisch | `engine` |
| 3 | Vorabprüfungen: Ziel vorhanden/erreichbar, Schreibprobe, Lock gegen parallele Läufe (Mutex + exklusive Lockdatei am Ziel), Quelle/Ziel überlappen nicht | identisch; zusätzlich Prüfung des freien Speicherplatzes vor dem Kopieren | `engine`, `fsutil` |
| 4 | Tagesstand `Daily/<JJJJ-MM-TT>`, pro Tag genau ein regulärer Stand; weitere Stände nur mit `-ForceSnapshot` (`<Datum>_<Zeit>_<GUID>`) | `existing_daily: skip \| new_snapshot` in der Konfiguration, `--new-snapshot` auf der Kommandozeile | `engine` |
| 5 | Mehrfachanmeldung am selben Tag: „AlreadyComplete“, Quellen werden nicht erneut gelesen | identisch | `engine` |
| 6 | Sicherungsart: **Inkrement** (Standard) oder **Vollsicherung** (alle Dateien neu kopiert, keine Hardlinks zum Vorgänger, eigenständiger Stand; Art steht als `stats.type` im Manifest). Automatisch alle `full_every_days` Tage (Standard 7, 0 = aus), erzwungen mit `backup --full` / `--incremental` und Schaltfläche „Vollsicherung“. Inkrementell: unveränderte Dateien (Größe, Zeitstempel, Hash) per NTFS-Hardlink aus dem letzten gültigen Stand, sonst Kopie | identisch, aber Änderungserkennung wahlweise per Metadaten (Standard, schnell) oder per Hash | `engine` |
| 7 | Staging in `.incomplete-<GUID>`, Freigabe erst nach Manifest durch atomares Umbenennen; Manifest `.snapshot.json` (Status, SID, Datum, Dateiliste mit SHA-256) | identisch (Format 2, Zeitstempel in ns, Dateiattribute) | `engine`, `manifest` |
| 8 | Fehler bei einer Datei → kein neuer Wiederherstellungspunkt, keine Rotation | **Abweichung:** Teil-Stand wird als `Partial` gekennzeichnet gesichert, nie als vollständig gewertet, löst keine Rotation aus (siehe Abschnitt 3) | `engine` |
| 9 | Junctions/Symlinks werden nicht verfolgt; Cloud-Ordner (ReparsePoint ohne LinkType) schon | identisch; zusätzlich: nur online vorhandene OneDrive-Dateien (Recall-Attribute) werden übersprungen, nicht heruntergeladen | `engine` |
| 10 | System-/Temporär-Attribut, `*.tmp`, `*.temp`, `desktop.ini`, `Thumbs.db` ausgelassen | identisch plus konfigurierbare Ausschlussmuster | `engine`, `config` |
| 11 | Monats-/Jahresstände `Monthly/<JJJJ-MM>`, `Yearly/<JJJJ>` als Hardlink-Archiv aus dem Tagesstand, erster erfolgreicher Stand der Periode | identisch | `engine` |
| 12 | Aufbewahrung 7 Tage / 12 Monate / 5 Jahre (Kalenderfenster), erst nach erfolgreichem neuen Stand, Verweigerung bei Verknüpfungen im Stand | identisch; ungültige Ordner werden übersprungen statt die gesamte Rotation zu blockieren | `retention` |
| 13 | Protokoll `<Datum>.log`, Aufbewahrung 30 Tage | identisch plus Größenbegrenzung je Datei und Laufzusammenfassung | `runlog` |
| 14 | Wiederherstellung: Kategorien, vorhandene Dateien nie still überschreiben, Hashprüfung, temporäre Datei + atomarer Austausch, Schutz vor Verknüpfungen im Pfad | identisch; zusätzlich Einzeldateien/Unterordner, ursprünglicher Ort, `list`, `verify` | `restore` |
| 15 | Windows-Aufgabe: Anmeldung + 2 Minuten, optional täglich, Benutzerkontext, versteckt, `IgnoreNew`, ohne Adminrechte | identisch (Aufgabenname `SuperPyBackup-<SID>`, getrennt von SuperPSBackup) | `scheduler` |
| 16 | Selbstupdate aus GitHub (Manifest, SHA-256, Transaktion, Rollback, Programmsperre) | GitHub-Releases, SHA-256-Prüfung, Download nur nach Bestätigung, Austausch mit Rollback | `updater` |
| 17 | GUI (Taskplanung, Einstellungen, Updatedialog) | tkinter-GUI mit Tabs Sicherung, Einstellungen, Wiederherstellung, Aufgabe, Update, Protokoll; Dunkelmodus als Standard | `gui` |
| 18 | `FirstInstall.ps1` mit Systemcheck | Eigenständige EXE ohne Installer; Installation = Datei kopieren + `task install` | – |
| 20 | Desktop-Meldungen (Windows-Toast) bei Sicherung, Update und Fehlern, auch bei der geplanten Aufgabe | neu: `notifications.enabled` (Standard aus) plus Schalter je Ereignis, `notify-test`, Testknopf in den Einstellungen; Meldungen auch bei Aktionen in der Oberfläche (Sicherung, Wiederherstellung, Prüfung, Bereinigung, Update); Anzeige über verstecktes Windows PowerShell mit eigenem Absendernamen „SuperPyBackup“ (HKCUSoftwareClassesAppUserModelIdSuperPyBackup.Desktop), Fehler beim Anzeigen beeinträchtigen nie eine Sicherung | `notify` |
| 21 | Aufgabe für die Updateprüfung, in der GUI einrichtbar (Reiter Aufgabe): `<Aufgabenname>-Update`, startet `update check --quiet` nach der Anmeldung und optional täglich; Meldung einmal je Version (`update-notified.json`) | neu; ohne Entsprechung in der Referenz | `scheduler`, `cli`, `gui` |
| 22 | Oberfläche im Infobereich: Symbol rechts unten, Schließen blendet nur aus (`gui.close_to_tray`), Rechtsklick-Menü (öffnen, Sicherung, Weiterer Stand, Vollsicherung, abbrechen, Autostart, beenden), Einzelinstanz, Autostart per `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` (`gui --tray`), Programmsymbol in Fenster, Infobereich und EXE | neu; nur Win32-API über ctypes, keine Zusatzpakete | `tray`, `autostart`, `icon`, `gui` |
| 23 | Programmsymbol „Datenträger“ mit Zustandsfarbe (blau bereit, orange arbeitet, rot Fehler) und optionale Desktop-Verknüpfung (`shortcut create/remove/status`, Schaltflächen in den Einstellungen; `.lnk` über WScript.Shell, Symboldatei im Datenordner) | neu | `icon`, `shortcut`, `tray`, `gui` |
| 19 | Migration vom Altnamen UserProfileBackup | entfällt (neues Projekt) | – |

## 2. Gefundene Schwächen der Referenz (nicht übernommen)

1. **Vollständiges Hashing aller Quelldateien in jedem Lauf**, und bei Wiederverwendung zusätzlich Hash der Vorgängerdatei: doppelter Lese-Aufwand auch bei unveränderten Dateien.
2. **Ein einziger Dateifehler verwirft den gesamten Lauf**: Auch bei 99,9 % erfolgreich gesicherten Dateien entsteht kein Wiederherstellungspunkt (sicher, aber unnötig verlustreich).
3. **Kein Speicherplatz-Vorabcheck.**
4. **Ungültiger Ordner in der Rotation blockiert die gesamte Rotation** (`throw`) und damit dauerhaft die Bereinigung.
5. **Ungültiger Tagesstand blockiert den Tageslauf** („wird nicht überschrieben“), statt einen neuen, eindeutig benannten Stand anzulegen.
6. **Keine Konfigurationsdatei**: Einstellungen nur über Parameter/GUI-JSON; keine Ausschlussregeln, keine benutzerdefinierten Quellen.
7. **Zeitstempel im Manifest als ISO-Text** (100-ns-Rundung möglich, PowerShell-5/7-Unterschiede beim JSON-Einlesen).
8. **Keine Ausnahme für Online-only-Dateien**: OneDrive-Platzhalter können beim Lesen heruntergeladen werden.
9. **Manifest-Schreiben nicht atomar** (`Set-Content` direkt in den Staging-Ordner; unkritisch wegen des anschließenden Umbenennens, aber Archivmanifeste hängen daran).
10. **Gespeicherte Sicherungsdateien schreibbar**: Eine Änderung einer Sicherungsdatei „in place“ würde über Hardlinks alle Stände verändern.
11. **Lange Pfade (> 260 Zeichen)** werden nicht gezielt behandelt.
12. **Protokolle ohne Größenlimit** pro Tag.

## 3. Bewusste funktionale Abweichungen

* **Teil-Stände (`Partial`)**: Ein Lauf mit Dateifehlern erzeugt einen eigenen, im Manifest als `Partial` markierten Stand (`Daily/<Datum>_<Zeit>_<ID>`). Er wird in `list` klar als unvollständig angezeigt, ist nie Grundlage für Hardlink-Wiederverwendung, wird nie zum Tages-/Monats-/Jahresstand befördert und löst keine Rotation aus. Der Prozess endet mit Exit-Code 2.
* **Änderungserkennung per Metadaten** (Größe + `mtime_ns`) als Standard; `change_detection: "hash"` stellt das Verhalten der Referenz her.
* **Gesicherte Dateien schreibgeschützt** (Hardlink-Schutz); Wiederherstellung setzt die Originalattribute.
* **Zusätzliche Befehle**: `list`, `verify`, `config`, `task status`, `update rollback`, `cleanup` (Bereinigung mit Vorschau und Bestätigung; auch in der Oberfläche).
* **Wiederherstellung** kennt neben Kategorien auch einzelne Dateien/Unterordner und den ursprünglichen Speicherort.
* **Update** per GitHub-Release-Dateien (`SuperPyBackup.exe`, `SuperPyBackupCmd.exe`, `SHA256SUMS.txt`) statt Skript-Dateiliste; Hash schützt vor Beschädigung, nicht vor einem kompromittierten Repository (keine Codesignatur).
