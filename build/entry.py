"""Einstiegspunkt fuer PyInstaller (SuperPyBackup.exe und SuperPyBackupCmd.exe nutzen denselben Code)."""

from superpybackup.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
