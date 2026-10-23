"""Lagring av inställningar och inloggning i datamappen (/data i containern)."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
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

    def update(self, form: dict, partial: bool = False) -> list[str]:
        """Sparar värden från formuläret. Tomma lösenordsfält behåller nuvarande värde.
        Med partial=True ändras bara fälten som finns i formuläret (även kryssrutor).

        Returnerar en lista med fel; vid fel sparas ingenting.
        """
        with self._lock:
            new, errors = self.preview(form, partial)
            if errors:
                return errors
            self._values = new
            self._save()
        return []

    def preview(self, form: dict, partial: bool = False) -> tuple[dict, list[str]]:
        """Nuvarande värden med formulärets ändringar, utan att spara."""
        errors: list[str] = []
        with self._lock:
            new = dict(self._values)
        for f in FIELDS:
            if f.kind == "bool" and not partial:
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

    # ------------------------------------------------------------ användare
    # auth.json: {"users": [{"username", "name", "admin", "perms", "person", "salt", "hash", "rounds"}]}
    # Det äldre formatet med ett enda lösenord blir användaren "admin" med full behörighet.
    def _auth(self) -> dict:
        data = _read(self.auth_path)
        if data.get("hash") and "users" not in data:
            data = {"users": [{"username": "admin", "name": "Administratör", "admin": True, "perms": {},
                               "person": None, "salt": data["salt"], "hash": data["hash"],
                               "rounds": data.get("rounds", PBKDF2_ROUNDS)}]}
        data.setdefault("users", [])
        return data

    def _save_auth(self, data: dict) -> None:
        _write_atomic(self.auth_path, data)

    @staticmethod
    def _hash(password: str) -> dict:
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
        return {"salt": salt.hex(), "hash": digest.hex(), "rounds": PBKDF2_ROUNDS}

    @staticmethod
    def _verify(user: dict, password: str) -> bool:
        if not user.get("hash"):
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(user["salt"]),
                                     int(user.get("rounds", PBKDF2_ROUNDS)))
        return hmac.compare_digest(digest.hex(), user["hash"])

    @staticmethod
    def _public(user: dict) -> dict:
        return {k: v for k, v in user.items() if k not in ("salt", "hash", "rounds")}

    def has_password(self) -> bool:
        """Finns det minst en användare? (Annars visas första start.)"""
        return bool(self._auth()["users"])

    def users(self) -> list[dict]:
        with self._lock:
            return sorted((self._public(u) for u in self._auth()["users"]), key=lambda u: u["username"])

    def get_user(self, username: str | None) -> dict | None:
        if not username:
            return None
        with self._lock:
            for u in self._auth()["users"]:
                if u["username"].casefold() == username.casefold():
                    return self._public(u)
        return None

    def authenticate(self, username: str, password: str) -> dict | None:
        with self._lock:
            for u in self._auth()["users"]:
                if u["username"].casefold() == (username or "").strip().casefold():
                    return self._public(u) if self._verify(u, password) else None
        # Lika lång tid oavsett om användaren finns
        self._verify({"salt": "00" * 16, "hash": "00", "rounds": PBKDF2_ROUNDS}, password)
        return None

    def save_user(self, username: str, *, name: str = "", admin: bool = False, perms: dict | None = None,
                  person: str | None = None, password: str | None = None, create: bool = False) -> None:
        """Skapar eller uppdaterar en användare. Kastar ValueError med ett läsbart fel."""
        username = (username or "").strip()
        if not re.fullmatch(r"[\w.@-]{2,40}", username):
            raise ValueError("Användarnamnet ska vara 2–40 tecken: bokstäver, siffror, punkt, bindestreck eller @.")
        with self._lock:
            data = self._auth()
            existing = next((u for u in data["users"] if u["username"].casefold() == username.casefold()), None)
            if create and existing:
                raise ValueError(f"Användaren {username} finns redan.")
            if not create and not existing:
                raise ValueError(f"Användaren {username} finns inte.")
            if create and not password:
                raise ValueError("Ange ett lösenord för den nya användaren.")
            if existing and existing.get("admin") and not admin and \
                    sum(1 for u in data["users"] if u.get("admin")) == 1:
                raise ValueError("Det måste finnas minst en administratör.")
            user = existing or {"username": username}
            user.update({"name": (name or "").strip() or username, "admin": bool(admin),
                         "perms": {k: v for k, v in (perms or {}).items() if v}, "person": (person or "").strip() or None})
            if password:
                user.update(self._hash(password))
            if not existing:
                data["users"].append(user)
            self._save_auth(data)

    def delete_user(self, username: str) -> None:
        with self._lock:
            data = self._auth()
            user = next((u for u in data["users"] if u["username"].casefold() == username.casefold()), None)
            if not user:
                return
            if user.get("admin") and sum(1 for u in data["users"] if u.get("admin")) == 1:
                raise ValueError("Det måste finnas minst en administratör.")
            data["users"].remove(user)
            self._save_auth(data)

    def set_user_password(self, username: str, password: str) -> None:
        with self._lock:
            data = self._auth()
            for u in data["users"]:
                if u["username"].casefold() == username.casefold():
                    u.update(self._hash(password))
                    self._save_auth(data)
                    return
        raise ValueError(f"Användaren {username} finns inte.")

    def check_user_password(self, username: str, password: str) -> bool:
        return self.authenticate(username, password) is not None

    # Äldre gränssnitt: administratören "admin"
    def set_password(self, password: str, username: str = "admin") -> None:
        if self.get_user(username):
            self.set_user_password(username, password)
        else:
            self.save_user(username, name="Administratör", admin=True, password=password, create=True)

    def check_password(self, password: str, username: str = "admin") -> bool:
        return self.check_user_password(username, password)


# =========================================================================== behörigheter
# Område -> möjliga nivåer. Nivåerna är ordnade: egen < las < andra.
AREAS = [
    ("oversikt", "Översikt", ("las", "andra"), "Se synkens status. Ändra = köra, pausa och hämta om."),
    ("tid", "Tidrapport", ("egen", "las", "andra"), "Bara egen = bara sin egen tidrapport. Ändra = stämplingar."),
    ("bokforing", "Bokföring", ("las", "andra"), "Ändra = bankningar, datum, Worldline-filer och konton."),
    ("logg", "Logg", ("las",), "Se loggen."),
]
LEVEL_LABELS = {"": "Ingen åtkomst", "egen": "Bara egen", "las": "Läsa", "andra": "Läsa och ändra"}
_RANK = {"": 0, "egen": 1, "las": 2, "andra": 3}


def can(user: dict | None, area: str, level: str = "las") -> bool:
    """Har användaren minst nivån i området? Administratörer får allt; 'admin' kräver administratör."""
    if not user:
        return False
    if user.get("admin"):
        return True
    if area == "admin":
        return False
    return _RANK.get((user.get("perms") or {}).get(area, ""), 0) >= _RANK.get(level, 99)
