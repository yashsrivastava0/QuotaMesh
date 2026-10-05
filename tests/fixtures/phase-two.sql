BEGIN TRANSACTION;
CREATE TABLE attempts (
                      id INTEGER PRIMARY KEY, ts TEXT NOT NULL, request_id TEXT NOT NULL,
                      profile_id INTEGER NOT NULL, client_label TEXT,
                      attempt_idx INTEGER NOT NULL, provider_id TEXT NOT NULL, model TEXT NOT NULL,
                      credential_id INTEGER, outcome TEXT NOT NULL, http_status INTEGER,
                      error_class TEXT, latency_ms INTEGER, streamed INTEGER NOT NULL,
                      input_tokens INTEGER, output_tokens INTEGER,
                      provider_cost_usd REAL, estimated_cost_usd REAL
                    , paid INTEGER NOT NULL DEFAULT 0, skipped_json TEXT);
INSERT INTO "attempts" VALUES(1,'2026-10-01T10:00:00+00:00','legacy-request',1,NULL,1,'custom','legacy-model',1,'OK',NULL,NULL,NULL,0,10,20,0.25,NULL,1,NULL);
CREATE TABLE credentials (
                      id INTEGER PRIMARY KEY, provider_id TEXT NOT NULL, label TEXT NOT NULL,
                      plan_type TEXT NOT NULL, account_label TEXT, quota_group TEXT,
                      priority INTEGER NOT NULL DEFAULT 0, starting_credit_usd REAL,
                      trial_expires_at TEXT, base_url TEXT NOT NULL, fingerprint TEXT NOT NULL,
                      status TEXT NOT NULL DEFAULT 'ACTIVE', status_reason TEXT,
                      enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
                      updated_at TEXT NOT NULL
                    );
INSERT INTO "credentials" VALUES(1,'custom','Legacy paid','PAID',NULL,NULL,0,NULL,NULL,'http://127.0.0.1:8799/v1','d9cb78d2b150','ACTIVE',NULL,1,'2026-10-05T23:21:59.729080+00:00','2026-10-05T23:21:59.729080+00:00');
CREATE TABLE daily_usage (
                      day TEXT NOT NULL, profile_id INTEGER NOT NULL,
                      cost_usd REAL NOT NULL DEFAULT 0, unknown_cost INTEGER NOT NULL DEFAULT 0,
                      PRIMARY KEY(day, profile_id)
                    );
INSERT INTO "daily_usage" VALUES('2026-10-01',1,3.25,1);
CREATE TABLE profile_targets (
                      profile_id INTEGER NOT NULL REFERENCES project_profiles(id) ON DELETE CASCADE,
                      position INTEGER NOT NULL, provider_id TEXT NOT NULL, model TEXT NOT NULL,
                      credential_id INTEGER REFERENCES credentials(id), input_price REAL, output_price REAL,
                      PRIMARY KEY (profile_id, position)
                    );
INSERT INTO "profile_targets" VALUES(1,1,'custom','legacy-model',1,NULL,NULL);
CREATE TABLE project_profiles (
                      id INTEGER PRIMARY KEY, name TEXT NOT NULL, slug TEXT NOT NULL UNIQUE,
                      allow_paid INTEGER NOT NULL DEFAULT 0, allow_trial INTEGER NOT NULL DEFAULT 1,
                      max_attempts INTEGER NOT NULL DEFAULT 1,
                      nonstream_deadline_s INTEGER NOT NULL DEFAULT 45,
                      first_event_timeout_s INTEGER NOT NULL DEFAULT 30,
                      paid_daily_cap_usd REAL, paid_monthly_cap_usd REAL,
                      enabled INTEGER NOT NULL DEFAULT 1
                    , allow_unknown_price INTEGER NOT NULL DEFAULT 0);
INSERT INTO "project_profiles" VALUES(1,'Default','default',1,1,1,45,30,NULL,NULL,1,0);
CREATE TABLE quota_state (
                      quota_group TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL,
                      until TEXT, strikes INTEGER NOT NULL DEFAULT 0, reason TEXT,
                      PRIMARY KEY(quota_group, model)
                    );
CREATE TABLE secrets (
                      credential_id INTEGER PRIMARY KEY REFERENCES credentials(id) ON DELETE CASCADE,
                      secret_value TEXT, env_name TEXT,
                      CHECK ((secret_value IS NOT NULL) != (env_name IS NOT NULL))
                    );
INSERT INTO "secrets" VALUES(1,'fake-legacy-secret',NULL);
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE INDEX attempts_recent ON attempts(ts DESC);
COMMIT;
PRAGMA user_version=2;
