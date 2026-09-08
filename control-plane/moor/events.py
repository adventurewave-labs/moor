"""Event store (SQLAlchemy) and Slack-compatible webhook alerting.

Every drift, decision, action, alert and mode change lands in the
events table — the product's audit trail and forensic record. Alert
payloads use the Slack incoming-webhook card format, which every
major chat and incident platform ingests.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Callable

import httpx
from sqlalchemy import (JSON, Boolean, Column, Float, Integer, String,
                        Text, create_engine, desc, select)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


class EventRow(Base):
    __tablename__ = "events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ts = Column(Float, nullable=False, default=lambda: time.time())
    type = Column(String(64), nullable=False, index=True)
    service = Column(String(128), nullable=True)
    severity = Column(String(16), nullable=False, default="info")
    payload = Column(JSON, nullable=False, default=dict)


class SnapshotRow(Base):
    __tablename__ = "snapshots"

    id = Column(Integer, primary_key=True)  # singleton row id=1
    desired_hash = Column(String(64), nullable=True)
    actual_hash = Column(String(64), nullable=True)
    report_hash = Column(String(64), nullable=True)
    drift = Column(Boolean, nullable=False, default=False)
    updated = Column(Float, nullable=False, default=lambda: time.time())


class KVRow(Base):
    __tablename__ = "kv"

    key = Column(String(64), primary_key=True)
    value = Column(Text, nullable=False)


class BackoffRow(Base):
    __tablename__ = "backoffs"

    service = Column(String(128), primary_key=True)
    until = Column(Float, nullable=False, default=0.0)
    attempts = Column(Integer, nullable=False, default=0)


class EventStore:
    """Durable event log + snapshot/kv state, safe for concurrent access."""

    def __init__(self, db_url: str, echo: bool = False):
        self._engine = create_engine(db_url, echo=echo, future=True)
        self._Session = sessionmaker(bind=self._engine, future=True)
        self._lock = threading.Lock()
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        attempts = 0
        while True:
            try:
                Base.metadata.create_all(self._engine)
                return
            except Exception:
                attempts += 1
                if attempts > 15:
                    raise
                time.sleep(1.0)  # wait for the database container

    def _session(self) -> Session:
        return self._Session()

    # ------------------------------------------------------------ events

    def append(self, type_: str, service: str | None, severity: str, payload: dict) -> dict:
        with self._lock:
            with self._session() as session:
                row = EventRow(type=type_, service=service,
                               severity=severity, payload=payload)
                session.add(row)
                session.commit()
                out = {"id": row.id, "ts": row.ts, "type": row.type,
                       "service": row.service, "severity": row.severity,
                       "payload": row.payload}
        return out

    def list(self, limit: int = 100, since_id: int = 0) -> list[dict]:
        with self._session() as session:
            stmt = (
                select(EventRow)
                .where(EventRow.id > since_id)
                .order_by(desc(EventRow.id))
                .limit(limit)
            )
            rows = session.scalars(stmt).all()
            return [
                {"id": r.id, "ts": r.ts, "type": r.type, "service": r.service,
                 "severity": r.severity, "payload": r.payload}
                for r in rows
            ][::-1]

    def last_id(self) -> int:
        with self._session() as session:
            row = session.scalars(
                select(EventRow).order_by(desc(EventRow.id)).limit(1)
            ).first()
            return row.id if row else 0

    # --------------------------------------------------------- snapshots

    def get_snapshot(self) -> dict | None:
        with self._session() as session:
            row = session.get(SnapshotRow, 1)
            if row is None:
                return None
            return {
                "desired_hash": row.desired_hash,
                "actual_hash": row.actual_hash,
                "report_hash": row.report_hash,
                "drift": row.drift,
                "updated": row.updated,
            }

    def save_snapshot(self, desired_hash, actual_hash, report_hash, drift: bool) -> None:
        with self._lock:
            with self._session() as session:
                row = session.get(SnapshotRow, 1)
                if row is None:
                    row = SnapshotRow(id=1)
                    session.add(row)
                row.desired_hash = desired_hash
                row.actual_hash = actual_hash
                row.report_hash = report_hash
                row.drift = drift
                row.updated = time.time()
                session.commit()

    # ------------------------------------------------------------ kv/mode

    def get_mode(self) -> str:
        with self._session() as session:
            row = session.get(KVRow, "mode")
            return row.value if row else "advise"

    def set_mode(self, mode: str) -> None:
        with self._lock:
            with self._session() as session:
                row = session.get(KVRow, "mode")
                if row is None:
                    row = KVRow(key="mode")
                    session.add(row)
                row.value = mode
                session.commit()

    # ------------------------------------------------------------ backoff

    def backoff_until(self, service: str) -> float:
        with self._session() as session:
            row = session.get(BackoffRow, service)
            return row.until if row else 0.0

    def in_backoff(self, service: str) -> bool:
        return self.backoff_until(service) > time.time()

    def set_backoff(self, service: str, until: float, attempts: int) -> None:
        with self._lock:
            with self._session() as session:
                row = session.get(BackoffRow, service)
                if row is None:
                    row = BackoffRow(service=service)
                    session.add(row)
                row.until = until
                row.attempts = attempts
                session.commit()

    def clear_backoff(self, service: str) -> None:
        with self._lock:
            with self._session() as session:
                row = session.get(BackoffRow, service)
                if row:
                    session.delete(row)
                    session.commit()


# ----------------------------------------------------------------- alerts

SEVERITY_COLORS = {
    "critical": "#e01b24",
    "warning": "#e0a34e",
    "info": "#4da8da",
    "ok": "#3fb68b",
    "error": "#e01b24",
}


class WebhookAlerter:
    """Sends Slack incoming-webhook formatted cards to one URL."""

    def __init__(self, url: str | None, transport: httpx.BaseTransport | None = None):
        self.url = url
        self._client = httpx.Client(timeout=5.0, transport=transport)

    def send(
        self,
        title: str,
        text: str,
        color: str = "critical",
        fields: list[tuple[str, str]] | None = None,
        footer: str = "Moor control plane",
    ) -> tuple[bool, str | None]:
        if not self.url:
            return False, "no webhook configured"
        payload = {
            "username": "Moor",
            "icon_emoji": ":anchor:",
            "text": title,
            "attachments": [
                {
                    "color": SEVERITY_COLORS.get(color, "#4da8da"),
                    "title": title,
                    "text": text,
                    "footer": footer,
                    "ts": int(time.time()),
                    "fields": [
                        {"title": k, "value": v, "short": len(str(v)) < 40}
                        for k, v in (fields or [])
                    ],
                }
            ],
        }
        try:
            response = self._client.post(self.url, json=payload)
            response.raise_for_status()
            return True, None
        except Exception as exc:  # noqa: BLE001 — alerting must never crash the loop
            return False, str(exc)


def payload_json(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True)
