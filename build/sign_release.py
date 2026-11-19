"""Signatur der Release-Pruefsummen (Ed25519), damit der Updater Veroeffentlichungen pruefen kann.

    python build\\sign_release.py keygen          neues Schluesselpaar anlegen (nur einmal; die Datei wird nie ueberschrieben)
    python build\\sign_release.py public          oeffentlichen Schluessel anzeigen (steht fest im Programm: updater.RELEASE_PUBLIC_KEYS)
    python build\\sign_release.py sign DATEI      schreibt DATEI.sig (Base64 der 64-Byte-Signatur)

Der geheime Schluessel liegt ausserhalb des Repositorys: %USERPROFILE%\\.superpybackup\\release-signing.key
(oder der Pfad aus der Umgebungsvariable SUPERPYBACKUP_SIGNING_KEY). Er wird nie ausgegeben und nie veroeffentlicht;
die Datei bekommt nur Zugriffsrechte fuer den eigenen Benutzer. Gehen Schluessel und alle Sicherungen davon verloren, kann
keine Version mehr veroeffentlicht werden, die bestehende Installationen akzeptieren - der Schluessel gehoert in eine Datensicherung.
"""

from __future__ import annotations

import base64
import getpass
import os
import secrets
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from superpybackup import ed25519  # noqa: E402


def key_path() -> Path:
    override = os.environ.get("SUPERPYBACKUP_SIGNING_KEY")
    return Path(override) if override else Path.home() / ".superpybackup" / "release-signing.key"


def load_seed(path: Path | None = None) -> bytes:
    path = path or key_path()
    try:
        seed = bytes.fromhex(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError) as error:
        raise SystemExit(f"Signaturschluessel nicht lesbar ({path}): {error}") from error
    if len(seed) != 32:
        raise SystemExit(f"Signaturschluessel hat nicht 32 Bytes: {path}")
    return seed


def keygen(path: Path | None = None) -> str:
    path = path or key_path()
    if path.exists():
        raise SystemExit(f"Es gibt schon einen Schluessel: {path} (wird nie ueberschrieben).")
    path.parent.mkdir(parents=True, exist_ok=True)
    seed = secrets.token_bytes(32)
    with open(path, "x", encoding="ascii") as handle:
        handle.write(seed.hex() + "\n")
    if os.name == "nt":  # nur der eigene Benutzer darf lesen
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{getpass.getuser()}:F"], capture_output=True, check=False)
    return ed25519.public_key(seed).hex()


def sign_file(target: Path, path: Path | None = None) -> Path:
    seed = load_seed(path)
    signature = ed25519.sign(seed, target.read_bytes())
    out = target.with_name(target.name + ".sig")
    out.write_text(base64.b64encode(signature).decode("ascii") + "\n", encoding="ascii")
    if not ed25519.verify(ed25519.public_key(seed), target.read_bytes(), signature):  # Selbstpruefung
        raise SystemExit("Interner Fehler: die erzeugte Signatur besteht die eigene Pruefung nicht.")
    return out


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in ("keygen", "public", "sign"):
        print(__doc__)
        return 2
    if argv[0] == "keygen":
        print("Oeffentlicher Schluessel (in updater.RELEASE_PUBLIC_KEYS eintragen):\n" + keygen())
        print(f"Geheimer Schluessel gespeichert: {key_path()}  (nicht weitergeben; Datensicherung anlegen!)")
    elif argv[0] == "public":
        print(ed25519.public_key(load_seed()).hex())
    else:
        if len(argv) != 2:
            print("Aufruf: sign DATEI")
            return 2
        print(f"Signatur geschrieben: {sign_file(Path(argv[1]))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
