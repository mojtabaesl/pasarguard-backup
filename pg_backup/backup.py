"""Finite backup jobs, durable upload receipts, retention, and offline recovery."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import shutil
import subprocess
import tarfile
import tempfile
import zipfile

from .config import ManagerError, atomic_json, file_lock, read_json
from .telegram import Telegram


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds")


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def human_size(size):
    for suffix in ("B", "KB", "MB", "GB", "TB"):
        if size < 1000 or suffix == "TB":
            return "{:.1f} {}".format(size, suffix)
        size /= 1000


def check_space(path, needed):
    path = Path(path)
    while not path.exists():
        path = path.parent
    free = shutil.disk_usage(path).free
    if free < needed:
        raise ManagerError("Not enough free disk space at {}: {} available; {} required. Pending backups have been kept.".format(path, human_size(free), human_size(needed)))


def checked(command, **kwargs):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=3600, **kwargs)
    except FileNotFoundError as exc:
        raise ManagerError("Missing dependency: {}. Run sudo bash install.sh.".format(command[0])) from exc
    except subprocess.TimeoutExpired as exc:
        raise ManagerError("{} timed out.".format(Path(command[0]).name)) from exc
    if result.returncode:
        raise ManagerError("{} failed: {}".format(Path(command[0]).name, result.stderr.strip()[-500:]))
    return result.stdout.strip()


def ensure_key(store):
    config = store.load()
    if config["recipient"]:
        return config
    if not shutil.which("age") or not shutil.which("age-keygen"):
        raise ManagerError("Encryption needs age and age-keygen. Install them with sudo bash install.sh.")
    with file_lock(store.config_dir / "key.lock"):
        if not store.key_path.exists():
            identity = checked(["age-keygen"])
            if "AGE-SECRET-KEY-" not in identity:
                raise ManagerError("age-keygen did not produce a recovery key.")
            fd, name = tempfile.mkstemp(prefix=".key-", dir=str(store.config_dir))
            try:
                with os.fdopen(fd, "w") as stream:
                    stream.write(identity + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(name, store.key_path)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
        recipient = checked(["age-keygen", "-y", str(store.key_path)])
        return store.update(recipient=recipient, key_backed_up=False)


def native_compatible(config):
    path = Path(config["native_cli"])
    try:
        source = path.read_text()
    except OSError as exc:
        raise ManagerError("PasarGuard's management script was not found at {}. Set its path in Installation settings.".format(path)) from exc
    if "PASARGUARD_SOURCE_ONLY" not in source:
        raise ManagerError("Your PasarGuard management script needs source-only support. Update only the management script using the official PasarGuard instructions, then retry. No panel update is performed by this manager.")
    if not (Path(config["app_dir"]) / ".env").is_file():
        raise ManagerError("PasarGuard .env was not found in {}. Check Installation settings.".format(config["app_dir"]))


def archive_candidates(directory):
    if not directory.exists():
        return {}
    return {p.name: (p.stat().st_mtime_ns, p.stat().st_size) for p in directory.iterdir()
            if p.is_file() and not p.is_symlink() and re.fullmatch(r"backup_[A-Za-z0-9_.-]+\.(?:zip|tar\.gz)", p.name)}


def validate_archive(path):
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                if not archive.namelist():
                    raise ManagerError("The native backup archive is empty.")
                bad = archive.testzip()
                if bad:
                    raise ManagerError("The native backup archive contains a damaged file.")
        elif tarfile.is_tarfile(path):
            with tarfile.open(path, "r:*") as archive:
                members = archive.getmembers()
                if not members:
                    raise ManagerError("The native backup archive is empty.")
                for member in members:
                    if member.isfile():
                        with archive.extractfile(member) as stream:
                            while stream.read(1024 * 1024):
                                pass
        else:
            raise ManagerError("The native backup did not produce a supported ZIP or tar archive.")
    except (zipfile.BadZipFile, tarfile.TarError, EOFError) as exc:
        raise ManagerError("The native backup archive is damaged.") from exc


def native_backup(config, folder, emit=print):
    native_compatible(config)
    directory = Path(config["app_dir"]) / "backup"
    before = archive_candidates(directory)
    check_space(directory, config["min_free_mb"] * 1000 * 1000)
    environment = os.environ.copy()
    environment.update(APP_DIR=config["app_dir"], DATA_DIR=config["data_dir"],
                       APP_NAME=Path(config["app_dir"]).name, TERM="dumb")
    adapter = Path(__file__).with_name("native-backup.sh")
    log_path = folder / "native.log"
    emit("Creating a consistent backup with PasarGuard's native engine…")
    with log_path.open("w", encoding="utf-8") as log:
        os.chmod(log_path, 0o600)
        process = subprocess.Popen(["bash", str(adapter), config["native_cli"]], env=environment,
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            process.wait(timeout=7200)
        except BaseException as exc:
            # The native shell can have dump/compression children. Reap the whole
            # group on cancellation or timeout, not only the shell process.
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            except ProcessLookupError:
                pass
            finally:
                # A child might outlive a shell that exits immediately on TERM.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if isinstance(exc, subprocess.TimeoutExpired):
                raise ManagerError("PasarGuard backup exceeded two hours. See {}.".format(log_path)) from exc
            raise
    if process.returncode:
        raise ManagerError("PasarGuard backup failed. The private log is {}.".format(log_path))
    after = archive_candidates(directory)
    changed = [name for name in after if before.get(name) != after[name]]
    if not changed:
        raise ManagerError("PasarGuard did not create a new archive. Existing backups were not uploaded.")
    latest = max(changed, key=lambda name: after[name][0])
    match = re.fullmatch(r"(backup_[0-9]+)\.part([0-9]+)\.zip", latest)
    if match:
        stem = match.group(1)
        pieces = sorted((directory / name for name in changed if re.fullmatch(re.escape(stem) + r"\.part[0-9]+\.zip", name)), key=lambda p: int(re.search(r"\.part([0-9]+)", p.name).group(1)))
        if [int(re.search(r"\.part([0-9]+)", p.name).group(1)) for p in pieces] != list(range(1, len(pieces) + 1)):
            raise ManagerError("The native backup has missing archive parts.")
        name = stem + ".zip"
    else:
        pieces, name = [directory / latest], latest
    total = sum(path.stat().st_size for path in pieces)
    check_space(folder, total * (3 if config["encryption"] else 2) + 64 * 1024 * 1024)
    target = folder / name
    with target.open("xb") as output:
        os.chmod(target, 0o600)
        for piece in pieces:
            with piece.open("rb") as source:
                shutil.copyfileobj(source, output, 1024 * 1024)
    emit("Checking the native archive…")
    validate_archive(target)
    return target, pieces


def split_payload(payload, folder, part_bytes):
    if payload.stat().st_size <= part_bytes:
        return [payload]
    paths = []
    with payload.open("rb") as stream:
        index = 1
        while True:
            chunk = stream.read(min(1024 * 1024, part_bytes))
            if not chunk:
                break
            part = folder / (payload.name + ".part{:05d}".format(index))
            with part.open("xb") as output:
                os.chmod(part, 0o600)
                output.write(chunk)
                left = part_bytes - len(chunk)
                while left:
                    chunk = stream.read(min(1024 * 1024, left))
                    if not chunk:
                        break
                    output.write(chunk)
                    left -= len(chunk)
            paths.append(part)
            index += 1
    return paths


def safe_member(folder, name):
    if not isinstance(name, str) or name in ("", ".", "..") or Path(name).name != name or "\\" in name:
        raise ManagerError("The manifest contains an unsafe filename.")
    path = folder / name
    if path.is_symlink():
        raise ManagerError("Refusing to read a symbolic link from a backup manifest.")
    return path


def verify_manifest(manifest_path, emit=lambda message: None):
    manifest_path = Path(manifest_path)
    manifest = read_json(manifest_path)
    if manifest.get("format") != "pg-backup-v1" or not isinstance(manifest.get("parts"), list) or not manifest["parts"]:
        raise ManagerError("Unsupported or empty backup manifest.")
    names = set()
    combined = hashlib.sha256()
    for part in manifest["parts"]:
        if not isinstance(part, dict) or part.get("name") in names:
            raise ManagerError("Invalid or duplicate manifest part.")
        path = safe_member(manifest_path.parent, part.get("name"))
        names.add(path.name)
        if not path.is_file() or path.stat().st_size != part.get("size"):
            raise ManagerError("Missing or incomplete backup part: " + path.name)
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
                combined.update(chunk)
        if digest.hexdigest() != part.get("sha256"):
            raise ManagerError("Checksum mismatch: " + path.name)
        emit("Verified " + path.name)
    if combined.hexdigest() != manifest.get("payload_sha256"):
        raise ManagerError("The combined archive checksum does not match.")
    return manifest


class Worker:
    def __init__(self, store, emit=print, telegram_factory=Telegram, native=native_backup):
        self.store = store
        self.emit = emit
        self.telegram_factory = telegram_factory
        self.native = native

    def save_record(self, record):
        atomic_json(self.store.record_dir(record["id"]) / "record.json", record)

    def status(self, **changes):
        status = read_json(self.store.state_dir / "status.json")
        status.update(changes)
        atomic_json(self.store.state_dir / "status.json", status)

    def _reconcile(self):
        """With the worker lock held, recover records left by a killed process."""
        for record in self.store.records():
            if record.get("status") not in ("creating", "uploading"):
                continue
            manifest = self.store.record_dir(record["id"]) / "manifest.json"
            try:
                payload = verify_manifest(manifest)
            except (ManagerError, OSError):
                record.update(status="failed", error="The previous process was interrupted before a complete backup was committed.")
            else:
                record.update(status="pending" if record.get("send_requested", True) else "local",
                              size=sum(part["size"] for part in payload["parts"]), encrypted=payload["encryption"] == "age",
                              error="The previous process was interrupted; verified files were kept.")
            self.save_record(record)

    def create(self, send=True):
        with file_lock(self.store.state_dir / "worker.lock", blocking=False):
            self._reconcile()
            config = self.store.load()
            if config["encryption"]:
                if not config["recipient"]:
                    raise ManagerError("Finish encryption setup first; no recovery recipient is configured.")
                if send and not config["key_backed_up"]:
                    raise ManagerError("Save the recovery key separately in Storage and encryption before uploading encrypted backups.")
                if not shutil.which("age"):
                    raise ManagerError("age is missing. Run sudo bash install.sh.")
            if send:
                self.telegram_factory(config).validate_channel()
            backup_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + secrets.token_hex(3)
            folder = self.store.record_dir(backup_id)
            self.store.backups_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            folder.mkdir(parents=True, mode=0o700)
            check_space(folder, config["min_free_mb"] * 1000 * 1000)
            record = {"id": backup_id, "created_at": utc_now(), "status": "creating", "send_requested": send, "receipts": {}, "manifest_message_id": None, "error": ""}
            self.save_record(record)
            self.status(last_attempt=record["created_at"], last_result="Creating backup")
            archive = None
            try:
                archive, native_paths = self.native(config, folder, self.emit)
                original_sha = sha256(archive)
                payload = archive
                if config["encryption"]:
                    self.emit("Encrypting the archive…")
                    payload = archive.with_name(archive.name + ".age")
                    checked(["age", "-r", config["recipient"], "-o", str(payload), str(archive)])
                    os.chmod(payload, 0o600)
                payload_hash = sha256(payload)
                paths = split_payload(payload, folder, config["part_mb"] * 1000 * 1000)
                manifest = {
                    "format": "pg-backup-v1", "id": backup_id, "created_at": record["created_at"],
                    "server": config["server_name"], "database": config["database"],
                    "archive_name": archive.name, "archive_sha256": original_sha,
                    "encryption": "age" if config["encryption"] else "none",
                    "recipient": config["recipient"] if config["encryption"] else "",
                    "payload_sha256": payload_hash,
                    "parts": [{"name": p.name, "size": p.stat().st_size, "sha256": sha256(p)} for p in paths],
                    "recovery": "Download this manifest and every listed part into one directory. Run: pg-backup recover manifest.json --identity /path/to/recovery-key.txt --output /path/to/restored-backup.zip. Then use PasarGuard's native restore --check before restoring. Never upload your recovery key.",
                }
                atomic_json(folder / "manifest.json", manifest)
                # Commit verified payloads before removing any source material.
                verify_manifest(folder / "manifest.json")
                record.update(status="pending" if send else "local", size=sum(p["size"] for p in manifest["parts"]), encrypted=config["encryption"])
                self.save_record(record)
                if payload not in paths:
                    payload.unlink()
                if config["encryption"]:
                    archive.unlink()
                    # Delete only the exact new native files already safely stored.
                    for native_path in native_paths:
                        native_path.unlink(missing_ok=True)
                self.status(last_backup=record["created_at"], last_backup_id=backup_id, last_result="Backup created")
                self.emit("Backup created: " + backup_id)
                if send:
                    self._upload(record, config)
                self._prune(config)
                return record
            except BaseException as exc:
                record["status"] = "pending" if (folder / "manifest.json").is_file() and record.get("status") in ("pending", "uploading") else "failed"
                record["error"] = "Cancelled" if isinstance(exc, KeyboardInterrupt) else str(exc).replace(config["bot_token"], "[token hidden]") if config["bot_token"] else str(exc)
                self.save_record(record)
                self.status(last_result=record["status"], last_error=record["error"])
                raise

    def _upload(self, record, config, resend=False):
        folder = self.store.record_dir(record["id"])
        manifest = verify_manifest(folder / "manifest.json")
        client = self.telegram_factory(config)
        destination = client.validate_channel()
        identity = (destination["channel_id"], config["bot_token"].split(":", 1)[0])
        previous = (record.get("channel_id"), record.get("bot_id"))
        if record.get("receipts") and previous != identity and not resend:
            raise ManagerError("The destination channel or sender has changed. Choose Resend to current channel to send the complete backup there.")
        if resend or previous != identity:
            record.update(receipts={}, manifest_message_id=None)
        client.channel = identity[0]
        record.update(channel_id=identity[0], bot_id=identity[1], status="uploading", error="")
        self.save_record(record)
        for index, part in enumerate(manifest["parts"], 1):
            if part["name"] in record["receipts"]:
                self.emit("Already uploaded part {}/{}; resuming…".format(index, len(manifest["parts"])))
                continue
            self.emit("Uploading part {}/{} to {}…".format(index, len(manifest["parts"]), destination["channel_title"]))
            caption = "PasarGuard backup | {}\n{}\nID: {}\nPart {}/{} | {} | {}\nSHA256: {}".format(
                config["server_name"][:100], manifest["created_at"], record["id"], index, len(manifest["parts"]), human_size(part["size"]), manifest["encryption"], part["sha256"])
            message_id = client.document(folder / part["name"], caption)
            record["receipts"][part["name"]] = message_id
            self.save_record(record)
        if not record.get("manifest_message_id"):
            record["manifest_message_id"] = client.document(folder / "manifest.json", "Recovery manifest for {}. Keep this file with every backup part. The recovery key is stored separately.".format(record["id"]))
        record.update(status="uploaded", uploaded_at=utc_now(), error="")
        self.save_record(record)
        self.status(last_upload=record["uploaded_at"], last_upload_id=record["id"], last_result="Uploaded", last_error="")
        self.emit("✓ Every part and the recovery manifest were accepted by Telegram.")

    def upload(self, backup_id, resend=False):
        with file_lock(self.store.state_dir / "worker.lock", blocking=False):
            self._reconcile()
            config = self.store.load()
            record = read_json(self.store.record_dir(backup_id) / "record.json")
            if record.get("id") != backup_id or record.get("status") not in ("local", "pending", "uploading", "uploaded"):
                raise ManagerError("This backup is not ready for upload.")
            if record.get("encrypted") and not config["key_backed_up"]:
                raise ManagerError("Save the recovery key separately before uploading this backup.")
            try:
                self._upload(record, config, resend)
                self._prune(config)
            except BaseException as exc:
                record.update(status="pending", error="Cancelled" if isinstance(exc, KeyboardInterrupt) else str(exc).replace(config["bot_token"], "[token hidden]") if config["bot_token"] else str(exc))
                self.save_record(record)
                self.status(last_result="Upload pending", last_error=record["error"])
                raise
            return record

    def retry_pending(self):
        with file_lock(self.store.state_dir / "worker.lock", blocking=False):
            self._reconcile()
        failures = []
        for record in reversed(self.store.records()):
            if record["status"] in ("pending", "uploading"):
                try:
                    self.upload(record["id"])
                except ManagerError as exc:
                    self.emit("Pending {}: {}".format(record["id"], exc))
                    failures.append(record["id"])
        return failures

    def _prune(self, config):
        # Local-only, interrupted and failed uploads are never removed automatically.
        successful = [record for record in self.store.records() if record["status"] == "uploaded"]
        for record in successful[config["retention"]:]:
            shutil.rmtree(self.store.record_dir(record["id"]))
            self.emit("Removed old local copy: " + record["id"])


def recover(manifest_path, output, identity=None, emit=print):
    manifest_path, output = Path(manifest_path).resolve(), Path(output).absolute()
    manifest = verify_manifest(manifest_path, emit)
    if output.exists() or output.is_symlink():
        raise ManagerError("The output already exists. Choose a new output path.")
    if not output.parent.is_dir():
        raise ManagerError("The output directory does not exist.")
    if manifest["encryption"] not in ("age", "none"):
        raise ManagerError("Unsupported backup encryption.")
    if manifest["encryption"] == "age" and not identity:
        raise ManagerError("Supply --identity with your separately saved age recovery key.")
    check_space(output.parent, sum(part["size"] for part in manifest["parts"]) * 2 + 1024 * 1024)
    with tempfile.TemporaryDirectory(prefix=".pg-recover-", dir=str(output.parent)) as temporary:
        joined = Path(temporary) / "payload"
        with joined.open("wb") as stream:
            for part in manifest["parts"]:
                with safe_member(manifest_path.parent, part["name"]).open("rb") as source:
                    shutil.copyfileobj(source, stream, 1024 * 1024)
        archive = joined
        if manifest["encryption"] == "age":
            archive = Path(temporary) / "archive"
            checked(["age", "-d", "-i", str(identity), "-o", str(archive), str(joined)])
        if sha256(archive) != manifest.get("archive_sha256"):
            raise ManagerError("The recovered archive checksum does not match.")
        validate_archive(archive)
        os.chmod(archive, 0o600)
        # Hard-link provides atomic no-clobber publication on the same filesystem.
        try:
            os.link(archive, output)
        except FileExistsError as exc:
            raise ManagerError("The output was created by another process; it was not overwritten.") from exc
    emit("Recovered archive: " + str(output))
    import shlex
    emit("Next, validate with: sudo pasarguard restore {} --check".format(shlex.quote(str(output))))
