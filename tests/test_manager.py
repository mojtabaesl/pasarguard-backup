"""Exercise failure boundaries without a real panel, channel, root, or cron writes."""
import datetime as dt
import importlib
import fcntl
import json
import os
import shutil
from pathlib import Path
import subprocess
import select
import struct
import sys
import termios
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

from pg_backup.backup import Worker, ensure_key, recover, sha256, split_payload, verify_manifest
from pg_backup.config import ManagerError, Store, atomic_json, dotenv, file_lock
from pg_backup import scheduler
from pg_backup.telegram import Telegram


class TemporaryStore(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = Store(self.root / "config", self.root / "state")
        config = self.store.load()
        config.update(encryption=False, min_free_mb=1, bot_token="123456:token", channel_id="-100123456789", setup_complete=True)
        self.store.save(config)


class ConfigTests(TemporaryStore):
    def test_shared_settings_preserve_unrelated_values(self):
        self.store.update(schedule="0 3 * * *")
        new_store = Store(self.store.config_dir, self.store.state_dir)
        new_store.update(retention=12)
        config = self.store.load()
        self.assertEqual(config["schedule"], "0 3 * * *")
        self.assertEqual(config["retention"], 12)
        self.assertEqual(config["bot_token"], "123456:token")
        self.assertEqual(config["sources"]["retention"], "Saved")

    def test_private_config_permissions(self):
        self.assertEqual(self.store.config_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.store.config_dir.stat().st_mode & 0o777, 0o700)

    def test_invalid_update_does_not_replace_config(self):
        before = self.store.config_path.read_bytes()
        with self.assertRaises(ManagerError):
            self.store.update(part_mb=50)
        self.assertEqual(self.store.config_path.read_bytes(), before)

    def test_dotenv_is_never_executed(self):
        path = self.root / ".env"
        path.write_text('export TOKEN="abc=def"\nVALUE=$(touch /tmp/never-execute-this)\nNUMBER=123 # comment\n')
        self.assertEqual(dotenv(path), {"TOKEN": "abc=def", "VALUE": "$(touch /tmp/never-execute-this)", "NUMBER": "123"})

    def test_worker_lock_rejects_overlap(self):
        with file_lock(self.store.state_dir / "worker.lock"):
            with self.assertRaises(ManagerError):
                with file_lock(self.store.state_dir / "worker.lock", blocking=False):
                    pass
        with file_lock(self.store.state_dir / "worker.lock", blocking=False):
            pass


class TelegramTests(unittest.TestCase):
    config = {"bot_token": "123456:token", "channel_id": "-100123456789", "retries": 3}

    def test_respects_retry_after(self):
        calls, sleeps = [], []
        def transport(method, fields, document):
            calls.append(method)
            return (429, {"ok": False, "parameters": {"retry_after": 17}}) if len(calls) == 1 else (200, {"ok": True, "result": {"id": 42}})
        result = Telegram(self.config, transport, sleeps.append).call("getMe")
        self.assertEqual(result, {"id": 42})
        self.assertEqual(sleeps, [17])

    def test_permanent_error_is_not_retried_and_token_is_redacted(self):
        calls = []
        def transport(*args):
            calls.append(1)
            return 403, {"description": "forbidden 123456:token"}
        with self.assertRaisesRegex(ManagerError, "token hidden"):
            Telegram(self.config, transport).call("getMe")
        self.assertEqual(len(calls), 1)

    def test_network_errors_have_finite_retries(self):
        calls = []
        def transport(*args):
            calls.append(1)
            raise OSError("network unavailable")
        with self.assertRaises(ManagerError):
            Telegram(self.config, transport, lambda _: None).call("getMe")
        self.assertEqual(len(calls), 4)

    def test_rejects_non_channel_destinations(self):
        def transport(method, fields, document):
            return 200, {"ok": True, "result": {"id": 1, "type": "private"}}
        with self.assertRaisesRegex(ManagerError, "not a channel"):
            Telegram(self.config, transport).validate_channel()

    def test_requires_channel_post_permission(self):
        replies = iter([{"id": 1}, {"id": -1001, "type": "channel"}, {"status": "administrator", "can_post_messages": False}])
        with self.assertRaisesRegex(ManagerError, "Post Messages"):
            Telegram(self.config, lambda *args: (200, {"ok": True, "result": next(replies)})).validate_channel()

    def test_accepts_channel_and_canonicalizes_id(self):
        replies = iter([{"id": 1}, {"id": -100123456789, "type": "channel", "title": "Backups"}, {"status": "administrator", "can_post_messages": True}])
        result = Telegram(self.config, lambda *args: (200, {"ok": True, "result": next(replies)})).validate_channel()
        self.assertEqual(result, {"channel_id": "-100123456789", "channel_title": "Backups"})

    def test_direct_streamed_transport_ignores_proxy_environment(self):
        class Response:
            status = 200
            def read(self, limit):
                return b'{"ok":true,"result":{"message_id":12}}'
        class Connection:
            def __init__(self, *args, **kwargs):
                self.host = args[0]
                self.sent = []
                self.headers = {}
            def putrequest(self, method, path):
                self.path = path
            def putheader(self, key, value):
                self.headers[key] = value
            def endheaders(self):
                pass
            def send(self, chunk):
                self.sent.append(chunk)
            def getresponse(self):
                return Response()
            def close(self):
                pass
        connection = Connection("api.telegram.org")
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "part.bin"
            file.write_bytes(os.urandom(600000))
            with patch.dict(os.environ, {"HTTPS_PROXY": "http://unreachable.invalid"}), patch("pg_backup.telegram.http.client.HTTPSConnection", return_value=connection):
                self.assertEqual(Telegram(self.config).document(file), 12)
            self.assertEqual(sum(map(len, connection.sent)), int(connection.headers["Content-Length"]))
            self.assertTrue(all(len(chunk) <= 262144 for chunk in connection.sent[1:-1]))
            self.assertEqual(connection.host, "api.telegram.org")

    def test_text_fields_have_no_extra_trailing_newline(self):
        connection = unittest.mock.MagicMock()
        response = connection.getresponse.return_value
        response.status = 200
        response.read.return_value = b'{"ok":true,"result":{}}'
        with patch("pg_backup.telegram.http.client.HTTPSConnection", return_value=connection):
            Telegram(self.config).call("getChat", {"chat_id": "-100123456789"})
        body = b"".join(call.args[0] for call in connection.send.call_args_list)
        self.assertIn(b"-100123456789\r\n--pgbackup-", body)
        self.assertNotIn(b"-100123456789\r\n\r\n--", body)


class FakeTelegram:
    def __init__(self, config, sent, fail_at=None):
        self.channel = config["channel_id"]
        self.sent, self.fail_at = sent, fail_at

    def validate_channel(self):
        return {"channel_id": self.channel, "channel_title": "Test channel"}

    def document(self, path, caption=""):
        if self.fail_at is not None and len(self.sent) == self.fail_at:
            raise ManagerError("simulated upload failure")
        self.sent.append(Path(path).name)
        return len(self.sent)


def fake_native(config, folder, emit):
    path = folder / "backup_20261113000000.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("db.sql", os.urandom(1100000))
        archive.writestr(".env", "SAMPLE=true")
    return path, []


class WorkerTests(TemporaryStore):
    def worker(self, sent, fail_at=None):
        return Worker(self.store, lambda _: None, lambda config: FakeTelegram(config, sent, fail_at), fake_native)

    def test_partial_upload_is_durable_and_resumes_without_duplicate_confirmed_parts(self):
        self.store.update(part_mb=1)
        sent = []
        with self.assertRaises(ManagerError):
            self.worker(sent, fail_at=1).create()
        record = self.store.records()[0]
        self.assertEqual(record["status"], "pending")
        self.assertEqual(len(record["receipts"]), 1)
        first_part = sent[0]
        self.worker(sent).upload(record["id"])
        self.assertEqual(sent.count(first_part), 1)
        self.assertEqual(sent[-1], "manifest.json")
        self.assertEqual(self.store.records()[0]["status"], "uploaded")

    def test_manifest_failure_does_not_mark_backup_uploaded(self):
        sent = []
        with self.assertRaises(ManagerError):
            self.worker(sent, fail_at=1).create()
        record = self.store.records()[0]
        self.assertEqual(record["status"], "pending")
        self.assertIsNone(record["manifest_message_id"])
        self.worker(sent).upload(record["id"])
        self.assertEqual(sent, ["backup_20261113000000.zip", "manifest.json"])

    def test_changing_destination_requires_explicit_resend(self):
        self.store.update(part_mb=1)
        sent = []
        with self.assertRaises(ManagerError):
            self.worker(sent, fail_at=1).create()
        backup_id = self.store.records()[0]["id"]
        self.store.update(channel_id="-100999999999")
        with self.assertRaisesRegex(ManagerError, "destination channel or sender has changed"):
            self.worker(sent).upload(backup_id)
        self.worker(sent).upload(backup_id, resend=True)
        self.assertEqual(len(sent), 4)
        self.assertEqual(self.store.records()[0]["channel_id"], "-100999999999")

    def test_local_backup_can_be_sent_later(self):
        sent = []
        record = self.worker(sent).create(send=False)
        self.assertEqual(record["status"], "local")
        self.assertEqual(sent, [])
        self.worker(sent).upload(record["id"])
        self.assertEqual(self.store.records()[0]["status"], "uploaded")

    def test_retention_preserves_local_and_pending_backups(self):
        self.store.update(retention=1)
        sent = []
        local = self.worker(sent).create(send=False)
        with self.assertRaises(ManagerError):
            self.worker([], fail_at=0).create()
        pending = self.store.records()[0]
        old = self.worker(sent).create()
        newest = self.worker(sent).create()
        remaining = {r["id"] for r in self.store.records()}
        self.assertEqual(remaining, {local["id"], pending["id"], newest["id"]})
        self.assertNotIn(old["id"], remaining)

    def test_recovery_round_trip_for_split_backup_and_no_overwrite(self):
        self.store.update(part_mb=1)
        record = self.worker([]).create(send=False)
        folder = self.store.record_dir(record["id"])
        output = self.root / "recovered.zip"
        recover(folder / "manifest.json", output, emit=lambda _: None)
        manifest = json.loads((folder / "manifest.json").read_text())
        self.assertEqual(sha256(output), manifest["archive_sha256"])
        with self.assertRaisesRegex(ManagerError, "already exists"):
            recover(folder / "manifest.json", output, emit=lambda _: None)

    def test_tampered_part_prevents_upload(self):
        record = self.worker([]).create(send=False)
        folder = self.store.record_dir(record["id"])
        manifest = json.loads((folder / "manifest.json").read_text())
        (folder / manifest["parts"][0]["name"]).write_bytes(b"damaged")
        sent = []
        with self.assertRaises(ManagerError):
            self.worker(sent).upload(record["id"])
        self.assertEqual(sent, [])

    def test_manifest_path_traversal_is_rejected(self):
        folder = self.root / "unsafe"
        folder.mkdir()
        atomic_json(folder / "manifest.json", {"format": "pg-backup-v1", "parts": [{"name": "../secret", "size": 1}]})
        with self.assertRaisesRegex(ManagerError, "unsafe filename"):
            verify_manifest(folder / "manifest.json")

    def test_encrypted_upload_requires_separate_key_confirmation(self):
        self.store.update(encryption=True, recipient="age1example", key_backed_up=False)
        with self.assertRaisesRegex(ManagerError, "Save the recovery key"):
            self.worker([]).create()

    def test_retry_recovers_completed_payload_after_interrupted_creation(self):
        record = self.worker([]).create(send=False)
        record.update(status="creating", send_requested=True)
        atomic_json(self.store.record_dir(record["id"]) / "record.json", record)
        sent = []
        self.assertEqual(self.worker(sent).retry_pending(), [])
        self.assertEqual(self.store.records()[0]["status"], "uploaded")
        self.assertEqual(sent[-1], "manifest.json")

    def test_reconciliation_does_not_upload_interrupted_local_backup(self):
        record = self.worker([]).create(send=False)
        record.update(status="creating", send_requested=False)
        atomic_json(self.store.record_dir(record["id"]) / "record.json", record)
        sent = []
        self.worker(sent).retry_pending()
        self.assertEqual(self.store.records()[0]["status"], "local")
        self.assertEqual(sent, [])


class NativeAdapterTests(TemporaryStore):
    def test_adapter_suppresses_both_native_delivery_functions(self):
        script = self.root / "native.sh"
        script.write_text('''#!/usr/bin/env bash
send_backup_to_telegram() { echo UNENCRYPTED_UPLOAD; }
send_backup_error_to_telegram() { echo LOG_UPLOAD; }
backup_command() {
    echo NATIVE_BACKUP
    send_backup_to_telegram
    send_backup_error_to_telegram
}
if [ "${PASARGUARD_SOURCE_ONLY:-false}" != true ]; then
    echo DISPATCHER_WAS_RUN
    exit 1
fi
''')
        adapter = Path(__file__).resolve().parent.parent / "pg_backup/native-backup.sh"
        result = subprocess.run(["bash", str(adapter), str(script)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "NATIVE_BACKUP")

    def test_old_native_archive_is_not_silently_reused(self):
        from pg_backup.backup import native_backup
        app = self.root / "panel"
        directory = app / "backup"
        directory.mkdir(parents=True)
        (app / ".env").write_text("")
        (directory / "backup_old.zip").write_bytes(b"old")
        script = self.root / "native.sh"
        script.write_text('PASARGUARD_SOURCE_ONLY=true\nbackup_command() { :; }\n')
        self.store.update(app_dir=str(app), native_cli=str(script))
        folder = self.root / "output"
        folder.mkdir()
        with self.assertRaisesRegex(ManagerError, "did not create a new archive"):
            native_backup(self.store.load(), folder, lambda _: None)


class SchedulerTests(TemporaryStore):
    def test_custom_expression_ranges_lists_and_steps(self):
        fields = scheduler.parse_schedule("5,35 0-12/6 * * 1-5")
        self.assertEqual(fields[0], {5, 35})
        self.assertEqual(fields[1], {0, 6, 12})
        self.assertEqual(fields[4], {1, 2, 3, 4, 5})

    def test_rejects_invalid_or_injectable_schedule(self):
        for expression in ("0 */0 * * *", "60 * * * *", "0 0 * * *\nwhoami", "@reboot", "0 0 32 * *", "0 0 * 13 *"):
            with self.subTest(expression=expression), self.assertRaises(ManagerError):
                scheduler.parse_schedule(expression)

    def test_next_run(self):
        result = scheduler.next_run("0 */6 * * *", dt.datetime(2026, 11, 13, 12, 1))
        self.assertTrue(result.startswith("2026-11-13 18:00"), result)

    def test_day_of_month_and_weekday_follow_cron_or_semantics(self):
        # Saturday, November 14: next matching Monday is November 16.
        result = scheduler.next_run("0 3 20 * 1", dt.datetime(2026, 11, 14, 12, 0))
        self.assertTrue(result.startswith("2026-11-16 03:00"), result)

    def test_cron_never_contains_credentials(self):
        content = scheduler.cron_content(self.store.load(), self.store, Path("/opt/pg-backup/pg-backup"))
        self.assertNotIn("123456:token", content)
        self.assertIn("run --scheduled", content)
        self.assertIn(" root ", content)

    def test_only_marked_jobs_can_be_migrated(self):
        self.assertTrue(scheduler.is_legacy("0 * * * * bash /usr/local/bin/pasarguard backup # pasarguard-backup-service"))
        self.assertTrue(scheduler.is_legacy("0 * * * * /usr/local/bin/pasarguard backup"))
        self.assertFalse(scheduler.is_legacy("# 0 * * * * pasarguard backup"))

    def test_enable_disable_use_only_owned_cron_file(self):
        launcher = self.root / "pg-backup"
        launcher.write_text("#!/bin/sh\nexit 0\n")
        launcher.chmod(0o755)
        cron = self.root / "cron" / "pg-backup"
        with patch.object(scheduler, "legacy_jobs", return_value=[]):
            scheduler.enable(self.store, launcher, cron_path=cron)
        self.assertTrue(self.store.load()["schedule_enabled"])
        self.assertIn(scheduler.MARKER, cron.read_text())
        self.assertEqual(cron.stat().st_mode & 0o777, 0o644)
        scheduler.disable(self.store, cron_path=cron)
        self.assertFalse(cron.exists())
        self.assertFalse(self.store.load()["schedule_enabled"])

    def test_conflict_leaves_schedule_untouched(self):
        launcher = self.root / "pg-backup"
        launcher.write_text("#!/bin/sh\n")
        launcher.chmod(0o755)
        cron = self.root / "cron"
        with patch.object(scheduler, "legacy_jobs", return_value=[("/etc/cron.d/custom", "0 * * * * pasarguard backup")]):
            with self.assertRaises(ManagerError):
                scheduler.enable(self.store, launcher, cron_path=cron)
        self.assertFalse(cron.exists())


@unittest.skipUnless(shutil.which("age") and shutil.which("age-keygen"), "age binaries are required for encryption integration tests")
class EncryptionIntegrationTests(TemporaryStore):
    def test_interrupted_encrypted_local_backup_still_requires_saved_key_for_upload(self):
        self.store.update(encryption=True)
        ensure_key(self.store)
        record = Worker(self.store, lambda _: None, native=fake_native).create(send=False)
        record.update(status="creating")
        record.pop("encrypted")
        atomic_json(self.store.record_dir(record["id"]) / "record.json", record)
        Worker(self.store, lambda _: None).retry_pending()
        self.assertTrue(self.store.records()[0]["encrypted"])
        with self.assertRaisesRegex(ManagerError, "Save the recovery key"):
            Worker(self.store, lambda _: None).upload(record["id"])

    def test_real_age_encryption_split_and_recovery_round_trip(self):
        self.store.update(encryption=True, part_mb=1)
        config = ensure_key(self.store)
        self.assertTrue(config["recipient"].startswith("age1"))
        self.assertEqual(self.store.key_path.stat().st_mode & 0o777, 0o600)
        key_before = self.store.key_path.read_bytes()
        ensure_key(self.store)
        self.assertEqual(self.store.key_path.read_bytes(), key_before)
        record = Worker(self.store, lambda _: None, native=fake_native).create(send=False)
        folder = self.store.record_dir(record["id"])
        manifest = verify_manifest(folder / "manifest.json")
        self.assertEqual(manifest["encryption"], "age")
        self.assertEqual(len(manifest["parts"]), 2)
        self.assertFalse(list(folder.glob("*.zip")))
        self.assertFalse(list(folder.glob("*.age")))
        self.assertNotIn("AGE-SECRET-KEY", (folder / "manifest.json").read_text())
        output = self.root / "recovered.zip"
        recover(folder / "manifest.json", output, self.store.key_path, emit=lambda _: None)
        self.assertEqual(sha256(output), manifest["archive_sha256"])
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_wrong_recovery_key_leaves_no_output(self):
        self.store.update(encryption=True)
        ensure_key(self.store)
        record = Worker(self.store, lambda _: None, native=fake_native).create(send=False)
        other = Store(self.root / "other-config", self.root / "other-state")
        ensure_key(other)
        output = self.root / "recovered.zip"
        with self.assertRaises(ManagerError):
            recover(self.store.record_dir(record["id"]) / "manifest.json", output, other.key_path, emit=lambda _: None)
        self.assertFalse(output.exists())


class EntrypointTests(unittest.TestCase):
    def test_worker_import_does_not_load_ui(self):
        result = subprocess.run([sys.executable, "-c", "import pg_backup.cli, sys; assert 'curses' not in sys.modules; assert 'pg_backup.ui' not in sys.modules"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_demo_plain_exits_without_root_or_side_effects(self):
        result = subprocess.run([sys.executable, "pg-backup", "--demo", "--plain"], input="q\n", capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DEMO", result.stdout)

    def test_terminal_navigation_saved_value_and_clean_exit(self):
        import pty
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 100, 0, 0))
        environment = dict(os.environ, TERM="xterm-256color")
        process = subprocess.Popen([sys.executable, "pg-backup", "--demo"], stdin=slave, stdout=slave, stderr=slave, env=environment)
        os.close(slave)
        capture = b""
        def expect(value):
            nonlocal capture
            deadline = time.monotonic() + 5
            while value not in capture and time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    capture += os.read(master, 65536)
            self.assertIn(value, capture)
            capture = b""
        try:
            expect(b"PASARGUARD")
            os.write(master, b"4")
            expect(b"settings are remembered")
            os.write(master, b"3")
            expect(b"Destination channel ID")
            os.write(master, b"\n")
            expect(b"My Backup Channel")
            os.write(master, b"qq")
            deadline = time.monotonic() + 5
            while process.poll() is None and time.monotonic() < deadline:
                # A real terminal drains output continuously; do the same so
                # curses cannot block while restoring the terminal on exit.
                if select.select([master], [], [], 0.1)[0]:
                    try:
                        os.read(master, 65536)
                    except OSError:
                        break
            process.wait(timeout=5)
            self.assertEqual(process.returncode, 0)
            with self.assertRaises(ProcessLookupError):
                os.kill(process.pid, 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            os.close(master)


if __name__ == "__main__":
    unittest.main()
