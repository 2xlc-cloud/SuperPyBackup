# Fuehrt einen Sicherungslauf der PowerShell-Referenz (SuperPSBackup) fuer den Benchmark aus.
# Das Skript wird als Kopie aus dem Arbeitsordner geladen; das Originalprojekt wird nicht veraendert.
param(
    [Parameter(Mandatory)][string]$Script,
    [Parameter(Mandatory)][string]$BackupRoot,
    [Parameter(Mandatory)][string]$Source,
    [Parameter(Mandatory)][string]$LogRoot
)
$ErrorActionPreference = 'Stop'
# Werte vorab sichern: Der param-Block der dot-geladenen Referenz ueberschreibt gleichnamige Variablen (z. B. $BackupRoot).
$benchTarget = $BackupRoot; $benchSource = $Source; $benchLogs = $LogRoot
. $Script
# -ForceSnapshot: jeder Lauf legt einen neuen Stand an (wie new_snapshot in SuperPyBackup), auch bei mehreren Laeufen pro Tag.
$result = Invoke-UserProfileBackup -BackupRoot $benchTarget -LogRoot $benchLogs -SourceFolders ([ordered]@{ Test = $benchSource }) -ForceSnapshot
$result | ConvertTo-Json -Compress
if ($result.Status -ne 'Complete') { exit 1 }
exit 0
