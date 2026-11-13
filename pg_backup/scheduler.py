"""Cron integration: persistent files only, no manager service."""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile

from .config import ManagerError, file_lock, server_timezone


MARKER = "# pg-backup-manager"
NATIVE_MARKER = "# pasarguard-backup-service"
CRON_PATH = Path("/etc/cron.d/pg-backup")


def parse_schedule(expression):
    if not re.fullmatch(r"[0-9*,/ -]+", expression):
        raise ManagerError("Use a five-field cron expression with numbers, *, ranges, lists, and steps.")
    parts = expression.split()
    if len(parts) != 5:
        raise ManagerError("Use five cron fields, for example: 0 */6 * * *")
    result = []
    for value, low, high in zip(parts, (0, 0, 1, 1, 0), (59, 23, 31, 12, 7)):
        selected = set()
        for item in value.split(","):
            if not re.fullmatch(r"(?:\*|\d+(?:-\d+)?)(?:/\d+)?", item):
                raise ManagerError("Invalid cron field: " + value)
            base, _, step_text = item.partition("/")
            step = int(step_text) if step_text else 1
            if not 1 <= step <= high + 1:
                raise ManagerError("Invalid cron step: " + item)
            if base == "*":
                start, end = low, high
            elif "-" in base:
                start, end = map(int, base.split("-"))
            else:
                start = int(base)
                end = high if step_text else start
            if not low <= start <= end <= high:
                raise ManagerError("Cron field is outside its allowed range: " + value)
            selected.update(range(start, end + 1, step))
        result.append(selected)
    return result


def next_run(expression, now=None):
    fields = parse_schedule(expression)
    parts = expression.split()
    stamp = (int((now.timestamp() if now else dt.datetime.now().timestamp()) // 60) + 1) * 60
    # Local timestamps follow the same timezone as the system cron daemon.
    for _ in range(60 * 24 * 366):
        current = dt.datetime.fromtimestamp(stamp)
        weekday = (current.weekday() + 1) % 7
        dom = current.day in fields[2]
        dow = weekday in fields[4] or (weekday == 0 and 7 in fields[4])
        day_match = (dom and dow) if parts[2].startswith("*") or parts[4].startswith("*") else (dom or dow)
        if current.minute in fields[0] and current.hour in fields[1] and current.month in fields[3] and day_match:
            return current.strftime("%Y-%m-%d %H:%M") + " " + server_timezone()
        stamp += 60
    return "No run within the next year; check the schedule"


def schedule_label(expression):
    return {"0 */6 * * *": "Every 6 hours", "0 * * * *": "Every hour", "0 */2 * * *": "Every 2 hours", "0 */12 * * *": "Every 12 hours", "0 3 * * *": "Daily at 03:00"}.get(expression, expression)


def legacy_jobs():
    """Inspect root's crontab and /etc/cron.d without changing either."""
    found = []
    if shutil.which("crontab"):
        process = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10)
        if process.returncode not in (0, 1):
            raise ManagerError("Cannot inspect the existing crontab. Check crontab permissions.")
        if process.returncode == 0:
            for line in process.stdout.splitlines():
                if is_legacy(line):
                    found.append(("root crontab", line))
    for path in Path("/etc/cron.d").glob("*"):
        if path == CRON_PATH or not path.is_file():
            continue
        for line in path.read_text(errors="replace").splitlines():
            if is_legacy(line):
                found.append((str(path), line))
    return found


def is_legacy(line):
    return not line.lstrip().startswith("#") and (NATIVE_MARKER in line or bool(re.search(r"(?:/|\s)pasarguard\s+backup(?:\s|$)", line)))


def remove_marked_native_job():
    """Only the exact upstream-tagged job in root's crontab can be migrated."""
    process = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10)
    if process.returncode:
        raise ManagerError("Could not read root's crontab for migration.")
    lines = [line for line in process.stdout.splitlines() if NATIVE_MARKER not in line]
    result = subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise ManagerError("Could not migrate the old PasarGuard cron job.")
    return process.stdout


def cron_content(config, store, launcher):
    parse_schedule(config["schedule"])
    args = [str(launcher), "--config-dir", str(store.config_dir), "--state-dir", str(store.state_dir), "run", "--scheduled"]
    for value in args:
        if any(c in value for c in ("%", "\n", "\r")):
            raise ManagerError("Scheduled command paths cannot contain %, CR, or LF.")
    command = " ".join(shlex.quote(value) for value in args)
    return "SHELL=/bin/sh\nPATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n{} root {} >/dev/null 2>&1 {}\n".format(config["schedule"], command, MARKER)


def enable(store, launcher, migrate=False, cron_path=None):
    config = store.load()
    if not config["setup_complete"]:
        raise ManagerError("Finish the first-run setup before enabling automatic backups.")
    if config["encryption"] and not config["key_backed_up"]:
        raise ManagerError("Save your recovery key separately before enabling automatic backups.")
    launcher = Path(launcher).resolve()
    if not launcher.is_file() or not os.access(launcher, os.X_OK):
        raise ManagerError("Install the manager first: sudo bash install.sh")
    conflicts = legacy_jobs()
    if conflicts:
        migratable = all(source == "root crontab" and NATIVE_MARKER in line for source, line in conflicts)
        if not migrate or not migratable:
            raise ManagerError("An existing PasarGuard backup schedule was found. Review it in Automatic backup schedule before enabling a second schedule.")
    path = Path(cron_path or CRON_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = cron_content(config, store, launcher)
    with file_lock(store.config_dir / "schedule.lock"):
        if path.exists() and MARKER not in path.read_text():
            raise ManagerError("Refusing to replace a cron file that is not owned by this manager.")
        fd, temporary = tempfile.mkstemp(prefix=".pg-backup-", dir=str(path.parent))
        old_crontab = None
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(content)
            os.chmod(temporary, 0o644)
            # Install first, then remove the old tagged job; rollback on failure.
            previous = path.read_bytes() if path.exists() else None
            os.replace(temporary, path)
            try:
                if conflicts:
                    old_crontab = remove_marked_native_job()
                store.update(schedule_enabled=True)
            except BaseException:
                if old_crontab is not None:
                    subprocess.run(["crontab", "-"], input=old_crontab, capture_output=True, text=True, timeout=10)
                if previous is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(previous)
                raise
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def disable(store, cron_path=None):
    path = Path(cron_path or CRON_PATH)
    with file_lock(store.config_dir / "schedule.lock"):
        if path.exists():
            if MARKER not in path.read_text():
                raise ManagerError("Refusing to remove a cron file that is not owned by this manager.")
            path.unlink()
        store.update(schedule_enabled=False)
