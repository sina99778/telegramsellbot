#!/bin/bash
# backup.sh - Creates a backup of the Telegram bot database and environment variables

set -e

# Load environment variables if .env exists
if [ -f .env ]; then
    export $(grep -v '^#' .env | xargs)
fi

DB_USER=${POSTGRES_USER:-telegramsellbot}
DB_NAME=${POSTGRES_DB:-telegramsellbot}

BACKUP_DIR="backups"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
BACKUP_FILE="${BACKUP_DIR}/telegramsellbot_backup_${TIMESTAMP}.sql.gz"
ENV_BACKUP="${BACKUP_DIR}/env_backup_${TIMESTAMP}.env"

echo "============================================="
echo "   TelegramSellBot Backup Utility"
echo "============================================="

# Ensure backup directory exists
mkdir -p "$BACKUP_DIR"

echo "[1/2] Backing up PostgreSQL Database..."
if docker ps | grep -q "telegramsellbot-postgres"; then
    docker exec telegramsellbot-postgres pg_dump -U "$DB_USER" -d "$DB_NAME" -F p | gzip > "$BACKUP_FILE"
    echo "  -> Database backup saved to: $BACKUP_FILE"
else
    echo "  -> ERROR: Postgres container 'telegramsellbot-postgres' is not running!"
    exit 1
fi

echo "[2/2] Backing up Environment Variables..."
if [ -f .env ]; then
    cp .env "$ENV_BACKUP"
    echo "  -> Environment variables saved to: $ENV_BACKUP"
else
    echo "  -> WARNING: .env file not found. Skipping."
fi

echo "============================================="
echo " Backup completed successfully! ✅"
echo ""
echo " To restore this backup on a new server, copy the 'backups' folder,"
echo " run ./setup.sh to configure the environment, and then run:"
echo " ./restore.sh $BACKUP_FILE"
echo "============================================="
