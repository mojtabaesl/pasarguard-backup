"""Headless commands; importing this module does not import curses or start a UI."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import tempfile

from . import __version__
from .backup import Worker, ensure_key, native_compatible, recover, utc_now, verify_manifest
from .config import ManagerError, Store, mask, read_json, require_root
from . import scheduler
from .telegram import Telegram, diagnose_network


LAUNCHER = Path(__file__).resolve().parent.parent / "pg-backup"


def parser():
    command = argparse.ArgumentParser(description="PasarGuard backups to your Telegram channel. No resident manager process.")
    command.add_argument("--version", action="version", version=__version__)
    command.add_argument("--config-dir", help="Configuration directory (default: /etc/pg-backup)")
    command.add_argument("--state-dir", help="Backup/state directory (default: /var/lib/pg-backup)")
    command.add_argument("--demo", action="store_true", help="Explore a safe UI demo without root, a server, or network access")
    command.add_argument("--plain", action="store_true", help="Use the numbered-menu interface")
    commands = command.add_subparsers(dest="command")
    commands.add_parser("menu", help="Open the interactive manager (default)")
    commands.add_parser("setup", help="First-run setup; reuse detected and saved settings")
    run = commands.add_parser("run", help="Create a backup, send it, and exit")
    run.add_argument("--local-only", action="store_true", help="Create an encrypted local backup without uploading")
    run.add_argument("--scheduled", action="store_true", help="Cron worker: retry pending uploads, back up, then exit")
    commands.add_parser("retry", help="Resume all pending uploads and exit")
    upload = commands.add_parser("send", help="Upload or resume an existing backup")
    upload.add_argument("backup_id")
    upload.add_argument("--resend", action="store_true", help="Send every part again to the currently configured channel")
    verify = commands.add_parser("verify", help="Verify every payload checksum")
    verify.add_argument("backup_id")
    restore = commands.add_parser("recover", help="Verify, reassemble, and decrypt downloaded backup files; does not restore the panel")
    restore.add_argument("manifest")
    restore.add_argument("--identity", help="Separately saved age recovery key")
    restore.add_argument("--output", required=True, help="New archive path; existing files are never overwritten")
    commands.add_parser("status", help="Print status and settings with the bot token hidden")
    commands.add_parser("list", help="List local backups")
    commands.add_parser("diagnose", help="Check local dependencies and installation; no Telegram messages sent")
    commands.add_parser("telegram-network", help="Test direct IPv4/IPv6 HTTPS access without credentials or sending messages")
    commands.add_parser("check-telegram", help="Check channel identity and posting permissions; no message sent")
    commands.add_parser("test-upload", help="Send a small test document to the channel")
    commands.add_parser("key-init", help="Generate an age recovery key once; never overwrites an existing key")
    commands.add_parser("enable", help="Enable cron after setup is complete")
    commands.add_parser("disable", help="Disable this manager's cron entry")
    return command


def telegram_check(store):
    info = Telegram(store.load()).validate_channel()
    info["channel_verified_at"] = utc_now()
    config = store.update(**info)
    print("✓ Channel: {} ({})".format(config["channel_title"], config["channel_id"]), flush=True)
    print("✓ The bot can post files directly to this channel.", flush=True)


def diagnose(store):
    config = store.load()
    problems = []
    for executable in ("bash", "age", "age-keygen", "zip", "unzip", "sqlite3", "docker", "crontab"):
        found = shutil.which(executable)
        print(("✓ " + executable + ": " + found) if found else ("! Missing: " + executable))
        if not found:
            problems.append(executable)
    try:
        native_compatible(config)
        print("✓ Native PasarGuard backup interface is available")
    except ManagerError as exc:
        print("! " + str(exc))
        problems.append("PasarGuard")
    print("Connection: direct HTTPS to api.telegram.org (no proxy)")
    print("Recovery key: " + (str(store.key_path) if store.key_path.exists() else "Not generated"))
    print("Saved separately: " + ("Yes" if config["key_backed_up"] else "No"))
    conflicts = scheduler.legacy_jobs()
    for source, _ in conflicts:
        print("! Existing PasarGuard backup schedule in " + source)
    print("Idle architecture: no service, listener, polling loop, or persistent manager process")
    return 1 if problems else 0


def main(argv=None):
    args = parser().parse_args(argv)
    def cancel_job(signum, frame):
        raise KeyboardInterrupt
    # Termination/SSH disconnects follow the same cleanup path as Ctrl+C.
    signal.signal(signal.SIGTERM, cancel_job)
    signal.signal(signal.SIGHUP, cancel_job)
    if args.demo:
        if args.command not in (None, "menu", "setup"):
            parser().error("--demo is for the interactive interface only")
        with tempfile.TemporaryDirectory(prefix="pg-backup-demo-") as directory:
            store = Store(Path(directory) / "config", Path(directory) / "state")
            config = store.load()
            config.update(setup_complete=True, database="PostgreSQL / TimescaleDB", server_name="my-panel",
                          channel_id="-1001234567890", channel_title="My Backup Channel", channel_verified_at=utc_now(),
                          bot_token="123456789:demo_token", recipient="age1demo", key_backed_up=True,
                          schedule_enabled=True)
            config["sources"].update({"database": "Detected", "app_dir": "Detected", "bot_token": "Saved", "channel_id": "Saved", "recipient": "Saved"})
            store.save(config)
            from .ui import launch
            launch(store, plain=args.plain, demo=True)
        return 0
    store = Store(args.config_dir, args.state_dir)
    try:
        if args.command == "recover":
            recover(args.manifest, args.output, args.identity)
            return 0
        if args.command == "telegram-network":
            return 0 if diagnose_network() else 1
        require_root()
        if args.command in (None, "menu", "setup"):
            if not sys.stdin.isatty():
                raise ManagerError("Open a terminal for the menu, or use pg-backup --help for headless commands.")
            from .ui import launch
            launch(store, plain=args.plain, setup=args.command == "setup")
        elif args.command == "run":
            config = store.load()
            if args.scheduled and not config["schedule_enabled"]:
                return 0
            worker = Worker(store, emit=lambda message: print(message, flush=True))
            if args.scheduled:
                worker.retry_pending()
            worker.create(send=not args.local_only)
        elif args.command == "retry":
            return 1 if Worker(store).retry_pending() else 0
        elif args.command == "send":
            Worker(store).upload(args.backup_id, resend=args.resend)
        elif args.command == "verify":
            verify_manifest(store.record_dir(args.backup_id) / "manifest.json", print)
            print("✓ Every backup part and the complete payload checksum match.")
            print("For a database validation, recover the archive and run pasarguard restore <archive> --check.")
        elif args.command == "key-init":
            ensure_key(store)
            print("Recovery key: " + str(store.key_path))
            print("Save this key separately from your server and Telegram channel.")
        elif args.command == "check-telegram":
            telegram_check(store)
        elif args.command == "test-upload":
            telegram_check(store)
            with tempfile.TemporaryDirectory(prefix="pg-backup-test-") as directory:
                path = Path(directory) / "pg-backup-test.txt"
                path.write_text("PasarGuard Backup Manager\nDirect Telegram channel upload test.\n" + utc_now() + "\n")
                Telegram(store.load()).document(path, "✓ Backup Manager: direct channel delivery works.")
            print("✓ Test document sent to the channel.")
        elif args.command == "enable":
            scheduler.enable(store, LAUNCHER)
            print("✓ Automatic backups enabled: " + scheduler.next_run(store.load()["schedule"]))
        elif args.command == "disable":
            scheduler.disable(store)
            print("✓ Automatic backups disabled. Local backups and settings were kept.")
        elif args.command == "diagnose":
            return diagnose(store)
        elif args.command == "list":
            for record in store.records():
                print("{}  {:10}  {}".format(record["id"], record["status"], record.get("created_at", "")))
        elif args.command == "status":
            config = store.load()
            config["bot_token"] = mask(config["bot_token"])
            print(json.dumps({"config": config, "status": read_json(store.state_dir / "status.json"),
                              "pending_uploads": sum(r["status"] in ("pending", "uploading") for r in store.records()),
                              "next_run": scheduler.next_run(config["schedule"]) if config["schedule_enabled"] else "Disabled"}, indent=2, ensure_ascii=False))
        return 0
    except KeyboardInterrupt:
        print("\nCancelled. Pending backup files have been kept.", file=sys.stderr)
        return 130
    except (ManagerError, OSError) as exc:
        try:
            config = store.load() if store.config_path.exists() else {}
        except (ManagerError, OSError):
            config = {}
        message = str(exc)
        if config.get("bot_token"):
            message = message.replace(config["bot_token"], "[token hidden]")
        if args.command == "run":
            try:
                Worker(store).status(last_result="Failed", last_error=message)
            except (ManagerError, OSError):
                pass
        print("Error: " + message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
