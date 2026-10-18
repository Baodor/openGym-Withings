"""Encrypted SQLite settings, account tokens and measurement history."""
import json
import os
import secrets
import sqlite3
from pathlib import Path
from cryptography.fernet import Fernet

DATA = Path(os.environ.get("DATA_DIR", "/data"))

def prepare() -> None:
    """Fail closed if the persistent state is missing, moved or corrupted.

    Runtime never bootstraps an empty database or generates replacement keys.
    A dedicated, explicit storage initialization/adoption is required.
    """
    expected = os.environ.get("BRIDGE_STORAGE_ID", "").strip()
    if not expected:
        raise RuntimeError(
            "BRIDGE_STORAGE_ID fehlt. Laufende Installation NICHT neu einrichten; "
            "zuerst persistenten Speicher prüfen und scripts/storage_admin.py verwenden."
        )
    if not DATA.is_dir():
        raise RuntimeError(f"Persistenter Speicher fehlt: {DATA}; Start aus Sicherheitsgründen verweigert.")
    required = ("storage.id", "bridge.db", "fernet.key", "session.key", "setup.key")
    missing = [name for name in required if not (DATA / name).is_file()]
    if missing:
        raise RuntimeError(
            "Persistenter Speicher unvollständig (" + ", ".join(missing) +
            "). Kein automatisches Zurücksetzen; bestehendes Backup/Mount prüfen."
        )
    actual = (DATA / "storage.id").read_text(encoding="ascii").strip()
    if actual != expected:
        raise RuntimeError("BRIDGE_STORAGE_ID stimmt nicht mit dem gespeicherten Datenspeicher überein. Start verweigert.")
    try:
        cipher = Fernet((DATA / "fernet.key").read_bytes())
        if not (DATA / "session.key").read_text().strip():
            raise ValueError("empty session.key")
        if not (DATA / "setup.key").read_text().strip():
            raise ValueError("empty setup.key")
        con = sqlite3.connect(f"file:{DATA / 'bridge.db'}?mode=ro", uri=True, timeout=15)
        try:
            if con.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("SQLite quick_check failed")
            names = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            if not {"settings", "measures", "logs"}.issubset(names):
                raise ValueError("missing required tables")
            for (encrypted,) in con.execute(
                "SELECT value FROM settings WHERE key LIKE 'secret_%' AND value <> ''"
            ):
                cipher.decrypt(encrypted.encode("utf-8"))
        finally:
            con.close()
    except Exception as exc:
        raise RuntimeError(
            "Bridge-Datenbank oder Verschlüsselungsschlüssel beschädigt/inkonsistent; "
            "Start verweigert, bitte Backup prüfen."
        ) from exc


def db():
    con = sqlite3.connect(DATA / "bridge.db", timeout=15)
    con.row_factory = sqlite3.Row
    return con

def get(key, default=""):
    with db() as con:
        item = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return item[0] if item else default

def put(key, value):
    with db() as con:
        con.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

def encrypt(value):
    return Fernet((DATA / "fernet.key").read_bytes()).encrypt(value.encode()).decode()

def decrypt(value):
    return Fernet((DATA / "fernet.key").read_bytes()).decrypt(value.encode()).decode()

def secret_set(key, value):
    put("secret_"+key, encrypt(value))

def secret_get(key):
    v = get("secret_"+key)
    return decrypt(v) if v else ""

def log(level, message):
    with db() as con:
        con.execute("INSERT INTO logs(level,message) VALUES(?,?)", (level, str(message)[:500]))
        con.execute("DELETE FROM logs WHERE id NOT IN (SELECT id FROM logs ORDER BY id DESC LIMIT 500)")

def recent_logs(limit=35):
    with db() as con:
        return [dict(r) for r in con.execute("SELECT stamp,level,message FROM logs ORDER BY id DESC LIMIT ?", (limit,))]

def save_measures(rows):
    with db() as con:
        for row in rows:
            con.execute("INSERT OR REPLACE INTO measures VALUES(?,?,?,?)", (row["group_id"], row["timestamp"], row["day"], json.dumps(row)))

def measures():
    with db() as con:
        return [json.loads(r[0]) for r in con.execute("SELECT details FROM measures ORDER BY timestamp")]
