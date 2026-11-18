# PowerShell (SuperPSBackup 1.1.0) und Python (SuperPyBackup 1.0.0) im Vergleich

Stand: 2026-11-18. Alle Messwerte stammen unverändert aus `bench/results/results.json` (erzeugt mit `bench/benchmark.py`, Tabellen mit `bench/render.py`). Es wurden keine Werte von Hand eingetragen oder geschätzt.

## 1. Umfang der Messung – bitte zuerst lesen

Der Benchmark wurde vorzeitig beendet und **nicht** wiederholt. Daher gilt:

| Szenario | Geplant | Tatsächlich gemessen |
|---|---|---|
| 1.000 kleine Dateien | 3 Durchläufe | 3 Durchläufe (vollständig) |
| 10.000 kleine Dateien | 3 Durchläufe | **2** Durchläufe (Median aus 2 Werten) |
| 100 größere Dateien (je 2 MiB) | 3 Durchläufe | **nicht gemessen** |

* Aussagen über große Dateien (Durchsatz, Hash-Kosten bei vielen MB) sind **nicht belegt**.
* Mit nur 2–3 Durchläufen sind Mediane und Spannen Richtwerte, keine Statistik. Einzelne Spannen sind groß (z. B. PowerShell, 10.000 Dateien, Erstlauf: 91–139 s).
* Von 60 Messläufen schlug einer fehl: PowerShell, 1.000 Dateien, Durchlauf 1, Schritt „10 % verändert“ (Exit-Code 1, 1 Dateifehler, 3,7 s). Er ist in der Spanne der Tabelle enthalten; die Ursache wurde nicht untersucht.
* Ein früherer, kontaminierter Messlauf wurde verworfen und ist nicht Teil dieser Zahlen.
* Gemessen wurde in einer virtuellen Maschine mit aktivem Defender-Echtzeitschutz. Absolute Zeiten sind auf anderer Hardware nicht übertragbar; das Verhältnis der Programme zueinander ist aussagekräftiger als die Sekundenwerte.
* „Python (Metadaten)“ verwendet die Standardeinstellung, „Python (Hash)“ `change_detection: "hash"` (entspricht der Arbeitsweise der PowerShell-Referenz).
* Der Inhaltsvergleich der jeweils neuesten Stände (Durchlauf 1) ergab bei allen drei Varianten identische Dateiinhalte.

## 2. Messergebnisse

Die EXE ist eine Ein-Datei-EXE, die sich beim Start entpackt; dieser Aufwand (Median 2,18 s gegenüber 0,17 s bei PowerShell) steckt in jeder Laufzeit. Die Spalte „ohne Programmstart“ zieht ihn ab.

**Messumgebung:** Intel(R) Core(TM) i7-14700T (12 logische Kerne), 11.8 GB RAM, Datenträger: Red Hat VirtIO (Unspecified), Windows-11-10.0.26300-SP0, Windows PowerShell 5.1.26100.9818, Python 3.13.17, Defender-Echtzeitschutz: True.  
**Durchläufe je Szenario:** 3 (Median; in Klammern Minimum–Maximum der Laufzeit). Programmstart (Median aus 5): PowerShell 0.17 s, SuperPyBackup-EXE 2.18 s.

**Fehlgeschlagene Läufe:** 1 von 60.

**Inhaltsvergleich** der jeweils neuesten Stände (Durchlauf 1, alle Schritte): identisch bei allen Programmen.

### 1.000 kleine Dateien (je 4 KiB)

| Schritt | Programm | Laufzeit s (min–max) | davon ohne Programmstart s | CPU-Zeit s | Spitzen-Speicher MB | Mehrverbrauch im Ziel MB | E/A-Operationen (Lesen+Schreiben+Sonst.) | E/A-Daten MB (Lesen / Schreiben) |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| Erster Lauf (vollständig) | PowerShell | 10.54 (8.43–16.70) | 10.37 | 7.73 | 131.7 | 4.83 | 80,847 | 17.0 / 4.8 |
| Erster Lauf (vollständig) | Python (Metadaten) | 5.96 (5.66–10.82) | 3.78 | 4.59 | 23.8 | 4.35 | 111,779 | 20.0 / 28.5 |
| Erster Lauf (vollständig) | Python (Hash) | 8.66 (5.87–11.29) | 6.48 | 4.53 | 23.6 | 4.35 | 111,782 | 20.0 / 28.5 |
| Zweiter Lauf, unverändert | PowerShell | 4.70 (4.57–4.76) | 4.53 | 4.98 | 116.2 | 0.31 | 33,572 | 10.4 / 0.3 |
| Zweiter Lauf, unverändert | Python (Metadaten) | 2.90 (2.83–4.58) | 0.72 | 1.88 | 26.0 | 0.15 | 46,159 | 16.9 / 24.3 |
| Zweiter Lauf, unverändert | Python (Hash) | 3.71 (3.48–4.04) | 1.53 | 2.61 | 26.0 | 0.15 | 54,162 | 20.9 / 24.3 |
| 10 % verändert | PowerShell | 5.66 (3.75–9.03) | 5.49 | 5.25 | 116.1 | 0.70 | 33,097 | 10.8 / 0.7 |
| 10 % verändert | Python (Metadaten) | 3.55 (2.89–4.29) | 1.37 | 2.06 | 26.5 | 0.54 | 48,931 | 18.0 / 24.7 |
| 10 % verändert | Python (Hash) | 4.01 (3.58–4.64) | 1.83 | 2.70 | 27.8 | 0.54 | 56,134 | 21.5 / 24.7 |
| 10 % gelöscht | PowerShell | 4.81 (4.55–4.97) | 4.64 | 5.12 | 119.6 | 0.28 | 30,958 | 10.2 / 0.3 |
| 10 % gelöscht | Python (Metadaten) | 2.78 (2.71–2.92) | 0.60 | 1.89 | 28.5 | 0.13 | 45,397 | 18.1 / 24.3 |
| 10 % gelöscht | Python (Hash) | 3.27 (3.26–3.32) | 1.09 | 2.42 | 28.8 | 0.13 | 52,603 | 21.7 / 24.3 |

Summe der Median-Laufzeiten aller vier Schritte: PowerShell **25.7 s**, Python (Metadaten) **15.2 s** (1.7× schneller als PowerShell), Python (Hash) **19.6 s** (1.3× schneller als PowerShell).

### 10.000 kleine Dateien (je 4 KiB)

| Schritt | Programm | Laufzeit s (min–max) | davon ohne Programmstart s | CPU-Zeit s | Spitzen-Speicher MB | Mehrverbrauch im Ziel MB | E/A-Operationen (Lesen+Schreiben+Sonst.) | E/A-Daten MB (Lesen / Schreiben) |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| Erster Lauf (vollständig) | PowerShell | 115.05 (91.48–138.61) | 114.87 | 76.36 | 307.2 | 48.26 | 758,720 | 165.9 / 48.3 |
| Erster Lauf (vollständig) | Python (Metadaten) | 75.42 (50.75–100.09) | 73.25 | 33.70 | 47.5 | 43.50 | 781,424 | 60.5 / 67.6 |
| Erster Lauf (vollständig) | Python (Hash) | 47.11 (47.01–47.20) | 44.93 | 36.80 | 47.3 | 43.50 | 781,462 | 60.5 / 67.6 |
| Zweiter Lauf, unverändert | PowerShell | 43.92 (42.51–45.34) | 43.75 | 41.22 | 322.1 | 3.07 | 294,687 | 100.1 / 3.1 |
| Zweiter Lauf, unverändert | Python (Metadaten) | 8.46 (8.33–8.58) | 6.28 | 5.91 | 60.7 | 1.48 | 128,816 | 30.4 / 25.6 |
| Zweiter Lauf, unverändert | Python (Hash) | 15.65 (13.73–17.57) | 13.47 | 12.30 | 61.2 | 1.48 | 208,852 | 69.3 / 25.6 |
| 10 % verändert | PowerShell | 47.43 (45.68–49.18) | 47.26 | 42.23 | 364.9 | 6.97 | 289,496 | 103.1 / 7.0 |
| 10 % verändert | Python (Metadaten) | 15.49 (13.02–17.97) | 13.32 | 7.88 | 72.6 | 5.39 | 155,892 | 40.2 / 29.5 |
| 10 % verändert | Python (Hash) | 16.53 (15.43–17.63) | 14.35 | 13.81 | 72.9 | 5.39 | 227,885 | 75.3 / 29.5 |
| 10 % gelöscht | PowerShell | 43.72 (42.87–44.56) | 43.55 | 42.31 | 350.7 | 2.76 | 268,090 | 98.1 / 2.8 |
| 10 % gelöscht | Python (Metadaten) | 8.71 (8.33–9.09) | 6.53 | 6.52 | 81.6 | 1.33 | 119,950 | 41.9 / 25.5 |
| 10 % gelöscht | Python (Hash) | 13.38 (13.29–13.47) | 11.20 | 12.02 | 81.2 | 1.33 | 191,986 | 77.0 / 25.5 |

Summe der Median-Laufzeiten aller vier Schritte: PowerShell **250.1 s**, Python (Metadaten) **108.1 s** (2.3× schneller als PowerShell), Python (Hash) **92.7 s** (2.7× schneller als PowerShell).



## 3. Auswertung der Messwerte

Alle Aussagen gelten nur für die gemessenen Szenarien.

* **Laufzeit, 1.000 Dateien:** Summe aller vier Schritte: Python (Metadaten) 1,7-mal, Python (Hash) 1,3-mal schneller als PowerShell. Beim Erstlauf wirkt der EXE-Start (2,2 s) deutlich.
* **Laufzeit, 10.000 Dateien:** 2,3-fach (Metadaten) bzw. 2,7-fach (Hash) schneller in der Summe. Bei unveränderten Daten: PowerShell 43,9 s, Python (Metadaten) 8,5 s, Python (Hash) 15,7 s.
* **Speicher:** PowerShell 116–365 MB Spitze, Python 24–82 MB. Bei 10.000 Dateien braucht Python rund 15–25 % des PowerShell-Speichers.
* **CPU-Zeit:** Bei 10.000 Dateien in Folgeläufen PowerShell rund 41–42 s, Python (Metadaten) 6–8 s, Python (Hash) 12–14 s.
* **E/A:** Python schreibt laut Messung auch bei unveränderten Läufen 24–29 MB, PowerShell nur 0,3–7 MB. Die Ursache wurde nicht untersucht. Lesemengen und Operationszahlen unterscheiden sich je nach Schritt in beide Richtungen (siehe Tabellen).
* **Speicherplatz im Ziel:** Beide Programme verbrauchen etwa gleich viel (Hardlinks); Python liegt im Erstlauf rund 10 % darunter.
* **Hash-Modus:** Bei 10.000 unveränderten Dateien kostet `change_detection: "hash"` etwa das Doppelte von Metadaten (15,7 s gegenüber 8,5 s), bleibt aber klar unter PowerShell. Beim Erstlauf mit 10.000 Dateien lag Hash (47,1 s, zwei eng beieinander liegende Werte) unter Metadaten (75,4 s, Spanne 50,8–100,1 s). Das ist ein Streuungseffekt der Messung und **kein** Beleg, dass Hash schneller wäre.

## 4. Funktionsvergleich

Die vollständige Zuordnung steht in `docs/FUNKTIONSUEBERSICHT.md`. Kurzfassung:

| Bereich | SuperPSBackup 1.1.0 | SuperPyBackup 1.0.0 |
|---|---|---|
| Inkrementelle Snapshots per Hardlink, Staging + atomares Umbenennen, Manifest mit SHA-256 | ja | ja (Manifest Format 2) |
| Aufbewahrung 7 Tage / 12 Monate / 5 Jahre | ja | ja |
| Wiederherstellung (nie stilles Überschreiben, Hashprüfung) | ja | ja, zusätzlich Einzeldateien/Unterordner |
| Fehler bei einzelner Datei | verwirft den ganzen Lauf | Teil-Stand (`Partial`), nie Basis, keine Rotation, Exit-Code 2 |
| Konfigurationsdatei, Ausschlussmuster, eigene Quellen | nein | ja (`config.json`) |
| Speicherplatz-Vorabprüfung | nein | ja |
| Lange Pfade, Online-only-Dateien (OneDrive) | nicht gezielt behandelt | behandelt |
| Sicherungsdateien schreibgeschützt (Hardlink-Schutz) | nein | ja |
| Zusätzliche Befehle | – | `list`, `verify`, `config`, `task status`, `update rollback` |
| Windows-Aufgabe (Anmeldung + 2 min, versteckt, ohne Adminrechte) | ja | ja (Name `SuperPyBackup-<SID>`) |
| Selbstupdate (SHA-256, Rollback) | ja | ja, aus GitHub-Releases, nur nach Bestätigung |
| GUI | ja | ja (tkinter, Dunkelmodus als Standard, umschaltbar) |
| Installation | `FirstInstall.ps1` mit Systemcheck und Git-Nachinstallation | EXE kopieren, `task install` |

Die PowerShell-Version ist in einem Punkt strenger: Bei Dateifehlern legt sie bewusst keinen Stand an. Das ist konsequent „nur vollständige Stände“, aber verlustreicher.

## 5. Installationsaufwand und Laufzeitumgebung

* **PowerShell:** Läuft mit dem unter Windows vorhandenen Windows PowerShell 5.1, ohne Kompilierung. Skripte sind im Klartext prüfbar. Ausführungsrichtlinien können die Ausführung einschränken.
* **Python:** Zwei eigenständige EXE-Dateien (je rund 11,5 MB, SHA-256 in `SHA256SUMS.txt`), keine Python-Installation nötig (per Test mit bereinigtem `PATH` belegt). Nachteile: Die EXE ist **nicht code-signiert**; Defender/SmartScreen kann bei unbekannten PyInstaller-EXEs warnen; der Start dauert gemessen rund 2 s länger.

## 6. Fehlerbehandlung und Testabdeckung

* SuperPyBackup hat 187 automatisierte Tests (Backup-Kern, Aufbewahrung, Abbruch-/Kill-/Parallel-/Defekt-Szenarien, Wiederherstellung, Konfiguration, echte Aufgabenplanung mit Testnamen, Updater gegen lokalen Fake-GitHub-Server, EXE ohne Python, GUI). Alle bestanden im letzten Lauf.
* Die Tests von SuperPSBackup wurden für diesen Vergleich **nicht erneut ausgeführt**; maßgeblich ist deren eigene Dokumentation im PowerShell-Projekt.
* Beide Programme sperren gegen Parallelläufe (Mutex + Lockdatei) und löschen keine defekten Ordner automatisch.
* SuperPyBackup protokolliert Fehler auch ohne Konsole in `errors.log`/`crash.log`.

## 7. Wartbarkeit

* **Python:** Modulare Struktur (`engine`, `retention`, `restore`, `scheduler`, `updater`, `gui` …), automatisierte Tests, reproduzierbarer Build (`build/build.py`), zur Laufzeit nur Standardbibliothek. Aufwand: PyInstaller und Python-Version pflegen; jede Änderung braucht einen neuen Release.
* **PowerShell:** Wenige größere Skripte, direkt editierbar, kein Build. Windows-spezifische Dinge sind nativ erreichbar. Große Skripte sind schwerer isoliert zu testen.

## 8. Vor- und Nachteile

| | Vorteile | Nachteile |
|---|---|---|
| **SuperPSBackup** | keine Kompilierung, schneller Programmstart, im Klartext prüfbar, keine zusätzlichen Programmdateien | in den Messungen langsamer und speicherhungriger bei vielen Dateien, ein Dateifehler verwirft den Lauf, keine Konfigurationsdatei |
| **SuperPyBackup** | in den Messungen 1,3–2,7× schneller (Summe) und deutlich sparsamer im Speicher, Teil-Stände, Konfigurationsdatei, mehr Prüfungen und Befehle, GUI mit Dunkelmodus | rund 2 s Programmstart, nicht signierte EXE (SmartScreen/Defender-Hinweise möglich), ~11,5 MB je Datei, Update-Hash schützt vor Beschädigung, nicht vor kompromittiertem Repository |

## 9. Empfehlung

Für Datenbestände mit **mehreren tausend Dateien oder mehr** und für Umgebungen, in denen einzelne Dateien gesperrt sein können, empfehle ich **SuperPyBackup**: Bei 10.000 Dateien lag die Laufzeit in der Summe unter der Hälfte, der Speicherbedarf bei einem Bruchteil, und Teilfehler werden praxisnäher behandelt.

**SuperPSBackup** bleibt sinnvoll, wenn keine zusätzlichen, unsignierten Programmdateien erlaubt sind, wenn der Quelltext im Klartext prüfbar sein muss oder wenn der Bestand klein ist (bei 1.000 Dateien lag der Unterschied bei wenigen Sekunden je Lauf).

Diese Empfehlung stützt sich auf die oben genannten, unvollständigen Messungen. **Nicht belegt** sind: Verhalten bei großen Dateien, Verhalten auf physischer Hardware oder mit Netzlaufwerk-Ziel und die Ursache der beobachteten Streuung.
