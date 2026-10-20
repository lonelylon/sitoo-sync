"""Lagring av inställningar och inloggning i datamappen (/data i containern)."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
from pathlib import Path

from .config import FIELDS, Config, ConfigError, env_defaults, missing_required, parse_value

PBKDF2_ROUNDS = 240_000


def _write_atomic(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return {}


class SettingsStore:
    def __init__(self, data_dir: str | Path):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.settings_path = self.dir / "settings.json"
        self.auth_path = self.dir / "auth.json"
        self._lock = threading.RLock()
        self.version = 0
        self._values = self._load()

    # ------------------------------------------------------------ settings
    def _load(self) -> dict:
        stored = _read(self.settings_path)
        values = {f.key: f.default for f in FIELDS}
        values.update(env_defaults())
        for f in FIELDS:
            if f.key in stored:
                try:
                    values[f.key] = parse_value(f, stored[f.key])
                except ConfigError:
                    pass
        values["paused"] = bool(stored.get("paused", False))
        return values

    def values(self) -> dict:
        with self._lock:
            return dict(self._values)

    def update(self, form: dict) -> list[str]:
        """Sparar värden från formuläret. Tomma lösenordsfält behåller nuvarande värde.

        Returnerar en lista med fel; vid fel sparas ingenting.
        """
        with self._lock:
            new, errors = self.preview(form)
            if errors:
                return errors
            self._values = new
            self._save()
        return []

    def preview(self, form: dict) -> tuple[dict, list[str]]:
        """Nuvarande värden med formulärets ändringar, utan att spara."""
        errors: list[str] = []
        with self._lock:
            new = dict(self._values)
        for f in FIELDS:
            if f.kind == "bool":
                new[f.key] = parse_value(f, form.get(f.key, ""))
                continue
            if f.key not in form:
                continue
            if f.kind == "secret" and str(form[f.key]) == "":
                continue
            try:
                new[f.key] = parse_value(f, form[f.key])
            except ConfigError as err:
                errors.append(str(err))
        return new, errors

    def set_paused(self, paused: bool) -> None:
        with self._lock:
            self._values["paused"] = paused
            self._save()

    def _save(self) -> None:
        _write_atomic(self.settings_path, self._values)
        self.version += 1

    @property
    def paused(self) -> bool:
        with self._lock:
            return bool(self._values.get("paused"))

    def config(self) -> Config:
        return Config.from_values(self.values())

    def missing(self) -> list[str]:
        return missing_required(self.values())

    # ------------------------------------------------------------ auth
    def has_password(self) -> bool:
        return bool(_read(self.auth_path).get("hash"))

    def set_password(self, password: str) -> None:
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
        _write_atomic(self.auth_path, {"salt": salt.hex(), "hash": digest.hex(), "rounds": PBKDF2_ROUNDS})

    def check_password(self, password: str) -> bool:
        auth = _read(self.auth_path)
        if not auth.get("hash"):
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(auth["salt"]),
                                     int(auth.get("rounds", PBKDF2_ROUNDS)))
        return hmac.compare_digest(digest.hex(), auth["hash"])
