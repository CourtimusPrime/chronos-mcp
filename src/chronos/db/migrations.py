"""Apply schema migrations on database open."""
from __future__ import annotations

import logging
import sqlite3

from .schema import ALL_DDL

logger = logging.getLogger(__name__)


def apply_migrations(conn: sqlite3.Connection) -> None:
    """Apply all DDL statements to initialize or upgrade the database schema."""
    try:
        for ddl in ALL_DDL:
            ddl = ddl.strip()
            if ddl:
                conn.execute(ddl)
        conn.commit()
        logger.debug("Schema migrations applied successfully.")
    except sqlite3.Error as e:
        logger.error("Failed to apply migrations: %s", e)
        raise
