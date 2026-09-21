#!/usr/bin/env python3
"""Migration script: add expires_at column to memory.db (Task 11).

The memory_keys table was created without an expires_at column in earlier
phases. This migration adds it to support TTL-based fact expiration, which
is used by the simulation system's expiry scenarios (spec § 5.1 B).

Usage:
    python scripts/migrate_memory_db.py [--db-path memory.db] [--dry-run]

Exit codes:
    0: Migration successful or already applied
    1: Error during migration
    2: Dry-run mode (no changes made)
"""

import argparse
import sqlite3
import sys
import logging
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)


def check_column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Check if a column exists in a table."""
    cursor = conn.cursor()
    cursor.execute(f"PRAGMA table_info({table})")
    columns = [row[1] for row in cursor.fetchall()]
    return column in columns


def migrate_memory_db(db_path: str, dry_run: bool = False) -> bool:
    """Add expires_at column to memory_keys table if it doesn't exist.

    Args:
        db_path: Path to memory.db
        dry_run: If True, check but don't modify the database

    Returns:
        True if migration succeeded or was already applied, False on error
    """
    db_file = Path(db_path)

    if not db_file.exists():
        logger.warning(f"Database file not found: {db_path}")
        logger.info("Skipping migration (will be created fresh on next engine start)")
        return True

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # Check if the column already exists
        if check_column_exists(conn, "memory_keys", "expires_at"):
            logger.info("Column 'expires_at' already exists in memory_keys table")
            conn.close()
            return True

        logger.info(f"Adding 'expires_at' column to memory_keys table in {db_path}")

        if dry_run:
            logger.info("(Dry-run mode: no changes made)")
            conn.close()
            return True

        # Add the column with NULL default (can be set when a fact is created with a TTL)
        cursor.execute("""
            ALTER TABLE memory_keys
            ADD COLUMN expires_at REAL DEFAULT NULL
        """)

        conn.commit()
        logger.info("Migration successful: expires_at column added")

        # Verify the column was added
        if check_column_exists(conn, "memory_keys", "expires_at"):
            logger.info("Verification: expires_at column confirmed in database")
            conn.close()
            return True
        else:
            logger.error("Verification failed: expires_at column not found after migration")
            conn.close()
            return False

    except sqlite3.Error as e:
        logger.error(f"Database error: {e}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Migrate memory.db: add expires_at column for TTL support"
    )
    parser.add_argument(
        "--db-path",
        default="memory.db",
        help="Path to memory.db (default: memory.db)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Check migration without making changes",
    )

    args = parser.parse_args()

    success = migrate_memory_db(args.db_path, dry_run=args.dry_run)

    if args.dry_run:
        sys.exit(2)  # Exit code 2 = dry-run (no changes)
    elif success:
        logger.info("Migration complete")
        sys.exit(0)
    else:
        logger.error("Migration failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
