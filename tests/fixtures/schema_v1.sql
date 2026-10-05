
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
                    