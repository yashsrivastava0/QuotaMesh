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
        self.accounting_marker = data_dir / "paid-accounting-incomplete"
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
            if version > 2:
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
            if version < 2:
                conn.executescript("""
                    BEGIN;
                    ALTER TABLE project_profiles ADD COLUMN allow_unknown_price INTEGER NOT NULL DEFAULT 0;
                    ALTER TABLE profile_targets ADD COLUMN input_price REAL;
                    ALTER TABLE profile_targets ADD COLUMN output_price REAL;
                    ALTER TABLE attempts ADD COLUMN paid INTEGER NOT NULL DEFAULT 0;
                    ALTER TABLE attempts ADD COLUMN skipped_json TEXT;
                    CREATE TABLE quota_state (
                      quota_group TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL,
                      until TEXT, strikes INTEGER NOT NULL DEFAULT 0, reason TEXT,
                      PRIMARY KEY(quota_group, model)
                    );
                    CREATE TABLE daily_usage (
                      day TEXT NOT NULL, profile_id INTEGER NOT NULL,
                      cost_usd REAL NOT NULL DEFAULT 0, unknown_cost INTEGER NOT NULL DEFAULT 0,
                      PRIMARY KEY(day, profile_id)
                    );
                    UPDATE project_profiles SET max_attempts=5;
                    UPDATE attempts SET paid=1 WHERE credential_id IN
                        (SELECT id FROM credentials WHERE plan_type IN ('PAID','UNKNOWN'));
                    INSERT INTO daily_usage(day,profile_id,cost_usd,unknown_cost)
                        SELECT substr(ts,1,10),profile_id,0,count(*) FROM attempts
                        WHERE paid=1 GROUP BY substr(ts,1,10),profile_id;
                    PRAGMA user_version=2;
                    COMMIT;
                """)
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
        quota_group: str | None = None,
        trial_expires_at: str | None = None,
        priority: int = 0,
        input_price: float | None = None,
        output_price: float | None = None,
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
                "UPDATE credentials SET quota_group=?,trial_expires_at=?,priority=? WHERE id=1",
                (quota_group, trial_expires_at, priority),
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
            conn.execute(
                "UPDATE profile_targets SET input_price=?,output_price=? "
                "WHERE profile_id=1 AND position=1",
                (input_price, output_price),
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
            "paid",
            "skipped_json",
        )
        with self.connection() as conn:
            conn.execute(
                f"INSERT INTO attempts ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                [values.get(column, 0 if column == "paid" else None) for column in columns],
            )

            if values.get("paid"):
                cost = values.get("provider_cost_usd")
                if cost is None:
                    cost = values.get("estimated_cost_usd")
                conn.execute(
                    """INSERT INTO daily_usage(day,profile_id,cost_usd,unknown_cost)
                    VALUES (?,?,?,?) ON CONFLICT(day,profile_id) DO UPDATE SET
                    cost_usd=cost_usd+excluded.cost_usd,
                    unknown_cost=unknown_cost+excluded.unknown_cost""",
                    (values["ts"][:10], values["profile_id"], cost or 0, int(cost is None)),
                )

    def recent_attempts(self, limit: int = 10) -> list[dict[str, Any]]:
        with self.connection() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    """SELECT ts, request_id, provider_id, model, outcome, http_status,
                          latency_ms, streamed, attempt_idx, error_class, skipped_json,
                          provider_cost_usd, estimated_cost_usd FROM attempts ORDER BY id DESC LIMIT ?""",
                    (limit,),
                )
            ]

    def routing_snapshot(self, now):
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM project_profiles WHERE id=1").fetchone()
            profile = dict(row) if row else None
            targets = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM profile_targets WHERE profile_id=1 ORDER BY position"
                )
            ]
            credentials = [
                dict(r)
                for r in conn.execute(
                    "SELECT c.*,s.secret_value,s.env_name FROM credentials c "
                    "JOIN secrets s ON c.id=s.credential_id"
                )
            ]
            states = {
                (r["quota_group"], r["model"]): dict(r)
                for r in conn.execute("SELECT * FROM quota_state")
            }
            day, month = now.date().isoformat(), now.strftime("%Y-%m")
            usage = {
                "daily": 0,
                "monthly": 0,
                "unknown": False,
                "unknown_daily": False,
                "unknown_monthly": False,
            }
            for r in conn.execute(
                "SELECT * FROM daily_usage WHERE profile_id=1 AND day LIKE ?", (month + "%",)
            ):
                usage["monthly"] += r["cost_usd"]
                usage["unknown"] |= bool(r["unknown_cost"])
                usage["unknown_monthly"] |= bool(r["unknown_cost"])
                if r["day"] == day:
                    usage["daily"] += r["cost_usd"]
                    usage["unknown_daily"] |= bool(r["unknown_cost"])
            return profile, targets, credentials, states, usage

    def safe_routing(self, now):
        profile, targets, credentials, states, usage = self.routing_snapshot(now)
        for c in credentials:
            c.pop("secret_value", None)
        return {
            "profile": profile,
            "targets": targets,
            "credentials": credentials,
            "quota_states": list(states.values()),
            "usage": usage,
        }

    def add_credential(self, **values):
        now = utc_now()
        secret = values.pop("secret_value")
        env = values.pop("env_name")
        values.pop("allow_paid", None)
        values.pop("model", None)
        values.pop("input_price", None)
        values.pop("output_price", None)
        values.update(
            created_at=now,
            updated_at=now,
            fingerprint=hashlib.sha256((secret or env).encode()).hexdigest()[:12],
        )
        with self.connection() as conn:
            cursor = conn.execute(
                f"INSERT INTO credentials ({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
                list(values.values()),
            )
            identifier = cursor.lastrowid
            conn.execute("INSERT INTO secrets VALUES (?,?,?)", (identifier, secret, env))
            return identifier

    def save_policy(self, policy, targets):
        with self.connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO project_profiles(id,name,slug,max_attempts) "
                "VALUES(1,'Default','default',5)"
            )
            conn.execute(
                "UPDATE project_profiles SET " + ",".join(k + "=?" for k in policy) + " WHERE id=1",
                list(policy.values()),
            )
            conn.execute("DELETE FROM profile_targets WHERE profile_id=1")
            for position, target in enumerate(targets, 1):
                conn.execute(
                    """INSERT INTO profile_targets
                    (profile_id,position,provider_id,model,credential_id,input_price,output_price)
                    VALUES(1,?,?,?,?,?,?)""",
                    (
                        position,
                        target["provider_id"],
                        target["model"],
                        target["credential_id"],
                        target["input_price"],
                        target["output_price"],
                    ),
                )

    def apply_outcome(self, decision, outcome, now):
        from quotamesh.engine.classify import next_quota_state

        if outcome.scope == "credential":
            with self.connection() as conn:
                conn.execute(
                    "UPDATE credentials SET status=?,status_reason=?,updated_at=? WHERE id=?",
                    (outcome.state, outcome.kind, now.isoformat(), decision["credential_id"]),
                )
        elif outcome.scope == "quota" or outcome.kind == "ok":
            with self.connection() as conn:
                key = (decision["quota_group"], decision["model"])
                row = conn.execute(
                    "SELECT * FROM quota_state WHERE quota_group=? AND model=?", key
                ).fetchone()
                if (
                    outcome.kind == "ok"
                    and row
                    and row["until"]
                    and datetime.fromisoformat(row["until"]) > now
                ):
                    # A success already in flight must not erase a more recent throttle.
                    return
                state = next_quota_state(dict(row) if row else {}, outcome, now)
                conn.execute(
                    """INSERT INTO quota_state VALUES(?,?,?,?,?,?)
                    ON CONFLICT(quota_group,model) DO UPDATE SET status=excluded.status,
                    until=excluded.until,strikes=excluded.strikes,reason=excluded.reason""",
                    (*key, state["status"], state["until"], state["strikes"], state["reason"]),
                )

    def reset_credential(self, identifier):
        from quotamesh.engine.select import quota_key

        with self.connection() as conn:
            row = conn.execute("SELECT * FROM credentials WHERE id=?", (identifier,)).fetchone()
            if not row:
                raise ValueError("Credential does not exist")
            conn.execute(
                "UPDATE credentials SET status='ACTIVE',status_reason=NULL WHERE id=?",
                (identifier,),
            )
            conn.execute("DELETE FROM quota_state WHERE quota_group=?", (quota_key(dict(row)),))
