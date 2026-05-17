"""Data models for Chronos."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Account:
    id: str
    provider: str  # 'gmail' | 'google_calendar'
    email: str
    display_name: Optional[str]
    auth_type: str  # 'oauth2'
    auth_data: str  # JSON
    sync_enabled: int  # 0 | 1
    last_synced_at: Optional[int]  # unix ms
    sync_cursor: Optional[str]
    created_at: int  # unix ms


@dataclass
class Thread:
    id: str
    account_id: str
    provider_thread_id: str
    subject: Optional[str]
    participant_addresses: Optional[str]  # JSON list
    message_count: int
    last_message_at: Optional[int]
    provider_raw: Optional[str]
    created_at: int


@dataclass
class Message:
    id: str
    account_id: str
    thread_id: Optional[str]
    provider_message_id: str
    subject: Optional[str]
    from_address: str
    from_name: Optional[str]
    to_addresses: str  # JSON list
    cc_addresses: Optional[str]  # JSON list
    bcc_addresses: Optional[str]  # JSON list
    date_unix: int
    body_text: Optional[str]
    body_html: Optional[str]
    labels: Optional[str]  # JSON list
    has_attachments: int  # 0 | 1
    attachment_names: Optional[str]  # JSON list
    in_reply_to: Optional[str]
    references_header: Optional[str]
    sync_state: str  # 'synced' | 'pending' | 'conflict'
    created_at: int
    provider_raw: Optional[str]


@dataclass
class Calendar:
    id: str
    account_id: str
    provider_calendar_id: str
    name: str
    description: Optional[str]
    color: Optional[str]
    is_primary: int  # 0 | 1
    is_read_only: int  # 0 | 1
    timezone: Optional[str]
    provider_raw: Optional[str]
    created_at: int


@dataclass
class Event:
    id: str
    account_id: str
    calendar_id: str
    provider_event_id: str
    title: str
    description: Optional[str]
    location: Optional[str]
    start_unix: int
    end_unix: int
    is_all_day: int  # 0 | 1
    timezone: Optional[str]
    status: str  # 'confirmed' | 'tentative' | 'cancelled'
    organizer_address: Optional[str]
    attendees: Optional[str]  # JSON
    rrule: Optional[str]
    is_recurring_master: int  # 0 | 1
    recurrence_master_id: Optional[str]
    recurrence_instance_date: Optional[int]
    conference_url: Optional[str]
    source_message_id: Optional[str]
    sync_state: str  # 'synced' | 'pending' | 'conflict'
    created_at: int
    provider_raw: Optional[str]


@dataclass
class PendingChange:
    id: str
    resource_type: str  # 'event'
    resource_id: str
    account_id: str
    operation: str  # 'create' | 'update' | 'delete'
    payload: str  # JSON
    status: str  # 'pending' | 'submitted' | 'confirmed' | 'rejected'
    created_at: int
    submitted_at: Optional[int]
    confirmed_at: Optional[int]
    rejection_reason: Optional[str]


@dataclass
class SyncLog:
    id: str
    account_id: str
    sync_type: str  # 'full' | 'incremental'
    started_at: int
    completed_at: Optional[int]
    records_synced: int
    status: str  # 'running' | 'completed' | 'failed'
    error_message: Optional[str]
