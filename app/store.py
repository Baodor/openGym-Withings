"""Encrypted SQLite settings, account tokens and measurement history."""
import json
import os
import secrets
import sqlite3
from pathlib import Path
from cryptography.fernet import Fernet

DATA = Path(os.environ.get("DATA_DIR", "/data"))

def prepare() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    if not (DATA / "session.key").exists():
        (DATA / "session.key").write_text(secrets.token_urlsafe(48))
        (DATA / "session.key").chmod(0o600)
    if not (DATA / "fernet.key").exists():
        (DATA / "fernet.key").write_bytes(Fernet.generate_key())
        (DATA / "fernet.key").chmod(0o600)
    if not (DATA / "setup.key").exists():
        (DATA / "setup.key").write_text(secrets.token_urlsafe(24))
        (DATA / "setup.key").chmod(0o600)
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS measures (group_id TEXT PRIMARY KEY, timestamp INTEGER NOT NULL,
            day TEXT NOT NULL, details TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS logs (id INTEGER PRIMARY KEY AUTOINCREMENT, stamp TEXT DEFAULT CURRENT_TIMESTAMP,
            level TEXT NOT NULL, message TEXT NOT NULL);
        """)
        con.execute("INSERT OR IGNORE INTO settings VALUES ('interval_minutes','60')")
        con.execute("INSERT OR IGNORE INTO settings VALUES ('prefer_withings','true')")
        con.execute("INSERT OR IGNORE INTO settings VALUES ('sync_enabled','false')")

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
