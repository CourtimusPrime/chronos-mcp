"""SyncEngine: asyncio task orchestrator for all provider workers."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class SyncEngine:
    """Orchestrates provider workers; one asyncio Task per account."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path
        self._workers: dict[str, object] = {}  # account_id -> worker
        self._tasks: dict[str, asyncio.Task] = {}  # account_id -> Task
        self._running = False

    def _get_conn(self):
        from chronos.db.connection import get_connection
        return get_connection(self.db_path)

    def get_worker_state(self, account_id: str) -> str:
        """Return sync state for a given account_id."""
        worker = self._workers.get(account_id)
        if worker is None:
            return "idle"
        return worker.get_sync_state()

    async def _start_worker(self, account_row: dict) -> None:
        """Start a provider worker task for an account."""
        account_id = account_row["id"]
        provider = account_row["provider"]

        if account_id in self._tasks:
            # Already running
            return

        try:
            if provider == "gmail":
                from chronos.sync.gmail import GmailWorker
                worker = GmailWorker(dict(account_row), self.db_path)
            elif provider == "google_calendar":
                from chronos.sync.gcal import CalendarWorker
                worker = CalendarWorker(dict(account_row), self.db_path)
            else:
                logger.warning("Unknown provider: %s for account %s", provider, account_id)
                return

            self._workers[account_id] = worker
            task = asyncio.create_task(
                worker.run(),
                name=f"sync-{provider}-{account_id[:8]}",
            )
            self._tasks[account_id] = task
            logger.info("Started %s worker for account %s", provider, account_id)

            # Clean up task when it exits
            task.add_done_callback(lambda t: self._on_task_done(account_id, t))

        except Exception as e:
            logger.error("Failed to start worker for account %s: %s", account_id, e)

    def _on_task_done(self, account_id: str, task: asyncio.Task) -> None:
        """Handle worker task completion."""
        self._tasks.pop(account_id, None)
        if task.cancelled():
            logger.info("Worker task cancelled for account %s", account_id)
        elif task.exception():
            logger.error("Worker task failed for account %s: %s", account_id, task.exception())
        else:
            logger.info("Worker task completed for account %s", account_id)

    async def trigger_sync(self, account_id: str, sync_type: str = "incremental") -> bool:
        """
        Trigger an immediate out-of-cycle sync for an account.
        Returns True if triggered, False if account not found.
        """
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
        conn.close()

        if not row:
            return False

        worker = self._workers.get(account_id)
        if not worker:
            # Start worker if not running
            await self._start_worker(dict(row))
            worker = self._workers.get(account_id)

        if worker:
            # Run sync in a new task
            if sync_type == "full":
                asyncio.create_task(worker.full_sync())
            else:
                asyncio.create_task(worker.incremental_sync())

        return True

    async def run(self) -> None:
        """Main orchestrator loop: start workers for all sync-enabled accounts."""
        self._running = True

        # Initial worker startup
        conn = self._get_conn()
        accounts = conn.execute(
            "SELECT * FROM accounts WHERE sync_enabled = 1"
        ).fetchall()
        conn.close()

        for account in accounts:
            await self._start_worker(dict(account))

        # Poll for new accounts and manage existing workers
        from chronos.config import get as _cfg
        poll_interval = float(_cfg("sync.poll_interval_seconds", 30))
        while self._running:
            await asyncio.sleep(poll_interval)

            try:
                conn = self._get_conn()
                accounts = conn.execute(
                    "SELECT * FROM accounts WHERE sync_enabled = 1"
                ).fetchall()
                conn.close()

                active_ids = {a["id"] for a in accounts}

                # Start workers for new accounts
                for account in accounts:
                    account_id = account["id"]
                    if account_id not in self._tasks:
                        await self._start_worker(dict(account))

                # Cancel workers for removed/disabled accounts
                for account_id in list(self._tasks.keys()):
                    if account_id not in active_ids:
                        task = self._tasks.get(account_id)
                        if task and not task.done():
                            task.cancel()
                        worker = self._workers.pop(account_id, None)
                        if worker:
                            worker.stop()

            except Exception as e:
                logger.error("SyncEngine orchestrator error: %s", e)

    def stop(self) -> None:
        """Stop all workers."""
        self._running = False
        for account_id, task in list(self._tasks.items()):
            if not task.done():
                task.cancel()
            worker = self._workers.get(account_id)
            if worker:
                worker.stop()
