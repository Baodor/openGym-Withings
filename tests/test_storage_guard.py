"""Regression tests: container rebuild must never erase or re-create credentials."""
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile

import pytest
from cryptography.fernet import Fernet
from app import store


ADMIN = Path(__file__).resolve().parents[1] / "scripts" / "storage_admin.py"
ROOT = Path(__file__).resolve().parents[1]


def admin(action, data, envfile, *args, success=True):
    result = subprocess.run(
        [sys.executable, str(ADMIN), action,
         "--data", str(data), "--env", str(envfile), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if success:
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode != 0
    return result


def configured(tmp_path, monkeypatch):
    data = tmp_path / "state"
    envfile = tmp_path / ".env"
    envfile.write_text("PUBLIC_URL=https://opengym-withings.home.glockb.de\n")
    admin("init", data, envfile)
    ident = next(
        line.split("=", 1)[1] for line in envfile.read_text().splitlines()
        if line.startswith("BRIDGE_STORAGE_ID=")
    )
    monkeypatch.setattr(store, "DATA", data)
    monkeypatch.setenv("BRIDGE_STORAGE_ID", ident)
    return data, envfile, ident


def test_repeated_start_preserves_keys_and_data(tmp_path, monkeypatch):
    data, envfile, _ = configured(tmp_path, monkeypatch)
    store.prepare()
    store.put("oidc_client_id", "existing-client")
    store.secret_set("oidc_client_secret", "sensitive-but-test-only")
    store.secret_set("withings_token", "saved-token")
    before = {f: (data / f).read_bytes()
              for f in ("fernet.key", "session.key", "setup.key", "storage.id")}
    for _ in range(3):
        store.prepare()  # container restart and forced recreate semantics
        assert store.get("oidc_client_id") == "existing-client"
        assert store.secret_get("oidc_client_secret") == "sensitive-but-test-only"
        assert store.secret_get("withings_token") == "saved-token"
    assert all((data / key).read_bytes() == blob for key, blob in before.items())
    assert admin("verify", data, envfile).returncode == 0


def test_lost_mount_is_fatal_not_new_install(tmp_path, monkeypatch):
    data, _, _ = configured(tmp_path, monkeypatch)
    store.prepare()
    moved = data.with_name("missing-dir")
    monkeypatch.setattr(store, "DATA", moved)
    with pytest.raises(RuntimeError, match="Persistenter Speicher fehlt"):
        store.prepare()
    assert not moved.exists()


def test_missing_key_never_regenerated(tmp_path, monkeypatch):
    data, _, _ = configured(tmp_path, monkeypatch)
    (data / "fernet.key").unlink()
    with pytest.raises(RuntimeError, match="unvollständig"):
        store.prepare()
    assert not (data / "fernet.key").exists()


def test_mismatched_mount_id_is_fatal(tmp_path, monkeypatch):
    data, _, _ = configured(tmp_path, monkeypatch)
    (data / "storage.id").write_text("someone-elses-installation\n")
    with pytest.raises(RuntimeError, match="stimmt nicht"):
        store.prepare()


def test_changed_encryption_key_does_not_reset_tokens(tmp_path, monkeypatch):
    data, _, _ = configured(tmp_path, monkeypatch)
    store.prepare()
    store.secret_set("withings_token", "existing-token")
    old_db = (data / "bridge.db").read_bytes()
    (data / "fernet.key").write_bytes(Fernet.generate_key())
    with pytest.raises(RuntimeError, match="inkonsistent"):
        store.prepare()
    assert (data / "bridge.db").read_bytes() == old_db


def test_empty_legacy_directory_cannot_be_adopted(tmp_path):
    data = tmp_path / "state"
    data.mkdir()
    env = tmp_path / ".env"
    env.write_text("PUBLIC_URL=https://local.example\n")
    result = admin("adopt", data, env, success=False)
    assert "ABBRUCH" in result.stderr
    assert "BRIDGE_STORAGE_ID" not in env.read_text()


def test_explicit_legacy_adoption_retains_older_secrets(tmp_path, monkeypatch):
    data, env, _ = configured(tmp_path, monkeypatch)
    store.prepare()
    store.put("oidc_client_id", "previous-id")
    store.secret_set("oidc_client_secret", "previous-secret")
    # Simulate the real pre-upgrade layout: complete DB and keys, no marker.
    (data / "storage.id").unlink()
    env.write_text("PUBLIC_URL=https://local.example\n")
    assert admin("adopt", data, env).returncode == 0
    new_id = next(s.split("=",1)[1] for s in env.read_text().splitlines()
                  if s.startswith("BRIDGE_STORAGE_ID="))
    monkeypatch.setenv("BRIDGE_STORAGE_ID", new_id)
    store.prepare()
    assert store.secret_get("oidc_client_secret") == "previous-secret"


def test_atomic_backup_has_matching_database_and_keys(tmp_path, monkeypatch):
    data, env, _ = configured(tmp_path, monkeypatch)
    store.prepare()
    store.secret_set("withings_token", "valuable")
    output = tmp_path / "backups"
    admin("backup", data, env, "--backup-dir", str(output))
    archives = list(output.glob("opengym-withings-*.tar.gz"))
    assert len(archives) == 1
    with tarfile.open(archives[0]) as archive:
        assert set(archive.getnames()) == {
            "bridge.db", "session.key", "fernet.key", "setup.key", "storage.id",
            "dotenv.backup"
        }
        assert archive.extractfile("fernet.key").read() == (data / "fernet.key").read_bytes()
        assert archive.extractfile("storage.id").read() == (data / "storage.id").read_bytes()


def test_compose_disallows_implicit_bind_directory_creation():
    compose = (ROOT / "compose.yaml").read_text()
    assert "create_host_path: false" in compose
    assert "BRIDGE_DATA_HOST_PATH" in compose
