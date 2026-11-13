"""One persisted configuration, conservative detection, and private atomic writes."""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import socket
import tempfile


class ManagerError(Exception):
    """An actionable error suitable for display without a traceback."""


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".write-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as stream:
            data = json.load(stream)
        if not isinstance(data, dict):
            raise ManagerError("Expected a JSON object in {}.".format(path))
        return data
    except FileNotFoundError:
        return {} if default is None else default
    except (ValueError, OSError) as exc:
        raise ManagerError("Cannot read {}: {}".format(path, exc)) from exc


@contextlib.contextmanager
def file_lock(path, blocking=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            raise ManagerError("Another backup operation is running. Try again when it finishes.") from exc
        yield
    finally:
        os.close(fd)


def dotenv(path):
    """Read values as data. Never source or evaluate an existing .env file."""
    values = {}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, PermissionError):
        return values
    for line in lines:
        match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", line)
        if not match:
            continue
        key, value = match.groups()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        values[key] = value
    return values


def server_timezone():
    try:
        value = Path("/etc/timezone").read_text().strip()
        if value:
            return value
    except OSError:
        pass
    try:
        target = str(Path("/etc/localtime").resolve())
        if "/zoneinfo/" in target:
            return target.split("/zoneinfo/", 1)[1]
    except OSError:
        pass
    import time
    return time.tzname[0] or "server local time"


def defaults():
    app_dir = "/opt/pasarguard"
    env = dotenv(Path(app_dir) / ".env")
    db = env.get("SQLALCHEMY_DATABASE_URL", "")
    database = "Not detected"
    if db:
        scheme = db.split(":", 1)[0].split("+", 1)[0]
        database = {"sqlite": "SQLite", "mysql": "MySQL / MariaDB", "postgresql": "PostgreSQL / TimescaleDB"}.get(scheme, scheme)
    sources = {}
    values = {
        "schema": 1, "app_dir": app_dir, "data_dir": "/var/lib/pasarguard",
        "native_cli": shutil.which("pasarguard") or "/usr/local/bin/pasarguard",
        "database": database, "server_name": socket.gethostname(),
        "timezone": server_timezone(), "bot_token": "", "channel_id": "",
        "channel_title": "", "channel_verified_at": "",
        "schedule": "0 */6 * * *", "schedule_enabled": False,
        "retention": 7, "part_mb": 45, "retries": 3,
        "encryption": True, "recipient": "", "key_backed_up": False,
        "min_free_mb": 1024, "setup_complete": False,
    }
    for key in ("server_name", "timezone"):
        sources[key] = "Detected"
    if Path(app_dir).is_dir():
        for key in ("app_dir", "data_dir", "database", "native_cli"):
            sources[key] = "Detected"
    for key, aliases in {
        "bot_token": ("BACKUP_TELEGRAM_BOT_KEY", "TELEGRAM_TOKEN", "TELEGRAM_API_TOKEN"),
        "channel_id": ("BACKUP_TELEGRAM_CHAT_ID", "TELEGRAM_CHAT_ID"),
    }.items():
        for alias in aliases:
            pattern = r"\d+:[A-Za-z0-9_-]+" if key == "bot_token" else r"-100\d+|@[A-Za-z0-9_]{5,}"
            if env.get(alias) and re.fullmatch(pattern, env[alias]):
                values[key] = env[alias]
                sources[key] = "Detected"
                break
    values["sources"] = sources
    return values


class Store:
    def __init__(self, config_dir=None, state_dir=None):
        self.config_dir = Path(config_dir or os.environ.get("PG_BACKUP_CONFIG_DIR", "/etc/pg-backup"))
        self.state_dir = Path(state_dir or os.environ.get("PG_BACKUP_STATE_DIR", "/var/lib/pg-backup"))
        self.config_path = self.config_dir / "config.json"
        self.backups_dir = self.state_dir / "backups"
        self.key_path = self.config_dir / "recovery-key.txt"

    def load(self):
        current = read_json(self.config_path)
        base = defaults()
        base.update(current)
        # Cron follows the machine's timezone; this is a detected, read-only value.
        base["timezone"] = server_timezone()
        validate(base)
        return base

    def save(self, config):
        validate(config)
        with file_lock(self.config_dir / "config.lock"):
            atomic_json(self.config_path, config)

    def update(self, **changes):
        with file_lock(self.config_dir / "config.lock"):
            config = self.load()
            config.update(changes)
            sources = dict(config.get("sources", {}))
            for key in changes:
                if key != "sources":
                    sources[key] = "Saved"
            config["sources"] = sources
            validate(config)
            atomic_json(self.config_path, config)
        return config

    def records(self):
        if not self.backups_dir.exists():
            return []
        records = []
        for folder in sorted(self.backups_dir.iterdir(), reverse=True):
            if folder.is_dir() and not folder.is_symlink() and (folder / "record.json").is_file():
                record = read_json(folder / "record.json")
                if record.get("id") == folder.name:
                    records.append(record)
        return sorted(records, key=lambda record: (record.get("created_at", ""), record["id"]), reverse=True)

    def record_dir(self, backup_id):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", backup_id):
            raise ManagerError("Invalid backup ID.")
        return self.backups_dir / backup_id


def validate(config):
    for key, low, high in (("retention", 1, 1000), ("part_mb", 1, 45), ("retries", 0, 10), ("min_free_mb", 1, 1000000)):
        value = config.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ManagerError("{} must be between {} and {}.".format(key, low, high))
    for key in ("app_dir", "data_dir", "native_cli"):
        value = config.get(key)
        if not isinstance(value, str) or not value.startswith("/") or any(ord(c) < 32 for c in value):
            raise ManagerError("{} must be an absolute path without control characters.".format(key))
    for key in ("encryption", "schedule_enabled", "key_backed_up", "setup_complete"):
        if not isinstance(config.get(key), bool):
            raise ManagerError("{} must be true or false.".format(key))
    for key in ("bot_token", "channel_id", "recipient", "server_name", "database", "timezone", "channel_title", "schedule"):
        if not isinstance(config.get(key), str) or any(ord(c) < 32 for c in config[key]):
            raise ManagerError("{} must be text without control characters.".format(key))
    if config["bot_token"] and not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", config["bot_token"]):
        raise ManagerError("The bot token format is invalid. Copy it from BotFather.")
    if config["channel_id"] and not re.fullmatch(r"-100\d+|@[A-Za-z0-9_]{5,}", config["channel_id"]):
        raise ManagerError("Use a channel ID beginning with -100, or its @username.")
    if config["recipient"] and not re.fullmatch(r"age1[a-z0-9]+", config["recipient"]):
        raise ManagerError("The age recipient must be an age1 public key.")


def mask(token):
    return "••••••" + token[-4:] if token else "Not configured"


def require_root():
    if os.geteuid() != 0:
        raise ManagerError("Run this on your PasarGuard server with sudo: sudo pg-backup")
