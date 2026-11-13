#!/usr/bin/env bash
# Install a finite-job CLI. This does not install a manager daemon or schedule.
set -euo pipefail

SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/opt/pg-backup"
INSTALL_DEPS=1
OPEN_MENU=1
for argument in "$@"; do
    case "$argument" in
        --no-deps) INSTALL_DEPS=0 ;;
        --no-menu) OPEN_MENU=0 ;;
        -h|--help)
            printf 'Usage: sudo bash install.sh [--no-deps] [--no-menu]\n'
            exit 0 ;;
        *) printf 'Unknown option: %s\n' "$argument" >&2; exit 1 ;;
    esac
done
[[ "$(uname -s)" == Linux ]] || { printf 'Install on your Linux PasarGuard server. For a local preview: python3 pg-backup --demo\n' >&2; exit 1; }
[[ $EUID == 0 ]] || { printf 'Please run: sudo bash install.sh\n' >&2; exit 1; }
[[ -f "$SOURCE_DIR/pg-backup" && -d "$SOURCE_DIR/pg_backup" ]] || { printf 'Run the installer from the complete project checkout.\n' >&2; exit 1; }

missing=()
for command_name in python3 age age-keygen curl zip unzip sqlite3 crontab; do
    command -v "$command_name" >/dev/null 2>&1 || missing+=("$command_name")
done
if (( ${#missing[@]} )); then
    if (( INSTALL_DEPS )) && command -v apt-get >/dev/null 2>&1; then
        printf 'Installing missing backup tools using APT…\n'
        apt-get update
        DEBIAN_FRONTEND=noninteractive apt-get install -y python3 age curl zip unzip sqlite3 cron ca-certificates
    else
        printf 'Install these tools with your package manager first: %s\n' "${missing[*]}" >&2
        printf 'Then run: sudo bash install.sh --no-deps\n' >&2
        exit 1
    fi
fi
python3 -c 'import sys; assert sys.version_info >= (3, 9), "Python 3.9 or later is required"'

staging="$(mktemp -d /opt/.pg-backup-install.XXXXXX)"
trap 'rm -rf -- "$staging"' EXIT
install -m 0755 "$SOURCE_DIR/pg-backup" "$staging/pg-backup"
mkdir "$staging/pg_backup"
for source_file in "$SOURCE_DIR"/pg_backup/*.py "$SOURCE_DIR"/pg_backup/*.sh; do
    install -m 0644 "$source_file" "$staging/pg_backup/"
done
install -m 0644 "$SOURCE_DIR/README.md" "$staging/README.md"
chmod 0755 "$staging"
# Keep settings, keys and backups outside the application directory.
install -d -m 0700 /etc/pg-backup /var/lib/pg-backup
if [[ -e "$INSTALL_DIR" && ! -d "$INSTALL_DIR/pg_backup" ]]; then
    printf '%s already exists and is not a manager installation.\n' "$INSTALL_DIR" >&2
    exit 1
fi
if [[ -d "$INSTALL_DIR" ]]; then
    # Existing code remains recoverable if publishing the new copy fails.
    previous="$(mktemp -d /opt/.pg-backup-previous.XXXXXX)"
    rmdir "$previous"
    mv "$INSTALL_DIR" "$previous"
    if ! mv "$staging" "$INSTALL_DIR"; then
        mv "$previous" "$INSTALL_DIR"
        exit 1
    fi
    rm -rf -- "$previous"
else
    mv "$staging" "$INSTALL_DIR"
fi
ln -sfn "$INSTALL_DIR/pg-backup" /usr/local/bin/pg-backup

# Cron is the OS scheduler; no pg-backup service is created.
if command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]]; then
    if systemctl cat cron.service >/dev/null 2>&1; then
        systemctl enable --now cron
    elif systemctl cat crond.service >/dev/null 2>&1; then
        systemctl enable --now crond
    else
        printf 'Ensure your operating system cron daemon is running before enabling backups.\n'
    fi
elif [[ -x /etc/init.d/cron ]] && command -v service >/dev/null 2>&1; then
    service cron start
else
    printf 'Ensure your operating system cron daemon is running before enabling backups.\n'
fi
printf '\n✓ Installed. Open the manager with: sudo pg-backup\n'
printf 'No backup schedule is enabled until you finish setup.\n'
if (( OPEN_MENU )) && [[ -t 0 && -t 1 ]]; then
    exec /usr/local/bin/pg-backup
fi
