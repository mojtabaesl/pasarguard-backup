"""Original, event-driven terminal UI. Imported only for interactive commands."""
from __future__ import annotations

import copy
import getpass
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading

from . import __version__, scheduler
from .backup import human_size
from .config import ManagerError, dotenv, mask, read_json, validate


def clean(text):
    return "".join(char for char in str(text) if char.isprintable())


class PlainView:
    def choose(self, title, options, details=()):
        print("\n◆ " + title + "\n" + "─" * 60)
        for line in details:
            print("  " + clean(line))
        print()
        for index, option in enumerate(options, 1):
            print("  {}. {}".format(index, clean(option)))
        while True:
            try:
                value = input("\nSelect [Enter = 1, q = back]: ").strip()
            except EOFError:
                return None
            if value.lower() in ("q", "0"):
                return None
            value = value or "1"
            if value.isdigit() and 1 <= int(value) <= len(options):
                return int(value) - 1

    def ask(self, title, current="", secret=False):
        hint = "saved value" if secret and current else current
        label = "{} [{}; Enter keeps it, /cancel goes back]: ".format(title, hint or "required")
        try:
            value = getpass.getpass(label) if secret else input(label)
        except EOFError:
            return None
        return None if value == "/cancel" else value or current

    def message(self, title, lines):
        self.choose(title, ["Continue"], lines)

    def job(self, command, title):
        print("\n◆ " + title)
        result = subprocess.run(command)
        self.message("Finished" if result.returncode == 0 else "Action needs attention", ["Exit status: " + str(result.returncode)])
        return result.returncode == 0


class TerminalView:
    def __init__(self, screen, curses):
        self.screen, self.c = screen, curses
        self.screen.keypad(True)
        self.screen.timeout(-1)  # Block until a key arrives. No idle refresh loop.
        self.colors = False
        if curses.has_colors():
            curses.start_color()
            try:
                curses.use_default_colors()
                background = -1
            except curses.error:
                background = curses.COLOR_BLACK
            palette = [curses.COLOR_CYAN, curses.COLOR_MAGENTA, curses.COLOR_GREEN, curses.COLOR_WHITE, curses.COLOR_YELLOW, curses.COLOR_RED]
            for index, color in enumerate(palette, 1):
                curses.init_pair(index, color, background)
            self.colors = True
        try:
            curses.curs_set(0)
        except curses.error:
            pass

    def text(self, y, x, text, color=0, bold=False, reverse=False):
        rows, columns = self.screen.getmaxyx()
        if not 0 <= y < rows or not 0 <= x < columns - 1:
            return
        style = self.c.color_pair(color) if color and self.colors else 0
        if bold:
            style |= self.c.A_BOLD
        if reverse:
            style |= self.c.A_REVERSE
        try:
            self.screen.addnstr(y, x, clean(text), max(0, columns - x - 2), style)
        except self.c.error:
            pass

    def frame(self, title, details=()):
        self.screen.erase()
        rows, columns = self.screen.getmaxyx()
        width = min(columns - 4, 92)
        if width < 34 or rows < 12:
            self.text(0, 0, "Please enlarge your terminal to at least 40 × 12.", 5)
            self.text(2, 0, "q closes the menu. --plain uses a smaller UI.")
            self.screen.refresh()
            return 4
        self.text(1, 2, "╭" + "─" * (width - 2) + "╮", 1)
        self.text(2, 2, "│" + " " * (width - 2) + "│", 1)
        self.text(2, 5, "◆ PASARGUARD", 1, bold=True)
        self.text(2, 20, "BACKUP MANAGER", 2, bold=True)
        if width > 60:
            self.text(2, width - 8, "v" + __version__, 4)
        self.text(3, 2, "╰" + "─" * (width - 2) + "╯", 1)
        self.text(5, 4, title, 2, bold=True)
        y = 7
        for line in list(details)[:max(0, min(7, rows - 17))]:
            self.text(y, 4, line, 4)
            y += 1
        self.text(y, 4, "─" * max(0, width - 4), 1)
        return y + 2

    def choose(self, title, options, details=()):
        selected = 0
        while True:
            start = self.frame(title, details)
            rows, columns = self.screen.getmaxyx()
            count = max(1, rows - start - 3)
            offset = max(0, selected - count + 1)
            for index in range(offset, min(len(options), offset + count)):
                active = index == selected
                line = "{} {:>2}  {}".format("❯" if active else " ", index + 1, options[index])
                self.text(start + index - offset, 4, line, 1 if active else 4, bold=active, reverse=active)
            self.text(rows - 2, 4, "↑ ↓ move  ·  Enter select  ·  1–9 shortcut  ·  q back", 1)
            if offset or offset + count < len(options):
                self.text(rows - 3, 4, "{}/{}  ·  scroll for more".format(selected + 1, len(options)), 5)
            self.screen.refresh()
            try:
                key = self.screen.get_wch()
            except self.c.error:
                continue
            if key in ("q", "Q", "\x1b", "0"):
                return None
            if key in (self.c.KEY_DOWN, "j", "\t"):
                selected = (selected + 1) % len(options)
            elif key in (self.c.KEY_UP, "k", self.c.KEY_BTAB):
                selected = (selected - 1) % len(options)
            elif key in (self.c.KEY_HOME,):
                selected = 0
            elif key in (self.c.KEY_END,):
                selected = len(options) - 1
            elif key in ("\n", "\r", self.c.KEY_ENTER):
                return selected
            elif isinstance(key, str) and key in "123456789" and int(key) <= len(options):
                return int(key) - 1

    def ask(self, title, current="", secret=False):
        value = ""
        position = 0
        while True:
            start = self.frame(title, ["Current: " + (mask(current) if secret else (str(current) or "Not configured")), "Enter keeps the current value. Esc cancels."])
            width = max(10, self.screen.getmaxyx()[1] - 12)
            display = "•" * len(value) if secret else value
            offset = max(0, position - width + 1)
            self.text(start, 4, "› " + display[offset:offset + width], 1)
            try:
                self.c.curs_set(1)
                self.screen.move(start, min(self.screen.getmaxyx()[1] - 2, 6 + position - offset))
            except self.c.error:
                pass
            self.screen.refresh()
            key = self.screen.get_wch()
            if key in ("\n", "\r", self.c.KEY_ENTER, "\x1b"):
                try:
                    self.c.curs_set(0)
                except self.c.error:
                    pass
                return None if key == "\x1b" else value or str(current)
            if key in (self.c.KEY_BACKSPACE, "\x7f", "\b"):
                if position:
                    value = value[:position - 1] + value[position:]
                    position -= 1
            elif key == self.c.KEY_DC:
                value = value[:position] + value[position + 1:]
            elif key == self.c.KEY_LEFT:
                position = max(0, position - 1)
            elif key == self.c.KEY_RIGHT:
                position = min(len(value), position + 1)
            elif key == self.c.KEY_HOME:
                position = 0
            elif key == self.c.KEY_END:
                position = len(value)
            elif key == "\x15":
                value, position = "", 0
            elif isinstance(key, str) and key.isprintable() and len(value) < 4096:
                value = value[:position] + key + value[position:]
                position += 1

    def message(self, title, lines):
        # All lines remain accessible even in small terminals.
        self.choose(title, ["Continue"], ()) if not lines else self._document(title, list(lines))

    def _document(self, title, lines):
        import textwrap
        offset = 0
        while True:
            start = self.frame(title)
            rows, columns = self.screen.getmaxyx()
            wrapped = []
            for line in lines:
                wrapped.extend(textwrap.wrap(clean(line), max(12, columns - 10), replace_whitespace=False) or [""])
            count = max(1, rows - start - 3)
            offset = min(offset, max(0, len(wrapped) - count))
            for index, line in enumerate(wrapped[offset:offset + count]):
                self.text(start + index, 4, line, 4)
            self.text(rows - 2, 4, "↑ ↓ scroll  ·  Enter / q back", 1)
            self.screen.refresh()
            key = self.screen.get_wch()
            if key in ("q", "\x1b", "\n", "\r", self.c.KEY_ENTER):
                return
            if key in (self.c.KEY_DOWN, "j"):
                offset += 1
            elif key in (self.c.KEY_UP, "k"):
                offset = max(0, offset - 1)

    def job(self, command, title):
        events = queue.Queue()
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True, bufsize=1)
        def read_output():
            for line in process.stdout:
                events.put(line.rstrip())
            events.put(None)
        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        lines = []
        self.screen.timeout(100)  # Only while a foreground action is active.
        cancelled = False
        try:
            finished = False
            while not finished:
                try:
                    while True:
                        line = events.get_nowait()
                        if line is None:
                            finished = True
                            break
                        lines.append(line)
                except queue.Empty:
                    pass
                start = self.frame(title, ["Working…  Ctrl+C cancels this action."])
                rows, _ = self.screen.getmaxyx()
                for index, line in enumerate(lines[-max(1, rows - start - 3):]):
                    self.text(start + index, 4, line, 4)
                self.screen.refresh()
                if finished:
                    break
                try:
                    self.screen.get_wch()
                except self.c.error:
                    pass
        except KeyboardInterrupt:
            cancelled = True
            os.killpg(process.pid, signal.SIGINT)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
        finally:
            process.wait()
            reader.join(timeout=2)
            process.stdout.close()
            self.screen.timeout(-1)
        self.message("Cancelled" if cancelled else "Completed" if process.returncode == 0 else "Action needs attention", lines or ["The action finished without additional output."])
        return not cancelled and process.returncode == 0


class Controller:
    def __init__(self, view, store, demo=False):
        self.view, self.store, self.demo = view, store, demo

    def details(self):
        c = self.store.load()
        source = c.get("sources", {})
        status = read_json(self.store.state_dir / "status.json")
        pending = sum(r["status"] in ("pending", "uploading") for r in self.store.records())
        return [
            "CHANNEL     {}  ·  Direct HTTPS".format(c["channel_title"] or c["channel_id"] or "Setup needed"),
            "SCHEDULE    {}  [{}]{}".format(scheduler.schedule_label(c["schedule"]), source.get("schedule", "Default"), "" if c["schedule_enabled"] else " · Paused"),
            "STORAGE     Keep {} uploaded backups  ·  Encryption {}".format(c["retention"], "on" if c["encryption"] else "off"),
            "LAST RUN    {}  ·  {} pending".format(status.get("last_result", "No backups yet"), pending),
            "NEXT RUN    " + (scheduler.next_run(c["schedule"]) if c["schedule_enabled"] else "Automatic backups are paused"),
            "DEMO · No network or server changes" if self.demo else "SETTINGS    Detected / Default / Saved · reused everywhere",
        ]

    def job(self, args, title):
        if self.demo:
            self.view.message(title + " · demo", ["This is a safe preview. No backup, upload, or server change was performed.", "On your server, this action runs in a foreground worker and exits when finished."])
            return True
        launcher = Path(__file__).resolve().parent.parent / "pg-backup"
        command = [sys.executable, str(launcher), "--config-dir", str(self.store.config_dir), "--state-dir", str(self.store.state_dir)] + args
        return self.view.job(command, title)

    def edit(self, key, title, secret=False, integer=False):
        c = self.store.load()
        value = self.view.ask(title, c[key], secret)
        if value is None:
            return False
        try:
            if integer:
                value = int(value)
            candidate = copy.deepcopy(c)
            candidate[key] = value
            validate(candidate)
            changes = {key: value}
            if key in ("channel_id", "bot_token") and value != c[key]:
                changes.update(channel_title="", channel_verified_at="")
            self.store.update(**changes)
            return True
        except (ValueError, ManagerError) as exc:
            self.view.message("Please check this value", [str(exc)])
            return False

    def run(self, setup=False):
        if setup or not self.store.load()["setup_complete"]:
            self.setup()
        while True:
            try:
                selected = self.view.choose("Your backups, one place" if not self.demo else "Explore your backup manager", [
                    "Back up now → Telegram channel", "Backups and pending uploads", "Automatic backup schedule",
                    "Telegram channel settings", "Storage and encryption", "Status, logs and diagnostics", "Installation settings", "Exit",
                ], self.details())
                if selected in (None, 7):
                    return
                actions = [self.backup_now, self.backups, self.schedule, self.telegram, self.storage, self.status, self.installation]
                actions[selected]()
            except ManagerError as exc:
                self.view.message("Action needs attention", [str(exc)])

    def setup(self):
        c = self.store.load()
        self.store.save(c)
        selected = self.view.choose("Welcome · most settings are ready", ["Continue with detected settings", "Check installation paths", "Return to menu"], [
            "PasarGuard: " + c["app_dir"], "Database: " + c["database"],
            "Defaults: every 6h · keep 7 · encrypt · direct Telegram", "Only missing information will be requested."])
        if selected in (None, 2):
            return
        if selected == 1:
            self.installation()
        for key, title in (("bot_token", "Bot token from @BotFather"), ("channel_id", "Channel ID (-100…) or @channel_username")):
            while not self.store.load()[key]:
                if not self.edit(key, title, secret=key == "bot_token"):
                    return
        if not self.job(["check-telegram"], "Checking your Telegram channel"):
            return
        if self.store.load()["encryption"]:
            if not self.job(["key-init"], "Preparing encryption"):
                return
            if not self.store.load()["key_backed_up"]:
                self.recovery_key()
            if not self.store.load()["key_backed_up"]:
                return
        if not self.job(["diagnose"], "Checking server dependencies"):
            self.view.message("Setup is saved", ["Fix the items shown by diagnostics, then choose Finish setup in Installation settings.", "Your Telegram settings and recovery key will be reused."])
            return
        self.store.update(setup_complete=True)
        while True:
            selected = self.view.choose("Ready · review your defaults", ["Save and enable automatic backups", "Save with automatic backups paused", "Review every setting and default", "Change settings"], self.details())
            if selected == 0:
                self.set_enabled(True)
                return
            if selected == 2:
                self.review_settings()
            elif selected == 3:
                self.storage()
            else:
                if selected == 1 and self.store.load()["schedule_enabled"]:
                    self.set_enabled(False)
                return

    def review_settings(self):
        c = self.store.load()
        sources = c.get("sources", {})
        labels = {
            "app_dir": "PasarGuard directory", "data_dir": "Data directory", "native_cli": "Native script",
            "database": "Database", "server_name": "Server label", "timezone": "Server timezone",
            "channel_id": "Channel ID", "bot_token": "Sender token", "schedule": "Schedule",
            "retention": "Uploaded backups to keep", "encryption": "Encryption enabled",
            "part_mb": "Upload part size (MB)", "retries": "Upload retry count", "min_free_mb": "Minimum free space (MB)",
        }
        lines = []
        for key, label in labels.items():
            value = mask(c[key]) if key == "bot_token" else str(c[key])
            lines.append("{}: {} [{}]".format(label, value, sources.get(key, "Default")))
        lines.extend(["Telegram connection: Direct HTTPS", "Settings: " + str(self.store.config_path), "Backups: " + str(self.store.backups_dir)])
        self.view.message("All settings · detected / default / saved", lines)

    def backup_now(self):
        if not self.store.load()["setup_complete"] and not self.demo:
            self.setup()
            if not self.store.load()["setup_complete"]:
                return
        self.job(["run"], "Backup → encrypt → Telegram channel")

    def telegram(self):
        while True:
            c = self.store.load()
            selected = self.view.choose("Telegram channel · settings are remembered", ["Check connection and posting permissions", "Send a small test file to this channel", "Change destination channel", "Change sender bot token", "Test direct IPv4 / IPv6 connection", "Back"], [
                "Channel: " + (c["channel_title"] or c["channel_id"] or "Not configured"),
                "Sender token: " + mask(c["bot_token"]), "Connection: Direct HTTPS · no proxy",
                "The bot publishes into the channel; it is not the destination."])
            if selected in (None, 5):
                return
            if selected == 0:
                self.job(["check-telegram"], "Checking channel permissions")
            elif selected == 1:
                self.job(["test-upload"], "Sending a test file to your channel")
            elif selected == 2:
                self.edit("channel_id", "Destination channel ID or @username")
            elif selected == 3:
                self.edit("bot_token", "Sender bot token", secret=True)
            elif selected == 4:
                self.job(["telegram-network"], "Testing direct Telegram access")

    def set_enabled(self, enabled):
        if self.demo:
            self.store.update(schedule_enabled=enabled)
            return True
        if not enabled:
            scheduler.disable(self.store)
            return True
        conflicts = scheduler.legacy_jobs()
        migrate = False
        if conflicts:
            migratable = all(source == "root crontab" and scheduler.NATIVE_MARKER in line for source, line in conflicts)
            if not migratable:
                self.view.message("Existing schedule found", ["An existing PasarGuard backup job must be reviewed before enabling this schedule."] + [source + ": " + line for source, line in conflicts])
                return False
            selected = self.view.choose("Use one backup schedule", ["Replace the old PasarGuard schedule with this manager", "Keep the existing schedule; leave this manager paused"], ["A tagged PasarGuard backup job already exists in root's crontab.", "Replacement removes only that tagged job; other cron entries are preserved."])
            if selected != 0:
                return False
            migrate = True
        launcher = Path(__file__).resolve().parent.parent / "pg-backup"
        scheduler.enable(self.store, launcher, migrate=migrate)
        return True

    def schedule(self):
        while True:
            c = self.store.load()
            selected = self.view.choose("Automatic backups", ["Pause backups" if c["schedule_enabled"] else "Enable backups", "Change schedule", "Back"], [
                "Current: " + scheduler.schedule_label(c["schedule"]), "Cron: " + c["schedule"],
                "Timezone: " + c["timezone"] + " (server local time)",
                "Next: " + (scheduler.next_run(c["schedule"]) if c["schedule_enabled"] else "Paused"),
                "No manager process runs between scheduled jobs."])
            if selected in (None, 2):
                return
            if selected == 0:
                self.set_enabled(not c["schedule_enabled"])
            elif selected == 1:
                choices = ["Every 6 hours [Default]", "Every hour", "Every 2 hours", "Every 12 hours", "Daily at 03:00", "Custom cron expression", "Back"]
                value = self.view.choose("Choose your schedule", choices, ["Current: " + c["schedule"]])
                if value in (None, 6):
                    continue
                expression = ["0 */6 * * *", "0 * * * *", "0 */2 * * *", "0 */12 * * *", "0 3 * * *"][value] if value < 5 else self.view.ask("Five-field cron expression", c["schedule"])
                if expression:
                    scheduler.parse_schedule(expression)
                    self.store.update(schedule=expression)
                    if c["schedule_enabled"]:
                        try:
                            if not self.set_enabled(True):
                                self.store.update(schedule=c["schedule"])
                        except BaseException:
                            self.store.update(schedule=c["schedule"])
                            raise

    def recovery_key(self):
        c = self.store.load()
        while True:
            selected = self.view.choose("Save your recovery key separately", ["Show recovery key for copying", "I have saved this key outside the server", "Back"], [
                "Key file: " + str(self.store.key_path),
                "Keep a copy on your own computer or in a password manager.",
                "Keep it separate from the backup Telegram channel.",
                "Saved separately: " + ("Yes" if c["key_backed_up"] else "Not confirmed yet")])
            if selected in (None, 2):
                return
            if selected == 0:
                if self.demo:
                    self.view.message("Demo recovery key", ["The real recovery key is shown here only when you choose this action."])
                elif self.store.key_path.is_file():
                    self.view.message("Private recovery key · save separately", self.store.key_path.read_text().splitlines())
                else:
                    self.view.message("Key not generated", ["Choose Prepare encryption first."])
            elif selected == 1:
                if self.demo or self.store.key_path.is_file():
                    self.store.update(key_backed_up=True)
                    return
                self.view.message("Key not generated", ["Choose Prepare encryption first."])

    def storage(self):
        while True:
            c = self.store.load()
            selected = self.view.choose("Storage and encryption", ["Create a local backup only", "Change successful local backup retention", "Recovery key / confirm separate copy", "Prepare encryption", "Disable encryption" if c["encryption"] else "Enable encryption", "Change upload retries", "Change minimum free disk space", "Back"], [
                "Files: " + str(self.store.backups_dir), "Retention: {} uploaded backups [default: 7]".format(c["retention"]),
                "Encryption: " + ("age · enabled" if c["encryption"] else "disabled"),
                "Upload parts: 45 MB maximum · Retry count: " + str(c["retries"]),
                "Pending uploads and local-only backups are kept automatically."])
            if selected in (None, 7):
                return
            if selected == 0:
                self.job(["run", "--local-only"], "Creating a local backup")
            elif selected == 1:
                self.edit("retention", "Keep this many uploaded backups locally", integer=True)
            elif selected == 2:
                self.recovery_key()
            elif selected == 3:
                self.job(["key-init"], "Preparing encryption")
            elif selected == 4:
                if c["encryption"]:
                    if self.view.choose("Send future backups without encryption?", ["Keep encryption enabled", "Disable encryption for future backups"], ["Backups include database contents and application secrets."]) == 1:
                        self.store.update(encryption=False)
                else:
                    self.store.update(encryption=True)
                    self.job(["key-init"], "Preparing encryption")
            elif selected == 5:
                self.edit("retries", "Upload retries [default: 3]", integer=True)
            elif selected == 6:
                self.edit("min_free_mb", "Minimum free space in MB [default: 1024]", integer=True)

    def backups(self):
        while True:
            records = self.store.records()
            options = ["Retry all pending uploads"] + ["{} · {} · {}".format(r["id"], r["status"], human_size(r.get("size", 0))) for r in records] + ["Back"]
            selected = self.view.choose("Your backups", options, ["{} backup(s) · {} in recorded payloads".format(len(records), human_size(sum(r.get("size", 0) for r in records))), "Partial uploads resume from confirmed parts."])
            if selected is None or selected == len(options) - 1:
                return
            if selected == 0:
                self.job(["retry"], "Retrying pending uploads")
                continue
            record = records[selected - 1]
            action = self.view.choose(record["id"], ["Resume / send to configured channel", "Verify file checksums", "Recovery instructions", "Resend every part to current channel", "Delete this local backup", "Back"], ["State: " + record["status"], "Size: " + human_size(record.get("size", 0)), "Last error: " + (record.get("error") or "None")])
            if action == 0:
                self.job(["send", record["id"]], "Sending backup to channel")
            elif action == 1:
                self.job(["verify", record["id"]], "Checking backup parts")
            elif action == 2:
                folder = self.store.record_dir(record["id"])
                self.view.message("Recover this backup", [
                    "1. Download manifest.json and every part listed in it into the same directory.",
                    "2. Run: pg-backup recover /path/to/manifest.json --identity /path/to/recovery-key.txt --output /path/to/backup.zip",
                    "3. Run: sudo pasarguard restore /path/to/backup.zip --check",
                    "4. Follow PasarGuard's native restore instructions after validating the archive.",
                    "Local directory: " + str(folder), "The manager does not restore over a running panel."])
            elif action == 3:
                if self.view.choose("Resend complete backup?", ["Cancel", "Send all parts to the currently configured channel"], ["This creates another complete upload, including parts sent previously."]) == 1:
                    self.job(["send", record["id"], "--resend"], "Resending complete backup")
            elif action == 4:
                if self.view.choose("Delete local copy?", ["Keep backup", "Delete this local backup permanently"], [record["id"], "Telegram posts are unaffected."]) == 1:
                    from .config import file_lock
                    import shutil
                    with file_lock(self.store.state_dir / "worker.lock", blocking=False):
                        shutil.rmtree(self.store.record_dir(record["id"]))

    def status(self):
        selected = self.view.choose("Status and diagnostics", ["Show recent result", "Run local diagnostics", "View last native backup log", "Review saved settings and defaults", "Back"], self.details())
        if selected == 0:
            status = read_json(self.store.state_dir / "status.json")
            self.view.message("Latest backup status", ["{}: {}".format(key.replace("_", " "), value) for key, value in status.items()] or ["No backup has run yet."])
        elif selected == 1:
            self.job(["diagnose"], "Checking your server")
        elif selected == 2:
            records = self.store.records()
            if records:
                path = self.store.record_dir(records[0]["id"]) / "native.log"
                if path.is_file():
                    # Native logs stay private and are never sent to Telegram.
                    with path.open(errors="replace") as stream:
                        from collections import deque
                        lines = list(deque(stream, maxlen=300))
                    self.view.message("Private native backup log", lines)
        elif selected == 3:
            self.review_settings()

    def installation(self):
        while True:
            c = self.store.load()
            selected = self.view.choose("Installation settings", ["Finish / review setup", "Change PasarGuard installation path", "Change PasarGuard data path", "Change native management script path", "Change server label", "Back"], ["Application: " + c["app_dir"], "Data: " + c["data_dir"], "Native script: " + c["native_cli"], "Server label: " + c["server_name"]])
            if selected in (None, 5):
                return
            if selected == 0:
                self.setup()
                return
            key, title = [("app_dir", "PasarGuard installation directory"), ("data_dir", "PasarGuard data directory"), ("native_cli", "Native pasarguard script path"), ("server_name", "Server name in backup captions")][selected - 1]
            if self.edit(key, title) and key == "app_dir":
                env = dotenv(Path(self.store.load()["app_dir"]) / ".env")
                current = self.store.load()
                changes = {}
                for field, names in {"bot_token": ("BACKUP_TELEGRAM_BOT_KEY", "TELEGRAM_TOKEN", "TELEGRAM_API_TOKEN"), "channel_id": ("BACKUP_TELEGRAM_CHAT_ID", "TELEGRAM_CHAT_ID")}.items():
                    if not current[field]:
                        for name in names:
                            if env.get(name):
                                changes[field] = env[name]
                                break
                database_url = env.get("SQLALCHEMY_DATABASE_URL", "")
                if database_url:
                    changes["database"] = {"sqlite": "SQLite", "mysql": "MySQL / MariaDB", "postgresql": "PostgreSQL / TimescaleDB"}.get(database_url.split(":", 1)[0].split("+", 1)[0], "Custom database")
                if changes:
                    self.store.update(**changes)


def launch(store, plain=False, demo=False, setup=False):
    if plain or os.environ.get("TERM") == "dumb" or not sys.stdout.isatty():
        Controller(PlainView(), store, demo).run(setup)
        return
    try:
        import curses
    except ImportError:
        Controller(PlainView(), store, demo).run(setup)
        return
    try:
        curses.wrapper(lambda screen: Controller(TerminalView(screen, curses), store, demo).run(setup))
    except curses.error:
        Controller(PlainView(), store, demo).run(setup)
