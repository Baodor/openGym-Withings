#!/usr/bin/env python3
"""Safe, explicit persistent storage lifecycle for openGym-Withings.

Uses only the Python standard library; NEVER prints access tokens or keys.
Runtime has no permission to bootstrap or adopt storage.
"""
from __future__ import annotations
import argparse
import base64
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import sys
import tarfile
import tempfile
from datetime import datetime, timezone

REQUIRED = ("bridge.db", "fernet.key", "session.key", "setup.key")
TABLES = {"settings", "measures", "logs"}
SCHEMA = """
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE measures (group_id TEXT PRIMARY KEY, timestamp INTEGER NOT NULL,
    day TEXT NOT NULL, details TEXT NOT NULL);
CREATE TABLE logs (id INTEGER PRIMARY KEY AUTOINCREMENT, stamp TEXT DEFAULT CURRENT_TIMESTAMP,
    level TEXT NOT NULL, message TEXT NOT NULL);
INSERT INTO settings VALUES('interval_minutes', '60');
INSERT INTO settings VALUES('prefer_withings', 'true');
INSERT INTO settings VALUES('sync_enabled', 'false');
"""

class StorageError(RuntimeError):
    pass

def read_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise StorageError(f"Fehlende .env: {path}")
    result = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip().strip("'\"")
    return result

def set_env_id(path: Path, instance_id: str):
    original = path.read_text()
    if "BRIDGE_STORAGE_ID" in read_env(path):
        raise StorageError("BRIDGE_STORAGE_ID existiert bereits; wird nicht überschrieben.")
    contents = original.rstrip("\n") + "\nBRIDGE_STORAGE_ID=" + instance_id + "\n"
    tmp = path.with_name(path.name + ".storage-new")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as file:
            file.write(contents)
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp, path)
        path.chmod(0o600)
    finally:
        tmp.unlink(missing_ok=True)

def inspect(data: Path):
    if not data.is_dir():
        raise StorageError(f"Datenspeicher existiert nicht: {data}. Nicht automatisch neu erstellen!")
    missing = [name for name in REQUIRED if not (data / name).is_file()]
    if missing:
        raise StorageError(
            "Unvollständiger Datenspeicher: " + ", ".join(missing) +
            ". Möglicherweise falscher Mount; alte Dateien/Backups suchen."
        )
    try:
        raw = base64.urlsafe_b64decode((data / "fernet.key").read_bytes())
        if len(raw) != 32:
            raise ValueError("Schlüssellänge")
        for name in ("session.key", "setup.key"):
            if not (data / name).read_text().strip():
                raise ValueError(f"Leere Datei: {name}")
        with sqlite3.connect(f"file:{data / 'bridge.db'}?mode=ro", uri=True) as db:
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("SQLite beschädigt")
            names = {x[0] for x in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not TABLES.issubset(names):
                raise ValueError("Tabellen fehlen")
            keys = {k: v for k, v in db.execute("SELECT key, value FROM settings")}
    except (sqlite3.DatabaseError, ValueError, OSError) as exc:
        raise StorageError(f"Bridge-Speicher unvollständig oder beschädigt: {type(exc).__name__}") from exc
    return keys

def verify(data: Path, env: Path):
    keys = inspect(data)
    marker = data / "storage.id"
    expected = read_env(env).get("BRIDGE_STORAGE_ID", "")
    if not expected:
        raise StorageError(
            "Keine BRIDGE_STORAGE_ID in .env. Vorhandene Installation zuerst "
            "mit 'adopt' bewusst registrieren; KEIN erneutes Setup."
        )
    if not marker.is_file() or marker.read_text().strip() != expected:
        raise StorageError(
            "Speicherkennung fehlt oder stimmt nicht überein. "
            "Falscher/leerer Mount oder Datenverlust – Container NICHT starten."
        )
    return keys

def create_private_file(path: Path, data: bytes):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())

def initialize(data: Path, env: Path):
    config = read_env(env)
    if config.get("BRIDGE_STORAGE_ID"):
        raise StorageError("Bereits registrierte Installation. Niemals erneut initialisieren.")
    if data.exists() and any(data.iterdir()):
        raise StorageError(
            "Datenspeicher ist nicht leer. 'init' DARF keine Bestandsdateien verändern. "
            "Bei bestehender Installation zuerst 'adopt' oder Backup-Wiederherstellung."
        )
    data.mkdir(parents=True, exist_ok=True)
    data.chmod(0o700)
    ident = secrets.token_hex(20)
    create_private_file(data / "fernet.key", base64.urlsafe_b64encode(os.urandom(32)))
    create_private_file(data / "session.key", secrets.token_urlsafe(48).encode())
    create_private_file(data / "setup.key", secrets.token_urlsafe(24).encode())
    db = sqlite3.connect(data / "bridge.db")
    try:
        db.executescript(SCHEMA)
        db.commit()
    finally:
        db.close()
    (data / "bridge.db").chmod(0o600)
    create_private_file(data / "storage.id", (ident + "\n").encode("ascii"))
    set_env_id(env, ident)
    verify(data, env)
    print("Neuer persistenter Speicher explizit eingerichtet. Schlüssel werden nicht ausgegeben.")

def adopt(data: Path, env: Path):
    """Enroll ONLY a previously configured legacy installation, never an empty reset."""
    config = read_env(env)
    if config.get("BRIDGE_STORAGE_ID") or (data / "storage.id").exists():
        raise StorageError("Speicherkennung bereits vorhanden. 'verify' verwenden; nicht überschreiben.")
    settings = inspect(data)
    if not settings.get("oidc_client_id") or not settings.get("secret_oidc_client_secret"):
        raise StorageError(
            "Keine vollständige Pocket-ID-Konfiguration in bridge.db gefunden. "
            "Möglicherweise ist bereits ein falscher/leerer Speicher aktiv. "
            "Zuerst alte Daten/Backups suchen; NICHT automatisch übernehmen."
        )
    ident = secrets.token_hex(20)
    create_private_file(data / "storage.id", (ident + "\n").encode("ascii"))
    set_env_id(env, ident)
    verify(data, env)
    print("Bestehenden konfigurierten Speicher registriert. Keine Zugangsdaten geändert.")

def backup(data: Path, env: Path, destination: Path):
    verify(data, env)
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.chmod(0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"opengym-withings-{stamp}-{secrets.token_hex(4)}.tar.gz"
    work = Path(tempfile.mkdtemp(prefix=".ogw-snapshot-", dir=destination))
    try:
        with sqlite3.connect(f"file:{data / 'bridge.db'}?mode=ro", uri=True) as src:
            with sqlite3.connect(work / "bridge.db") as dst:
                src.backup(dst)
        (work / "bridge.db").chmod(0o600)
        for filename in ("fernet.key", "session.key", "setup.key", "storage.id"):
            shutil.copyfile(data / filename, work / filename)
            (work / filename).chmod(0o600)
        shutil.copyfile(env, work / "dotenv.backup")
        (work / "dotenv.backup").chmod(0o600)
        temp_archive = destination / ("." + name + ".incomplete")
        with tarfile.open(temp_archive, mode="w:gz") as tar:
            for filename in ("bridge.db", "fernet.key", "session.key", "setup.key", "storage.id", "dotenv.backup"):
                tar.add(work / filename, arcname=filename)
        temp_archive.chmod(0o600)
        os.replace(temp_archive, destination / name)
        print(f"Verifiziertes Backup erstellt: {destination / name}")
    finally:
        shutil.rmtree(work)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("verify", "adopt", "init", "backup"))
    parser.add_argument("--data", type=Path, default=Path("/mnt/docker/appdata/opengym-withings"))
    parser.add_argument("--env", type=Path, default=Path(".env"))
    parser.add_argument("--backup-dir", type=Path, default=Path("/mnt/docker/backups/opengym-withings"))
    args = parser.parse_args()
    data = args.data.expanduser().absolute()
    env = args.env.expanduser().absolute()
    if not data.is_absolute() or data == Path("/") or data == env.parent:
        raise StorageError("Unsicherer Speicherpfad")
    if args.action == "init":
        initialize(data, env)
    elif args.action == "adopt":
        adopt(data, env)
    elif args.action == "verify":
        settings = verify(data, env)
        print("Speicherkennung, SQLite und Schlüsseldateien vorhanden.")
        print("Pocket-ID eingerichtet:", bool(settings.get("oidc_client_id") and settings.get("secret_oidc_client_secret")))
        print("Withings eingerichtet:", bool(settings.get("withings_client_id") and settings.get("secret_withings_client_secret")))
        print("Withings verbunden:", bool(settings.get("secret_withings_token")))
        print("openGym gekoppelt:", bool(settings.get("secret_opengym_token")))
    elif args.action == "backup":
        backup(data, env, args.backup_dir)

if __name__ == "__main__":
    try:
        main()
    except (StorageError, OSError) as exc:
        print("ABBRUCH:", exc, file=sys.stderr)
        sys.exit(2)
