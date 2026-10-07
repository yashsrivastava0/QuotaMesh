"""Small SQLite repository. Every operation owns a short transaction."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
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
            if version > 4:
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
            if version < 3:
                conn.executescript("""
                    BEGIN;
                    ALTER TABLE credentials ADD COLUMN deleted_at TEXT;
                    ALTER TABLE project_profiles ADD COLUMN deleted_at TEXT;
                    ALTER TABLE attempts ADD COLUMN plan_type TEXT;
                    ALTER TABLE attempts ADD COLUMN credential_label TEXT;
                    ALTER TABLE attempts ADD COLUMN profile_slug TEXT;
                    CREATE TABLE usage_rollups (
                      day TEXT NOT NULL, profile_id INTEGER NOT NULL, credential_id INTEGER NOT NULL,
                      provider_id TEXT NOT NULL, model TEXT NOT NULL, plan_type TEXT NOT NULL,
                      attempts INTEGER NOT NULL, routed_requests INTEGER NOT NULL, failures INTEGER NOT NULL,
                      input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
                      input_missing INTEGER NOT NULL, output_missing INTEGER NOT NULL,
                      provider_cost_usd REAL NOT NULL, estimated_cost_usd REAL NOT NULL,
                      provider_cost_count INTEGER NOT NULL, estimated_cost_count INTEGER NOT NULL,
                      unknown_cost INTEGER NOT NULL, last_success_at TEXT, last_error_at TEXT,
                      last_error_class TEXT, last_seen_at TEXT NOT NULL, last_latency_ms INTEGER,
                      PRIMARY KEY(day,profile_id,credential_id,provider_id,model,plan_type)
                    );
                    CREATE INDEX attempts_request ON attempts(request_id,id);
                    CREATE INDEX usage_credential ON usage_rollups(credential_id,day);
                    CREATE INDEX usage_profile ON usage_rollups(profile_id,day);
                    UPDATE attempts SET
                      plan_type=COALESCE((SELECT plan_type FROM credentials WHERE id=credential_id),'UNKNOWN'),
                      credential_label=COALESCE((SELECT label FROM credentials WHERE id=credential_id),'Removed credential'),
                      profile_slug=COALESCE((SELECT slug FROM project_profiles WHERE id=profile_id),'Removed profile');
                    UPDATE attempts SET estimated_cost_usd=0,provider_cost_usd=NULL
                      WHERE outcome!='OK' AND http_status>=400 AND provider_cost_usd=0;
                    INSERT OR REPLACE INTO settings(key,value) VALUES('usage_coverage','complete gateway observations since installation');
                """)
                if version:
                    conn.execute(
                        "UPDATE settings SET value=? WHERE key='usage_coverage'",
                        (
                            "retained history before schema v3; complete gateway observations afterwards",
                        ),
                    )
                from quotamesh.usage import record_rollup

                for row in conn.execute("SELECT * FROM attempts ORDER BY id"):
                    record_rollup(conn, dict(row))
                conn.execute("PRAGMA user_version=3")
            if version < 4:
                if not conn.in_transaction:
                    conn.execute("BEGIN")
                conn.execute("""CREATE TABLE credential_checks (
                    credential_id INTEGER NOT NULL REFERENCES credentials(id),
                    revision TEXT NOT NULL, base_url TEXT NOT NULL, checked_at TEXT NOT NULL,
                    mode TEXT NOT NULL, outcome TEXT NOT NULL, http_status INTEGER,
                    latency_ms INTEGER, models_json TEXT NOT NULL DEFAULT '[]',
                    request_id TEXT, PRIMARY KEY(credential_id,mode)
                )""")
                conn.execute("PRAGMA user_version=4")
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
            if conn.execute(
                "SELECT 1 FROM profile_targets WHERE credential_id=1 AND profile_id!=1"
            ).fetchone():
                raise ValueError(
                    "Credential is pinned by another profile; use credential editing instead"
                )
            conn.execute("DELETE FROM profile_targets WHERE profile_id=1")
            conn.execute("DELETE FROM secrets WHERE credential_id=1")
            if conn.execute(
                "SELECT 1 FROM credentials WHERE id=1 AND deleted_at IS NOT NULL"
            ).fetchone():
                raise ValueError(
                    "Default credential is archived; add a new credential and edit the profile"
                )
            conn.execute(
                """INSERT INTO credentials
                   (id, provider_id, label, plan_type, base_url, fingerprint, created_at, updated_at)
                   VALUES (1, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET provider_id=excluded.provider_id,
                     label=excluded.label,plan_type=excluded.plan_type,base_url=excluded.base_url,
                     fingerprint=excluded.fingerprint,status='ACTIVE',status_reason=NULL,
                     enabled=1,updated_at=excluded.updated_at""",
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
                   JOIN project_profiles p ON p.id=t.profile_id WHERE c.id=1 AND c.deleted_at IS NULL"""
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
                   WHERE c.id=1 AND c.deleted_at IS NULL AND p.slug='default'"""
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
            "plan_type",
            "credential_label",
            "profile_slug",
        )
        with self.connection() as conn:
            # Legacy callers can omit snapshots; gateway callers supply immutable attempt metadata.
            if not values.get("plan_type"):
                row = conn.execute(
                    "SELECT plan_type,label FROM credentials WHERE id=?",
                    (values.get("credential_id"),),
                ).fetchone()
                values["plan_type"] = row["plan_type"] if row else "UNKNOWN"
                values["credential_label"] = row["label"] if row else "Removed credential"
            if not values.get("profile_slug"):
                row = conn.execute(
                    "SELECT slug FROM project_profiles WHERE id=?", (values["profile_id"],)
                ).fetchone()
                values["profile_slug"] = row["slug"] if row else "Removed profile"
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

            from quotamesh.usage import record_rollup

            record_rollup(conn, values)
            conn.execute(
                "DELETE FROM attempts WHERE ts<?",
                ((datetime.fromisoformat(values["ts"]) - timedelta(days=30)).isoformat(),),
            )
            conn.execute(
                "DELETE FROM attempts WHERE request_id IN "
                "(SELECT request_id FROM attempts ORDER BY id DESC LIMIT -1 OFFSET 50000)"
            )

    def recent_attempts(self, limit: int = 10) -> list[dict[str, Any]]:
        with self.connection() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    """SELECT ts, request_id, provider_id, model, outcome, http_status,
                          latency_ms, streamed, attempt_idx, error_class, skipped_json,
                          provider_cost_usd, estimated_cost_usd, profile_slug, credential_label,
                          plan_type FROM attempts ORDER BY id DESC LIMIT ?""",
                    (limit,),
                )
            ]

    def routing_snapshot(self, now, slug="default"):
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM project_profiles WHERE slug=? AND deleted_at IS NULL", (slug,)
            ).fetchone()
            profile = dict(row) if row else None
            targets = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM profile_targets WHERE profile_id=? ORDER BY position",
                    (profile["id"] if profile else -1,),
                )
            ]
            credentials = [
                dict(r)
                for r in conn.execute(
                    "SELECT c.*,s.secret_value,s.env_name FROM credentials c "
                    "JOIN secrets s ON c.id=s.credential_id WHERE c.deleted_at IS NULL"
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
                "SELECT * FROM daily_usage WHERE profile_id=? AND day LIKE ?",
                (profile["id"] if profile else -1, month + "%"),
            ):
                usage["monthly"] += r["cost_usd"]
                usage["unknown"] |= bool(r["unknown_cost"])
                usage["unknown_monthly"] |= bool(r["unknown_cost"])
                if r["day"] == day:
                    usage["daily"] += r["cost_usd"]
                    usage["unknown_daily"] |= bool(r["unknown_cost"])
            return profile, targets, credentials, states, usage

    def safe_routing(self, now, slug="default"):
        profile, targets, credentials, states, usage = self.routing_snapshot(now, slug)
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

    def save_policy(self, policy, targets, slug="default"):
        self.save_profile(slug, policy, targets)

    def apply_outcome(self, decision, outcome, now):
        from quotamesh.engine.classify import next_quota_state

        if outcome.scope == "credential":
            with self.connection() as conn:
                conn.execute(
                    "UPDATE credentials SET status=?,status_reason=?,updated_at=? WHERE id=? "
                    "AND (fingerprint=? OR ? IS NULL)",
                    (
                        outcome.state,
                        outcome.kind,
                        now.isoformat(),
                        decision["credential_id"],
                        decision.get("credential_revision"),
                        decision.get("credential_revision"),
                    ),
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
            row = conn.execute(
                "SELECT * FROM credentials WHERE id=? AND deleted_at IS NULL", (identifier,)
            ).fetchone()
            if not row:
                raise ValueError("Credential does not exist")
            conn.execute(
                "UPDATE credentials SET status='ACTIVE',status_reason=NULL WHERE id=?",
                (identifier,),
            )
            conn.execute("DELETE FROM quota_state WHERE quota_group=?", (quota_key(dict(row)),))

    def profiles(self, include_archived=False):
        with self.connection() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM project_profiles"
                    + ("" if include_archived else " WHERE deleted_at IS NULL")
                    + " ORDER BY id"
                )
            ]

    def save_profile(self, slug, policy, targets, *, name=None, create=False):
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM project_profiles WHERE slug=?", (slug,)).fetchone()
            if create and row:
                raise ValueError("Profile slug is already used, including archived profiles")
            if row and row["deleted_at"]:
                raise ValueError("Profile is archived")
            if not row:
                if not create and slug != "default":
                    raise LookupError("Profile not found")
                # Preserve the legacy default ID even when the first profile created is named.
                conn.execute(
                    "INSERT OR IGNORE INTO project_profiles(id,name,slug,max_attempts) VALUES(1,'Default','default',5)"
                )
                if slug != "default":
                    conn.execute(
                        "INSERT INTO project_profiles(name,slug,max_attempts) VALUES(?,?,5)",
                        (name or slug, slug),
                    )
                row = conn.execute(
                    "SELECT * FROM project_profiles WHERE slug=?", (slug,)
                ).fetchone()
            identifier = row["id"]
            if name is not None:
                policy = policy | {"name": name}
            conn.execute(
                "UPDATE project_profiles SET " + ",".join(k + "=?" for k in policy) + " WHERE id=?",
                [*policy.values(), identifier],
            )
            conn.execute("DELETE FROM profile_targets WHERE profile_id=?", (identifier,))
            for position, target in enumerate(targets, 1):
                if target["credential_id"] is not None:
                    credential = conn.execute(
                        "SELECT provider_id FROM credentials WHERE id=? AND deleted_at IS NULL",
                        (target["credential_id"],),
                    ).fetchone()
                    if not credential or credential["provider_id"] != target["provider_id"]:
                        raise ValueError("Pinned credential must match provider")
                conn.execute(
                    """INSERT INTO profile_targets
                    (profile_id,position,provider_id,model,credential_id,input_price,output_price)
                    VALUES(?,?,?,?,?,?,?)""",
                    (
                        identifier,
                        position,
                        target["provider_id"],
                        target["model"],
                        target["credential_id"],
                        target["input_price"],
                        target["output_price"],
                    ),
                )
            return identifier

    def delete_profile(self, slug):
        if slug == "default":
            raise ValueError("Disable default instead of deleting it")
        with self.connection() as conn:
            cursor = conn.execute(
                "UPDATE project_profiles SET enabled=0,deleted_at=? "
                "WHERE slug=? AND deleted_at IS NULL",
                (utc_now(), slug),
            )
            if not cursor.rowcount:
                raise LookupError("Profile not found")
            conn.execute(
                "DELETE FROM profile_targets WHERE profile_id=(SELECT id FROM project_profiles WHERE slug=?)",
                (slug,),
            )

    def edit_credential(self, identifier, values):
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM credentials WHERE id=? AND deleted_at IS NULL", (identifier,)
            ).fetchone()
            if not row:
                raise LookupError("Credential not found")
            values = dict(values)
            secret = values.pop("secret_value", None)
            env = values.pop("env_name", None)
            for field in ("model", "allow_paid", "input_price", "output_price"):
                values.pop(field, None)
            if (
                values.get("provider_id", row["provider_id"]) != row["provider_id"]
                and conn.execute(
                    "SELECT 1 FROM profile_targets WHERE credential_id=?", (identifier,)
                ).fetchone()
            ):
                raise ValueError("Change pinned profile targets before changing this provider")
            if secret or env:
                conn.execute(
                    "UPDATE secrets SET secret_value=?,env_name=? WHERE credential_id=?",
                    (secret, env, identifier),
                )
                values.update(
                    fingerprint=hashlib.sha256((secret or env).encode()).hexdigest()[:12],
                    status="ACTIVE",
                    status_reason=None,
                )
            values["updated_at"] = utc_now()
            if (
                secret
                or env
                or any(
                    values.get(field, row[field]) != row[field]
                    for field in ("base_url", "provider_id")
                )
            ):
                conn.execute("DELETE FROM credential_checks WHERE credential_id=?", (identifier,))
            conn.execute(
                "UPDATE credentials SET " + ",".join(k + "=?" for k in values) + " WHERE id=?",
                [*values.values(), identifier],
            )

    def delete_credential(self, identifier):
        with self.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM credentials WHERE id=? AND deleted_at IS NULL", (identifier,)
            ).fetchone()
            if not row:
                raise LookupError("Credential not found")
            if conn.execute(
                "SELECT 1 FROM profile_targets WHERE credential_id=?", (identifier,)
            ).fetchone():
                raise ValueError("Remove pinned profile targets before deleting this credential")
            conn.execute(
                "UPDATE credentials SET deleted_at=?,enabled=0,status='DISABLED' WHERE id=?",
                (utc_now(), identifier),
            )
            conn.execute("DELETE FROM secrets WHERE credential_id=?", (identifier,))
            conn.execute("DELETE FROM credential_checks WHERE credential_id=?", (identifier,))

    def save_check(self, credential, result):
        """A late probe cannot overwrite a rotated/deleted credential's diagnostics."""
        with self.connection() as conn:
            row = conn.execute(
                "SELECT fingerprint,base_url FROM credentials WHERE id=? AND deleted_at IS NULL",
                (credential["id"],),
            ).fetchone()
            if (
                not row
                or row["fingerprint"] != credential["fingerprint"]
                or (row["base_url"] != credential["base_url"])
            ):
                return False
            conn.execute(
                """INSERT OR REPLACE INTO credential_checks VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    credential["id"],
                    credential["fingerprint"],
                    credential["base_url"],
                    result["checked_at"],
                    result["mode"],
                    result["outcome"],
                    result.get("http_status"),
                    result.get("latency_ms"),
                    json.dumps(result.get("models", [])),
                    result.get("request_id"),
                ),
            )
            return True

    def checks(self, now):
        with self.connection() as conn:
            rows = conn.execute("""SELECT d.* FROM credential_checks d JOIN credentials c
                ON c.id=d.credential_id WHERE c.deleted_at IS NULL
                AND c.fingerprint=d.revision AND c.base_url=d.base_url""").fetchall()
        results = []
        for row in rows:
            result = dict(row)
            result.pop("revision")
            result.pop("base_url")
            result["models"] = json.loads(result.pop("models_json"))
            result["stale"] = (
                now - datetime.fromisoformat(result["checked_at"])
            ).total_seconds() > 86400
            results.append(result)
        return results

    def prune_history(self, now, max_rows=50000, days=30):
        with self.connection() as conn:
            conn.execute(
                "DELETE FROM attempts WHERE ts<?", ((now - timedelta(days=days)).isoformat(),)
            )
            # Keep complete request traces when the row limit cuts through a multi-attempt request.
            conn.execute(
                "DELETE FROM attempts WHERE request_id IN "
                "(SELECT request_id FROM attempts ORDER BY id DESC LIMIT -1 OFFSET ?)",
                (max_rows,),
            )

    def usage_rows(self, *, profile_id=None, credential_id=None, day=None):
        clauses, args = [], []
        for key, value in [
            ("profile_id", profile_id),
            ("credential_id", credential_id),
            ("day", day),
        ]:
            if value is not None:
                clauses.append(key + "=?")
                args.append(value)
        with self.connection() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM usage_rollups"
                    + (" WHERE " + " AND ".join(clauses) if clauses else "")
                    + " ORDER BY day DESC",
                    args,
                )
            ]

    def activity(self, *, slug=None, limit=20, before=None):
        where, args = [], []
        if slug:
            where.append("profile_slug=?")
            args.append(slug)
        with self.connection() as conn:
            heads = list(
                conn.execute(
                    "SELECT request_id,min(id) AS first_id,max(id) AS last_id "
                    "FROM attempts"
                    + (" WHERE " + " AND ".join(where) if where else "")
                    + " GROUP BY request_id"
                    + (" HAVING max(id)<?" if before is not None else "")
                    + " ORDER BY last_id DESC LIMIT ?",
                    (*args, *([before] if before is not None else []), limit),
                )
            )
            requests = []
            for head in heads:
                rows = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT * FROM attempts WHERE request_id=? ORDER BY attempt_idx,id",
                        (head["request_id"],),
                    )
                ]
                # Persisted columns are exclusively metadata; no secret table joins.
                requests.append(
                    {
                        "request_id": head["request_id"],
                        "ts": rows[0]["ts"],
                        "profile_slug": rows[0]["profile_slug"],
                        "outcome": rows[-1]["outcome"],
                        "http_status": rows[-1]["http_status"],
                        "attempt_count": len(rows),
                        "latency_ms": sum(r["latency_ms"] or 0 for r in rows),
                        "attempts": rows,
                    }
                )
            return {
                "requests": requests,
                "next_before": heads[-1]["last_id"] if len(heads) == limit else None,
            }
