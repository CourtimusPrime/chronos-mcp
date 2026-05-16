"""DB dependency injection for FastAPI."""
from __future__ import annotations

import sqlite3
from typing import Generator

from fastapi import Request


def get_db(request: Request) -> Generator[sqlite3.Connection, None, None]:
    """FastAPI dependency that provides a DB connection per request."""
    db_path = request.app.state.db_path
    from chronos.db.connection import get_connection
    conn = get_connection(db_path)
    try:
        yield conn
    finally:
        conn.close()


def get_sync_engine(request: Request):
    """FastAPI dependency that provides the SyncEngine."""
    return getattr(request.app.state, "sync_engine", None)
