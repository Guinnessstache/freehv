"""FreeHV authentication.

Single-admin auth suitable for an appliance: one hashed password plus a
persisted Flask secret key. On first run, the password comes from
FREEHV_ADMIN_PASSWORD, or a strong random one is generated and printed to the
console (the appliance prints its initial login on first boot). The password
is stored only as a salted PBKDF2 hash.

Set FREEHV_AUTH=off to disable auth entirely — DEV ONLY, never on a network.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash


def _config_dir() -> Path:
    candidate = Path(os.environ.get("FREEHV_CONFIG_DIR", "/var/lib/freehv"))
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        probe = candidate / ".write-test"
        probe.write_text("ok")
        probe.unlink()
        return candidate
    except OSError:
        fallback = Path.home() / ".freehv"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


class Auth:
    def __init__(self) -> None:
        self.enabled = os.environ.get("FREEHV_AUTH", "on").lower() not in (
            "off", "0", "false", "no")
        self._path = _config_dir() / "auth.json"
        self._data = self._load_or_init()

    def _load_or_init(self) -> dict:
        if self._path.exists():
            try:
                return json.loads(self._path.read_text())
            except (json.JSONDecodeError, OSError):
                pass  # corrupt — re-init below

        password = os.environ.get("FREEHV_ADMIN_PASSWORD")
        generated = password is None
        if generated:
            password = secrets.token_urlsafe(12)

        data = {
            "secret_key": secrets.token_hex(32),
            "password_hash": generate_password_hash(password),
        }
        self._write(data)

        if generated:
            line = "=" * 64
            print(f"\n{line}", file=sys.stderr)
            print(f"[freehv] Initial admin password:  {password}", file=sys.stderr)
            print(f"[freehv] Stored hashed at {self._path}. Change it in the UI.",
                  file=sys.stderr)
            print(f"{line}\n", file=sys.stderr)
        return data

    def _write(self, data: dict) -> None:
        self._path.write_text(json.dumps(data))
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass
        self._data = data

    @property
    def secret_key(self) -> str:
        return self._data["secret_key"]

    def verify(self, password: str | None) -> bool:
        return check_password_hash(self._data["password_hash"], password or "")

    def set_password(self, new_password: str) -> None:
        data = dict(self._data)
        data["password_hash"] = generate_password_hash(new_password)
        self._write(data)
