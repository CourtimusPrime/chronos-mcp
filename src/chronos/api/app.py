"""FastAPI app factory with envelope middleware."""
from __future__ import annotations

from typing import Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from chronos.api.routes import accounts, calendars, events, messages, query, sync


def create_app(db_path: Optional[str] = None, sync_engine=None) -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="Chronos HTTP API",
        description="Local-first email and calendar inbox for AI agents",
        version="0.1.0",
    )

    # Store state
    app.state.db_path = db_path
    app.state.sync_engine = sync_engine

    # CORS (localhost only, no credentials)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Register routers with v1 prefix
    app.include_router(accounts.router, prefix="/v1")
    app.include_router(messages.router, prefix="/v1")
    app.include_router(calendars.router, prefix="/v1")
    app.include_router(events.router, prefix="/v1")
    app.include_router(sync.router, prefix="/v1")
    app.include_router(query.router, prefix="/v1")

    @app.get("/")
    def root():
        return {"service": "chronos", "version": "0.1.0"}

    @app.get("/health")
    def health():
        return {"ok": True, "data": {"status": "healthy"}, "error": None}

    return app
