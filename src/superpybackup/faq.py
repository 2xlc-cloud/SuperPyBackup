"""Inhalt des FAQ-Reiters: alle Konsolenbefehle mit kurzer Erklärung und kopierbarem Beispiel.

Die Beispiele sind Argumente für SuperPyBackupCmd.exe; ein Test prüft, dass jedes Beispiel vom echten Befehlsparser akzeptiert wird.
"""

from __future__ import annotations

import shlex

EXE = "SuperPyBackupCmd.exe"

# (Abschnitt, [(Titel, Erklärung, Argumente), ...])
SECTIONS: list[tuple[str, list[tuple[str, str, str]]]] = [
    ("Allgemein", [
        ("Version anzeigen", "Zeigt die installierte Version.", "--version"),
        ("Andere Konfigurationsdatei verwenden", "Die Option --config steht immer vor dem Befehl und gilt für alle Befehle.", '--config "D:\\Sicherung\\config.json" backup'),
        ("Oberfläche öffnen", "Startet diese grafische Oberfläche. SuperPyBackup.exe ohne Argumente macht dasselbe.", "gui"),
    ]),
    ("Konfiguration", [
        ("Konfiguration anlegen", "Erzeugt eine config.json mit Standardwerten und dem angegebenen Sicherungsziel.", 'config init --backup-root "D:\\Sicherung"'),
        ("Konfiguration anzeigen", "Gibt die aktuell gueltige Konfiguration aus.", "config show"),
        ("Konfiguration prüfen", "Prüft alle Einstellungen und nennt jeden Fehler einzeln. Rückgabewert 0 bedeutet gueltig.", "config validate"),
    ]),
    ("Sicherung", [
        ("Sicherung starten", "Legt den Tagesstand an. Gibt es heute schon einen vollständigen Stand, passiert nichts (AlreadyComplete).", "backup"),
        ("Weiteren Stand anlegen", "Erzwingt einen zusätzlichen Stand, auch wenn heute schon einer existiert.", "backup --new-snapshot"),
        ("Vollsicherung erzwingen", "Kopiert alle Dateien neu, ohne Hardlinks zum Vorgänger: ein eigenständiger Stand. Legt auch bei vorhandenem Tagesstand einen weiteren an.", "backup --full"),
        ("Inkrement erzwingen", "Kopiert nur Geändertes, Unverändertes wird per Hardlink übernommen. Ohne Vorgänger wird trotzdem voll gesichert. Ohne Option entscheidet die Einstellung backup.full_every_days (Standard: alle 7 Tage voll).", "backup --incremental"),
        ("Alle Sicherungen nacheinander", "Führt die Hauptsicherung und alle weiteren Sicherungen (Befehl job) nacheinander aus. Eine fehlerhafte Sicherung hält die übrigen nicht auf; der Rückgabewert ist der schlechteste.", "backup --all"),
        ("Eine bestimmte Sicherung", "Die Option --job steht vor dem Befehl und wählt eine weitere Sicherung (ohne Angabe: Standard). Sie gilt für backup, list, restore, verify, cleanup, config und task.", "--job Arbeit backup"),
        ("Ohne Ausgabe", "Für geplante Aufgaben: keine Konsolenausgabe, Ergebnis nur im Protokoll und im Rückgabewert (0 = vollstaendig, 2 = Teil-Stand, 1 = Fehler).", "backup --quiet"),
    ]),
    ("Stände ansehen und prüfen", [
        ("Stände auflisten", "Zeigt alle Stände (Daily, Monthly, Yearly) mit Datum, Größe und Status.", "list"),
        ("Als JSON auflisten", "Maschinenlesbare Ausgabe, z. B. für Skripte.", "list --json"),
        ("Neuesten Stand prüfen", "Vergleicht alle gesicherten Dateien mit den SHA-256-Werten im Manifest.", "verify"),
        ("Bestimmten Stand prüfen", "Snapshot: latest, daily/JJJJ-MM-TT, monthly/JJJJ-MM, yearly/JJJJ oder ein Pfad.", "verify --snapshot daily/2026-11-18"),
    ]),
    ("Wiederherstellung", [
        ("Alles in einen Ordner", "Stellt den neuesten Stand in einen Zielordner wieder her (ein Unterordner je Kategorie). Vorhandene Dateien bleiben unberührt.", 'restore --snapshot latest --dest "D:\\Wiederherstellung"'),
        ("Nur eine Kategorie", "Kategorien: Desktop, Documents, Pictures, Music, Videos, Downloads, Favorites. Die Option darf mehrfach stehen.", 'restore --snapshot latest --category Documents --dest "D:\\Wiederherstellung"'),
        ("Nur eine Datei oder einen Ordner", "Pfad relativ zum Stand, Kategorie zuerst. Die Option darf mehrfach stehen.", 'restore --path Documents/Projekt/a.txt --dest "D:\\Wiederherstellung"'),
        ("Aus einem älteren Stand", "Wählt einen bestimmten Tages-, Monats- oder Jahresstand.", 'restore --snapshot monthly/2026-10 --dest "D:\\Wiederherstellung"'),
        ("An den ursprünglichen Ort", "Schreibt die Dateien dorthin zurück, wo sie gesichert wurden. Vorhandene Dateien werden nicht überschrieben.", "restore --snapshot latest --original"),
        ("Vorhandene Dateien ersetzen", "Mit --overwrite werden vorhandene Dateien ersetzt; --yes beantwortet die Sicherheitsfrage vorab. Nur gezielt einsetzen.", "restore --snapshot latest --original --overwrite --yes"),
    ]),
    ("Bereinigung", [
        ("Vorschau der Bereinigung", "Zeigt, was entfernt würde: Reste abgebrochener Läufe, abgelaufene Stände (Aufbewahrung 7/12/5), alte Protokolle. Es wird nichts gelöscht.", "cleanup --dry-run"),
        ("Bereinigen", "Zeigt die Vorschau und löscht nach Rückfrage. Der neueste vollständige Stand bleibt immer erhalten; defekte Stände werden nur gemeldet.", "cleanup"),
        ("Ohne Rückfrage bereinigen", "Für Skripte: löscht ohne Rückfrage. Während einer laufenden Sicherung passiert nichts.", "cleanup --yes"),
        ("Auch Teil-Stände entfernen", "Entfernt zusätzlich Stände mit Status Partial (Sicherung mit Dateifehlern).", "cleanup --partial --dry-run"),
        ("Protokolle behalten", "Lässt alte Protokolldateien aus.", "cleanup --skip-logs"),
    ]),
    ("Desktop-Verknüpfung", [
        ("Verknüpfung anlegen", "Legt auf dem Desktop eine Verknüpfung „SuperPyBackup“ an, die die Oberfläche öffnet (auch in der Oberfläche unter Einstellungen → Start und Infobereich).", "shortcut create"),
        ("Verknüpfung entfernen", "Entfernt nur diese Verknüpfung vom Desktop; andere Dateien bleiben unberührt.", "shortcut remove"),
    ]),
    ("Infobereich", [
        ("Nur im Infobereich starten", "Startet die Oberfläche ohne Fenster als Symbol rechts unten in der Taskleiste (Rechtsklick: sichern, öffnen, beenden). Das trägt auch der Autostart ein (Einstellungen → Start und Infobereich).", "gui --tray"),
    ]),
    ("Desktop-Meldungen", [
        ("Updateprüfung für die Aufgabe", "Stille Prüfung für die geplante Aufgabe (Reiter Aufgabe in der Oberfläche richtet sie ein): meldet eine neue Version einmal per Desktop-Meldung und schreibt das Ergebnis ins Protokoll.", "update check --quiet"),
        ("Testmeldung anzeigen", "Zeigt eine Meldung auf dem Desktop und nennt, ob Meldungen in der Konfiguration eingeschaltet sind (notifications.enabled, Standard: aus). Eingeschaltet melden Sicherung, Update und Fehler, auch bei der geplanten Aufgabe.", "notify-test"),
    ]),
    ("Mehrere Sicherungen", [
        ("Sicherungen anzeigen", "Listet die Hauptsicherung „Standard“ und alle weiteren Sicherungen mit ihrer Konfigurationsdatei (Ordner jobs neben der config.json).", "job list"),
        ("Sicherung anlegen", "Legt eine weitere Sicherung mit eigenen Quellen, eigenem Ziel (eigener Unterordner), eigener Aufbewahrung und Einstellungen an. Name: Buchstaben, Ziffern, . _ -", "job add Arbeit"),
        ("Einstellungen übernehmen", "Legt die neue Sicherung als Kopie einer vorhandenen an; danach Quellen und Ziel anpassen.", "job add Fotos --copy-from Standard"),
        ("Sicherung entfernen", "Entfernt nur die Einstellungen und die Aufgabe dieser Sicherung. Bereits gesicherte Stände im Ziel bleiben erhalten. Standard lässt sich nicht entfernen.", "job remove Fotos"),
    ]),
    ("Netzwerkziel (UNC)", [
        ("Anmeldedaten speichern", "Für ein Sicherungsziel wie \\\\NAS\\Backup: speichert Benutzer und Kennwort (das Kennwort kommt über die Standardeingabe, nie als Argument) und testet danach die Verbindung. Das Kennwort wird mit Windows (DPAPI) nur für Ihr Benutzerkonto verschlüsselt im Programmordner abgelegt.", 'credentials set --share "\\\\NAS\\Backup" --user benutzer --password-stdin'),
        ("Verbindung testen", "Prüft das Sicherungsziel der Konfiguration: Anmeldung, Ordner, Schreiben/Lesen/Löschen einer Testdatei, Hardlinks, freier Speicher. Mit --share ein anderes Ziel.", "credentials test"),
        ("Gespeicherte Anmeldedaten anzeigen", "Zeigt Freigaben und Benutzernamen (nie Kennwörter).", "credentials list"),
        ("Anmeldedaten entfernen", "Löscht die gespeicherten Anmeldedaten einer Freigabe; gesicherte Daten bleiben unberührt.", 'credentials remove --share "\\\\NAS\\Backup"'),
    ]),
    ("Windows-Aufgabe", [
        ("Aufgabe einrichten", "Legt die versteckte Aufgabe an: Start 2 Minuten nach der Anmeldung, ohne Administratorrechte.", "task install"),
        ("Andere Wartezeit", "Wartezeit in Minuten nach der Anmeldung.", "task install --delay 5"),
        ("Zusätzlich täglich", "Startet die Sicherung zusätzlich jeden Tag zur angegebenen Uhrzeit (HH:MM).", "task install --daily-at 20:00"),
        ("Aufgabe ändern", "Aktualisiert eine vorhandene Aufgabe mit neuen Werten.", "task set --delay 3 --daily-at 12:30"),
        ("Eigener Aufgabenname", "Der Name muss mit SuperPyBackup beginnen; fremde Aufgaben werden nie angefasst.", "task install --name SuperPyBackup-Test"),
        ("Aufgabe für alle Sicherungen", "Legt eine Aufgabe an, die alle Sicherungen nacheinander ausführt (Name: Basisname plus -Alle). Einzelne Sicherungen haben eigene Aufgaben (--job Name task install).", "task install --all --daily-at 20:00"),
        ("Aufgabe einer Sicherung", "Richtet die Aufgabe der gewählten Sicherung ein (Name: Basisname plus -Job- und der Name der Sicherung), z. B. mit eigener Uhrzeit.", "--job Arbeit task install --daily-at 19:30"),
        ("Aufgabe anzeigen", "Zeigt, ob die Aufgabe existiert, wann sie zuletzt lief und was sie startet.", "task status"),
        ("Aufgabe prüfen", "Prüft, ob die Aufgabe richtig eingerichtet ist (Programm, Benutzer, Versteckt, Mehrfachstart).", "task check"),
        ("Aufgabe entfernen", "Löscht die Aufgabe. Bereits gesicherte Stände bleiben erhalten.", "task uninstall"),
    ]),
    ("Update", [
        ("Nach Update suchen", "Fragt GitHub nach der neuesten Version. Es wird nichts heruntergeladen.", "update check"),
        ("Update installieren", "Lädt nach Rückfrage die neuen Programmdateien, prüft SHA-256 und tauscht sie aus.", "update install"),
        ("Ohne Rückfrage installieren", "Wie oben, die Bestätigung wird vorab erteilt.", "update install --yes"),
        ("Update zurücknehmen", "Stellt die vorherige Programmversion wieder her.", "update rollback"),
    ]),
]


def full_command(arguments: str) -> str:
    return f"{EXE} {arguments}"


def all_examples() -> list[str]:
    return [arguments for _, items in SECTIONS for _, _, arguments in items]


def split_arguments(arguments: str) -> list[str]:
    """Zerlegt ein Beispiel in Argumente (nur für Tests; Backslashes werden dafür durch / ersetzt)."""
    return shlex.split(arguments.replace("\\", "/"))
