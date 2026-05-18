#!/bin/bash
# backup.sh - Creates a backup of the Telegram bot database.
#
# Secrets in `.env` are intentionally NOT backed up. Restore secrets manually
# from your password manager / secret store, not from disk-side artifacts that
# can be exfiltrated together with the DB dump.

set -euo pipefail

# Load environment variables if .env exists
if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi

DB_USER="${POSTGRES_USER:-telegramsellbot}"
DB_NAME="${POSTGRES_DB:-telegramsellbot}"

BACKUP_DIR="backups"
TIMESTAMP="$(date +"%Y%m%d_%H%M%S")"
BACKUP_FILE="${BACKUP_DIR}/telegramsellbot_backup_${TIMESTAMP}.sql.gz"

echo "============================================="
echo "   TelegramSellBot Backup Utility"
echo "============================================="

umask 077
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

echo "[1/1] Backing up PostgreSQL Database..."
if docker ps --format '{{.Names}}' | grep -q "^telegramsellbot-postgres$"; then
    docker exec telegramsellbot-postgres pg_dump -U "$DB_USER" -d "$DB_NAME" -F p \
        | gzip > "$BACKUP_FILE"
    chmod 600 "$BACKUP_FILE"
    echo "  -> Database backup saved to: $BACKUP_FILE (mode 600)"
else
    echo "  -> ERROR: Postgres container 'telegramsellbot-postgres' is not running!"
    exit 1
fi

echo "============================================="
echo " Backup completed successfully."
echo " NOTE: .env is NOT in this archive. Restore secrets from your"
echo "       password manager / secret store separately."
echo ""
echo " To restore on a new server, copy '$BACKUP_FILE' to the target,"
echo " run ./setup.sh to recreate .env, then run:"
echo "   ./restore.sh $BACKUP_FILE"
echo "============================================="
