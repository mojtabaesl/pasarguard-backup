#!/usr/bin/env bash
# Adapter around the installed, trusted PasarGuard backup engine.
# Delivery is deliberately owned by the manager, after encryption.
set -e
export PASARGUARD_SOURCE_ONLY=true
export BACKUP_SPLIT_SIZE_BYTES=9223372036854775807
export BACKUP_SPLIT_THRESHOLD_BYTES=9223372036854775807
source "$1"
declare -F backup_command >/dev/null
send_backup_to_telegram() { :; }
send_backup_error_to_telegram() { :; }
backup_command
