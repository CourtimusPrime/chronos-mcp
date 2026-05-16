"""Shared test fixtures."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from chronos.db.connection import get_connection
from chronos.db.migrations import apply_migrations


@pytest.fixture
def db_conn(tmp_path):
    """Create a temporary SQLite connection for testing."""
    db_path = str(tmp_path / "test.db")
    conn = get_connection(db_path)
    apply_migrations(conn)
    yield conn
    conn.close()


@pytest.fixture
def db_path(tmp_path):
    """Create a temporary DB path and apply migrations."""
    path = str(tmp_path / "test.db")
    conn = get_connection(path)
    apply_migrations(conn)
    conn.close()
    return path


@pytest.fixture
def api_client(db_path):
    """Create a FastAPI test client with an in-memory DB."""
    from chronos.api.app import create_app
    app = create_app(db_path=db_path)
    with TestClient(app) as client:
        yield client
