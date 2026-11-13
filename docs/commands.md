# Command reference

[Back to the README](../README.md)

You can do most everyday tasks through `sudo pg-backup`. This page lists every command and option for running tasks directly.

The commands below use an installed manager. Server commands need `sudo`; help, the demo, the network check, and recovering downloaded files do not. From a downloaded project folder, use `python3 pg-backup` instead of `pg-backup` (or `sudo python3 pg-backup` for server commands).

## Menu and setup

| Command | What it does |
| --- | --- |
| `sudo pg-backup` | Opens the menu, starting setup if needed. |
| `sudo pg-backup menu` | Opens the same menu explicitly. |
| `sudo pg-backup setup` | Finishes or reviews setup, reusing detected and saved settings. |
| `sudo pg-backup --plain` | Opens a simple numbered menu. |
| `pg-backup --demo` | Opens a safe preview with temporary settings and no network requests or server changes. |
| `pg-backup --demo --plain` | Opens the preview as a numbered menu. |

## Backups and uploads

All backup commands use your saved settings, including whether encryption is enabled.

| Command | What it does |
| --- | --- |
| `sudo pg-backup run` | Creates a backup, uploads it to your channel, and exits. |
| `sudo pg-backup run --local-only` | Creates a local backup without uploading it. |
| `sudo pg-backup retry` | Resumes all pending uploads, then exits. Confirmed parts are skipped. |
| `sudo pg-backup list` | Lists locally stored backups, their IDs, and their states. |
| `sudo pg-backup send BACKUP_ID` | Uploads or resumes a locally stored backup. |
| `sudo pg-backup send BACKUP_ID --resend` | Sends every part again to the current channel, including parts sent previously. |
| `sudo pg-backup verify BACKUP_ID` | Checks the backup parts and complete payload against the saved checksums. |

Replace `BACKUP_ID` with an ID from `sudo pg-backup list`. It is the manager's backup ID, not a Telegram message ID. If you change the channel or sender after a partial upload, use `--resend` to send a complete copy to the new destination.

`verify` checks the stored files. It does not decrypt them or test a complete panel restore.

## Recover a downloaded backup

Recovery works on another server or computer after losing the original server. Download `manifest.json` and every listed part into the same folder. Bring your separately saved recovery key to the machine doing recovery.

Python 3.9+ and `age` are required. From the downloaded project folder:

```bash
python3 pg-backup recover /path/to/manifest.json \
  --identity /path/to/recovery-key.txt \
  --output /path/to/recovered-backup.zip
```

With an installed manager, you can use `pg-backup recover` instead of `python3 pg-backup recover`.

| Argument or option | Meaning |
| --- | --- |
| `manifest` | Path to the downloaded `manifest.json` file. Required. |
| `--identity PATH` | Path to your saved age recovery key. Required for encrypted backups; omit it for unencrypted backups. |
| `--output PATH` | Where to save the recovered archive. Required. Choose a new filename in an existing folder. |

The command checks the files, joins the parts in the correct order, decrypts the backup when needed, and checks the recovered archive. It refuses to overwrite an existing output file. It does not need root access, the original server, Telegram credentials, Docker, or a PasarGuard installation.

This produces a normal PasarGuard backup archive. To validate it on a server with PasarGuard's management script installed:

```bash
sudo pasarguard restore /path/to/recovered-backup.zip --check
```

Then follow the [official PasarGuard recovery guide](https://github.com/PasarGuard/scripts/blob/main/docs/backup-and-restore.md) to restore the panel. The manager's `recover` command does not restore over a running panel.

**Keep your recovery key separately for as long as you keep encrypted backups. Without it, those backups cannot be decrypted.**

## Status and Telegram checks

| Command | What it does |
| --- | --- |
| `sudo pg-backup status` | Prints saved settings with the bot token hidden, the latest results, pending upload count, and next run. |
| `sudo pg-backup diagnose` | Checks local tools, PasarGuard compatibility, recovery key status, and conflicting schedules. Sends no Telegram messages. |
| `pg-backup telegram-network` | Tests direct IPv4 and IPv6 DNS, HTTPS and TLS access without needing credentials or sending messages. |
| `sudo pg-backup check-telegram` | Checks the channel and the bot's posting permission. Sends no message or file. |
| `sudo pg-backup test-upload` | Checks the channel and sends a small test document there. |

If Telegram setup fails, run `pg-backup telegram-network`, then `sudo pg-backup check-telegram`. The manager tries IPv4 first and falls back to IPv6, so IPv6 failure alone does not prevent an IPv4 connection. Network errors identify DNS, certificate, timeout, or HTTP response problems without showing your token. A successful network check confirms connectivity; the channel check verifies the bot and posting permissions.

## Scheduling and encryption setup

| Command | What it does |
| --- | --- |
| `sudo pg-backup enable` | Enables the saved automatic backup schedule after setup is complete. |
| `sudo pg-backup disable` | Removes this manager's schedule while keeping settings, keys, and backups. |
| `sudo pg-backup key-init` | Prepares the age recovery key once and prints its location. Existing keys are preserved. |
| `sudo pg-backup run --scheduled` | Used by the scheduler: retries pending uploads, creates a backup, and exits. Does nothing if automatic backups are disabled. |

Change the schedule through the menu. It follows the server's local timezone. Setup must be complete before enabling it; with encryption enabled, you must also confirm in the menu that you saved the recovery key separately. `key-init` does not make that confirmation for you.

## Help and global options

```bash
pg-backup --help
pg-backup recover --help
pg-backup --version
```

Every command accepts `--help` to show its own usage.

| Option | Meaning |
| --- | --- |
| `-h`, `--help` | Shows help and exits. |
| `--version` | Shows the manager version and exits. |
| `--demo` | Uses a safe interactive preview. For the menu and setup only. |
| `--plain` | Uses the numbered-menu interface. |
| `--config-dir PATH` | Uses a different settings and recovery-key directory. Default: `/etc/pg-backup`. |
| `--state-dir PATH` | Uses a different backup and status directory. Default: `/var/lib/pg-backup`. |

Place global options **before** the command, for example:

```bash
sudo pg-backup --config-dir /etc/pg-backup --state-dir /var/lib/pg-backup status
```

The directory options are for custom installations. Keep using the same directories when opening the menu, running commands, or managing the schedule so they share the same settings and backups.

## Installer options

Run these from the downloaded project folder on your Linux server:

| Command | What it does |
| --- | --- |
| `sudo bash install.sh` | Installs the manager and missing tools, then opens the menu when run in a terminal. |
| `sudo bash install.sh --no-menu` | Installs without opening the menu. |
| `sudo bash install.sh --no-deps` | Installs using tools you have already installed with your package manager. |
| `bash install.sh --help` | Shows installer usage. |

Automatic dependency installation uses APT on Debian/Ubuntu. The manager does not install or update PasarGuard itself. See [removal instructions](../README.md#removal) to uninstall while keeping your keys and backups.
