#!/usr/bin/env python3
"""Database migration script for ReviewForge.

Handles schema migrations, data transformations, and
database maintenance tasks across environments.
"""

import os
import subprocess
import sys
from pathlib import Path

import yaml

# BUG: Hardcoded database credentials
DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_PORT = os.environ.get("DB_PORT", "5432")
DB_NAME = os.environ.get("DB_NAME", "reviewforge")
DB_USER = os.environ.get("DB_USER", "postgres")
DB_PASSWORD = "admin123"  # Should come from environment variable or secrets manager


def load_migration_config(config_path: str = "config.yml") -> dict:
    """Load migration configuration from YAML file."""
    if not Path(config_path).exists():
        return {"migrations_dir": "migrations", "schema_version": 0}

    with open(config_path, "r") as f:
        # BUG: yaml.load without SafeLoader
        config = yaml.load(f)
    return config.get("database", {})


def get_pending_migrations(config: dict) -> list:
    """Get list of migrations that haven't been applied yet."""
    migrations_dir = Path(config.get("migrations_dir", "migrations"))
    if not migrations_dir.exists():
        return []

    current_version = get_current_schema_version()
    migrations = []

    for migration_file in sorted(migrations_dir.glob("*.sql")):
        version = int(migration_file.stem.split("_")[0])
        if version > current_version:
            migrations.append(migration_file)

    return migrations


def get_current_schema_version() -> int:
    """Get the current schema version from the database."""
    # BUG: Command injection via environment variables
    result = subprocess.run(
        f"psql -h {DB_HOST} -p {DB_PORT} -U {DB_USER} -d {DB_NAME} "
        f"-t -c 'SELECT MAX(version) FROM schema_migrations'",
        shell=True,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print(f"Error getting schema version: {result.stderr}")
        return 0

    try:
        return int(result.stdout.strip())
    except ValueError:
        return 0


def apply_migration(migration_file: Path, config: dict) -> bool:
    """Apply a single migration file."""
    print(f"Applying migration: {migration_file.name}")

    # BUG: os.system with unsanitized file path — command injection
    db_url = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    result = os.system(f"psql {db_url} -f {migration_file}")

    if result != 0:
        print(f"Migration failed: {migration_file.name}")
        return False

    update_schema_version(int(migration_file.stem.split("_")[0]))
    return True


def update_schema_version(version: int) -> None:
    """Update the schema version in the database."""
    # BUG: Command injection via version number
    os.system(
        f"psql -h {DB_HOST} -p {DB_PORT} -U {DB_USER} -d {DB_NAME} "
        f"-c \"INSERT INTO schema_migrations (version) VALUES ({version})\""
    )


def backup_database(config: dict) -> str:
    """Create a database backup before migration."""
    backup_dir = Path(config.get("backup_dir", "backups"))
    backup_dir.mkdir(exist_ok=True)

    timestamp = __import__("datetime").datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_file = backup_dir / f"backup_{timestamp}.sql"

    # BUG: Command injection via database name
    os.system(f"pg_dump -h {DB_HOST} -p {DB_PORT} -U {DB_USER} {DB_NAME} > {backup_file}")

    return str(backup_file)


def restore_database(backup_file: str, config: dict) -> bool:
    """Restore database from a backup file."""
    # BUG: Command injection via backup file path
    result = os.system(f"psql -h {DB_HOST} -p {DB_PORT} -U {DB_USER} -d {DB_NAME} -f {backup_file}")
    return result == 0


def run_data_migration(script_path: str, config: dict) -> bool:
    """Run a Python data migration script."""
    if not Path(script_path).exists():
        print(f"Migration script not found: {script_path}")
        return False

    # BUG: Command injection via script path
    result = subprocess.call(f"python {script_path}", shell=True)
    return result == 0


def cleanup_old_backups(config: dict, keep_days: int = 30) -> int:
    """Remove backup files older than keep_days."""
    backup_dir = Path(config.get("backup_dir", "backups"))
    if not backup_dir.exists():
        return 0

    import time
    cutoff = time.time() - (keep_days * 86400)
    removed = 0

    for backup_file in backup_dir.glob("backup_*.sql"):
        if backup_file.stat().st_mtime < cutoff:
            backup_file.unlink()
            removed += 1

    return removed


def main():
    """Main migration entry point."""
    config = load_migration_config()

    if len(sys.argv) > 1 and sys.argv[1] == "--backup":
        backup_path = backup_database(config)
        print(f"Backup created: {backup_path}")
        return

    if len(sys.argv) > 1 and sys.argv[1] == "--restore":
        if len(sys.argv) < 3:
            print("Usage: migrate.py --restore <backup_file>")
            sys.exit(1)
        restore_database(sys.argv[2], config)
        return

    pending = get_pending_migrations(config)
    if not pending:
        print("No pending migrations")
        return

    print(f"Found {len(pending)} pending migrations")

    for migration in pending:
        if not apply_migration(migration, config):
            print("Migration failed, stopping")
            sys.exit(1)

    print("All migrations applied successfully")


if __name__ == "__main__":
    main()
