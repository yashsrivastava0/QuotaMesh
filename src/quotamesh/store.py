"""Small SQLite repository. Every operation owns a short transaction."""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            self.data_dir.chmod(0o700)
        self.path = data_dir / "quotamesh.db"
        self._migrate()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _migrate(self) -> None:
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > 1:
                raise RuntimeError(f"Database schema {version} is newer than this QuotaMesh build")
            if version == 0:
                conn.executescript(
                    """
                    BEGIN;
                    CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE credentials (
                      id INTEGER PRIMARY KEY, provider_id TEXT NOT NULL, label TEXT NOT NULL,
                      plan_type TEXT NOT NULL, account_label TEXT, quota_group TEXT,
                      priority INTEGER NOT NULL DEFAULT 0, starting_credit_usd REAL,
                      trial_expires_at TEXT, base_url TEXT NOT NULL, fingerprint TEXT NOT NULL,
                      status TEXT NOT NULL DEFAULT 'ACTIVE', status_reason TEXT,
                      enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
                      updated_at TEXT NOT NULL
                    );
                    CREATE TABLE secrets (
                      credential_id INTEGER PRIMARY KEY REFERENCES credentials(id) ON DELETE CASCADE,
                      secret_value TEXT, env_name TEXT,
                      CHECK ((secret_value IS NOT NULL) != (env_name IS NOT NULL))
                    );
                    CREATE TABLE project_profiles (
                      id INTEGER PRIMARY KEY, name TEXT NOT NULL, slug TEXT NOT NULL UNIQUE,
                      allow_paid INTEGER NOT NULL DEFAULT 0, allow_trial INTEGER NOT NULL DEFAULT 1,
                      max_attempts INTEGER NOT NULL DEFAULT 1,
                      nonstream_deadline_s INTEGER NOT NULL DEFAULT 45,
                      first_event_timeout_s INTEGER NOT NULL DEFAULT 30,
                      paid_daily_cap_usd REAL, paid_monthly_cap_usd REAL,
                      enabled INTEGER NOT NULL DEFAULT 1
                    );
                    CREATE TABLE profile_targets (
                      profile_id INTEGER NOT NULL REFERENCES project_profiles(id) ON DELETE CASCADE,
                      position INTEGER NOT NULL, provider_id TEXT NOT NULL, model TEXT NOT NULL,
                      credential_id INTEGER REFERENCES credentials(id),
                      PRIMARY KEY (profile_id, position)
                    );
                    CREATE TABLE attempts (
                      id INTEGER PRIMARY KEY, ts TEXT NOT NULL, request_id TEXT NOT NULL,
                      profile_id INTEGER NOT NULL, client_label TEXT,
                      attempt_idx INTEGER NOT NULL, provider_id TEXT NOT NULL, model TEXT NOT NULL,
                      credential_id INTEGER, outcome TEXT NOT NULL, http_status INTEGER,
                      error_class TEXT, latency_ms INTEGER, streamed INTEGER NOT NULL,
                      input_tokens INTEGER, output_tokens INTEGER,
                      provider_cost_usd REAL, estimated_cost_usd REAL
                    );
                    CREATE INDEX attempts_recent ON attempts(ts DESC);
                    PRAGMA user_version=1;
                    COMMIT;
                    """
                )
        if os.name != "nt":
            self.path.chmod(0o600)

    def gateway_key(self) -> str:
        path = self.data_dir / "gateway-key"
        if not path.exists():
            key = secrets.token_urlsafe(32)
            try:
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    stream.write(key + "\n")
        return path.read_text(encoding="utf-8").strip()

    def save_connection(
        self,
        *,
        provider_id: str,
        label: str,
        plan_type: str,
        model: str,
        base_url: str,
        secret_value: str | None,
        env_name: str | None,
        allow_paid: bool,
    ) -> None:
        now = utc_now()
        fingerprint = hashlib.sha256((secret_value or env_name or "").encode()).hexdigest()[:12]
        with self.connection() as conn:
            conn.execute("DELETE FROM profile_targets WHERE profile_id=1")
            conn.execute("DELETE FROM secrets WHERE credential_id=1")
            conn.execute("DELETE FROM credentials WHERE id=1")
            conn.execute(
                """INSERT INTO credentials
                   (id, provider_id, label, plan_type, base_url, fingerprint, created_at, updated_at)
                   VALUES (1, ?, ?, ?, ?, ?, ?, ?)""",
                (provider_id, label, plan_type, base_url, fingerprint, now, now),
            )
            conn.execute(
                "INSERT INTO secrets (credential_id, secret_value, env_name) VALUES (1, ?, ?)",
                (secret_value, env_name),
            )
            conn.execute(
                """INSERT INTO project_profiles (id, name, slug, allow_paid) VALUES (1,'Default','default',?)
                   ON CONFLICT(id) DO UPDATE SET allow_paid=excluded.allow_paid""",
                (int(allow_paid),),
            )
            conn.execute(
                """INSERT INTO profile_targets
                   (profile_id, position, provider_id, model, credential_id)
                   VALUES (1, 1, ?, ?, 1)""",
                (provider_id, model),
            )

    def connection_summary(self) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                """SELECT c.id, c.provider_id, c.label, c.plan_type, c.base_url,
                          c.status, c.enabled, c.updated_at, s.env_name,
                          p.slug, p.allow_paid, t.model
                   FROM credentials c JOIN secrets s ON s.credential_id=c.id
                   JOIN profile_targets t ON t.credential_id=c.id
                   JOIN project_profiles p ON p.id=t.profile_id WHERE c.id=1"""
            ).fetchone()
            return dict(row) if row else None

    def runtime_connection(self) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                """SELECT c.*, s.secret_value, s.env_name, p.allow_paid, p.enabled AS profile_enabled,
                          t.model, p.id AS profile_id
                   FROM credentials c JOIN secrets s ON s.credential_id=c.id
                   JOIN profile_targets t ON t.credential_id=c.id
                   JOIN project_profiles p ON p.id=t.profile_id
                   WHERE c.id=1 AND p.slug='default'"""
            ).fetchone()
            return dict(row) if row else None

    def record_attempt(self, **values: Any) -> None:
        columns = (
            "ts",
            "request_id",
            "profile_id",
            "client_label",
            "attempt_idx",
            "provider_id",
            "model",
            "credential_id",
            "outcome",
            "http_status",
            "error_class",
            "latency_ms",
            "streamed",
            "input_tokens",
            "output_tokens",
            "provider_cost_usd",
            "estimated_cost_usd",
        )
        with self.connection() as conn:
            conn.execute(
                f"INSERT INTO attempts ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                [values.get(column) for column in columns],
            )

    def recent_attempts(self, limit: int = 10) -> list[dict[str, Any]]:
        with self.connection() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    """SELECT ts, request_id, provider_id, model, outcome, http_status,
                          latency_ms, streamed FROM attempts ORDER BY id DESC LIMIT ?""",
                    (limit,),
                )
            ]
