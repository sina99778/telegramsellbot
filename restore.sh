#!/bin/bash
# restore.sh - Restores the database from a backup

set -e

echo "============================================="
echo "   TelegramSellBot Restore Utility"
echo "============================================="

if [ "$#" -ne 1 ]; then
    echo "Usage: ./restore.sh <backup_file.sql.gz>"
    echo "Example: ./restore.sh backups/telegramsellbot_backup_20260101_120000.sql.gz"
    exit 1
fi

BACKUP_FILE=$1

if [ ! -f "$BACKUP_FILE" ]; then
    echo "Error: File $BACKUP_FILE not found!"
    exit 1
fi

# Load environment variables if .env exists
if [ -f .env ]; then
    export $(grep -v '^#' .env | xargs)
fi

DB_USER=${POSTGRES_USER:-telegramsellbot}
DB_NAME=${POSTGRES_DB:-telegramsellbot}

echo "WARNING: This will drop and recreate the '$DB_NAME' database."
echo "         All current data will be LOST and replaced with the backup."
read -p "Are you sure you want to continue? (y/N) " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Restore cancelled."
    exit 1
fi

echo "[1/3] Terminating active database connections..."
docker exec telegramsellbot-postgres psql -U "$DB_USER" -d postgres -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$DB_NAME';" > /dev/null 2>&1 || true

echo "[2/3] Dropping and recreating database..."
docker exec telegramsellbot-postgres psql -U "$DB_USER" -d postgres -c "DROP DATABASE IF EXISTS $DB_NAME;" > /dev/null
docker exec telegramsellbot-postgres psql -U "$DB_USER" -d postgres -c "CREATE DATABASE $DB_NAME;" > /dev/null

echo "[3/3] Restoring database from $BACKUP_FILE..."
gunzip -c "$BACKUP_FILE" | docker exec -i telegramsellbot-postgres psql -U "$DB_USER" -d "$DB_NAME" > /dev/null

echo "============================================="
echo " Database restored successfully! ✅"
echo ""
echo " Note: Make sure your .env file matches the one from the backup"
echo " (especially the APP_SECRET_KEY, BOT_TOKEN, and DATABASE settings)."
echo " Restart your containers to apply changes:"
echo " docker compose -f docker-compose.prod.yml restart"
echo "============================================="
