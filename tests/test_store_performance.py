"""Reused SQLite connections keep the old transaction semantics and release their files."""

import gc
import shutil
import sqlite3
import threading
from datetime import UTC, datetime, timedelta

import pytest

from quotamesh.app import create_app
from quotamesh.store import Store


def settings(store):
    with store.connection() as conn:
        return {row["key"]: row["value"] for row in conn.execute("SELECT * FROM settings")}


def put(conn, key):
    conn.execute("INSERT INTO settings(key,value) VALUES(?,'x')", (key,))


def attempt(store, ts, request_id):
    store.record_attempt(
        ts=ts.isoformat(),
        request_id=request_id,
        profile_id=1,
        profile_slug="default",
        attempt_idx=1,
        provider_id="custom",
        model="model",
        credential_id=1,
        plan_type="FREE",
        credential_label="Fake",
        outcome="OK",
        streamed=0,
    )


def request_ids(store):
    with store.connection() as conn:
        return [row[0] for row in conn.execute("SELECT request_id FROM attempts ORDER BY id")]


def test_connection_is_reused_per_thread_with_pragmas(tmp_path):
    store = Store(tmp_path)
    with store.connection() as first:
        pass
    with store.connection() as second:
        assert second is first
        assert second.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert second.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert second.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
        assert second.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    seen = []

    def other_thread():
        with store.connection() as conn:
            seen.append(conn)

    thread = threading.Thread(target=other_thread)
    thread.start()
    thread.join()
    assert seen and seen[0] is not first
    store.close()


def test_nested_failure_rolls_back_only_inner_work(tmp_path):
    store = Store(tmp_path)
    with store.connection() as conn:
        put(conn, "outer")
        with pytest.raises(RuntimeError), store.connection() as inner:
            put(inner, "inner")
            raise RuntimeError("inner failure")
        assert conn.in_transaction
        put(conn, "outer-after")
    assert {"outer", "outer-after"} <= settings(store).keys()
    assert "inner" not in settings(store)
    store.close()


def test_outer_failure_undoes_nested_success_inside_its_transaction(tmp_path):
    store = Store(tmp_path)
    with pytest.raises(RuntimeError), store.connection() as conn:
        put(conn, "outer")
        with store.connection() as inner:
            put(inner, "inner")
        raise RuntimeError("outer failure")
    assert not {"outer", "inner"} & settings(store).keys()
    with store.connection() as conn:
        assert not conn.in_transaction
    store.close()


def test_nested_level_without_open_transaction_commits_like_a_separate_connection(tmp_path):
    store = Store(tmp_path)
    with pytest.raises(RuntimeError), store.connection() as conn:
        conn.execute("SELECT 1").fetchone()
        # BEGIN IMMEDIATE methods work at any depth on the reused connection.
        identifier = store.add_credential(
            provider_id="custom",
            label="Nested",
            plan_type="FREE",
            base_url="http://127.0.0.1:8799/v1",
            secret_value="fake-nested",
            env_name=None,
        )
        raise RuntimeError("outer failure after an independent commit")
    assert identifier == 1
    with pytest.raises(RuntimeError), store.connection() as conn:
        put(conn, "outer")
        store.add_credential(
            provider_id="custom",
            label="Joined",
            plan_type="FREE",
            base_url="http://127.0.0.1:8799/v1",
            secret_value="fake-joined",
            env_name=None,
        )
        raise RuntimeError("outer failure undoes the joined insert")
    labels = [c["label"] for c in store.safe_routing(datetime.now(UTC))["credentials"]]
    assert labels == ["Nested"] and "outer" not in settings(store)
    store.close()


def test_failed_block_leaves_no_transaction_for_the_next_operation(tmp_path):
    store = Store(tmp_path)
    with pytest.raises(sqlite3.IntegrityError), store.connection() as conn:
        put(conn, "duplicate")
        put(conn, "duplicate")
    with store.connection() as conn:
        assert not conn.in_transaction
        put(conn, "next")
    assert "duplicate" not in settings(store) and "next" in settings(store)
    store.close()


def test_retention_prunes_every_n_inserts_with_unchanged_semantics(tmp_path, monkeypatch):
    store = Store(tmp_path)
    monkeypatch.setattr(store, "PRUNE_EVERY", 4)
    calls = []
    original = store.prune_history
    monkeypatch.setattr(
        store, "prune_history", lambda now, **kw: calls.append(now) or original(now, **kw)
    )
    now = datetime.now(UTC)
    for index in range(3):
        attempt(store, now - timedelta(days=40), f"old-{index}")
    # No per-insert DELETE: expired rows survive until the periodic prune.
    assert request_ids(store) == ["old-0", "old-1", "old-2"] and not calls
    attempt(store, now, "fresh")
    assert request_ids(store) == ["fresh"]
    assert calls == [now]  # The newest attempt's timestamp, as the per-insert DELETE used.
    for index in range(3):
        attempt(store, now, f"more-{index}")
    assert len(calls) == 1
    attempt(store, now, "fourth")
    assert len(calls) == 2
    store.close()


def test_prune_failure_never_unrecords_the_attempt(tmp_path, monkeypatch):
    store = Store(tmp_path)
    monkeypatch.setattr(store, "PRUNE_EVERY", 1)

    def broken(now, **kwargs):
        raise sqlite3.OperationalError("simulated prune failure")

    monkeypatch.setattr(store, "prune_history", broken)
    attempt(store, datetime.now(UTC), "kept")
    assert request_ids(store) == ["kept"]
    assert store.usage_rows()[0]["attempts"] == 1
    store.close()


def test_close_releases_files_and_store_reopens_lazily(tmp_path):
    directory = tmp_path / "data"
    store = Store(directory)
    with store.connection() as conn:
        put(conn, "before-close")
    store.close()
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")
    assert "before-close" in settings(store)  # A fresh connection is opened on demand.
    store.close()
    store.close()  # Idempotent.
    shutil.rmtree(directory)  # Windows refuses to delete files that are still open.
    assert not directory.exists()


def test_abandoned_store_is_closed_by_its_finalizer(tmp_path):
    directory = tmp_path / "abandoned"

    def use_and_drop():
        store = Store(directory)
        with store.connection() as conn:
            put(conn, "abandoned")

    use_and_drop()
    gc.collect()
    shutil.rmtree(directory)


def test_finished_thread_releases_its_connection(tmp_path):
    store = Store(tmp_path)
    before = len(store._registry)

    def worker():
        with store.connection() as conn:
            conn.execute("SELECT 1").fetchone()

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    gc.collect()
    assert len(store._registry) == before
    store.close()


async def test_lifespan_shutdown_closes_the_store(tmp_path):
    directory = tmp_path / "app"
    app = create_app(directory)
    async with app.router.lifespan_context(app):
        assert app.state.store.profiles() == []
    assert not app.state.store._registry
    assert app.state.client.is_closed
    shutil.rmtree(directory)


def test_failed_migration_releases_its_connection(tmp_path, monkeypatch):
    directory = tmp_path / "failing"
    Store(directory).close()
    with sqlite3.connect(directory / "quotamesh.db") as conn:
        conn.execute("PRAGMA user_version=99")
    conn.close()
    with pytest.raises(RuntimeError, match="newer"):
        Store(directory)
    shutil.rmtree(directory)
