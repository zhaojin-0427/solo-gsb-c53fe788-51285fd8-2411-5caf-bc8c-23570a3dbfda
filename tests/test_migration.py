"""Startup migration of legacy ``idempotent_operations`` tables.

The defect these tests pin down: the old two-phase implementation committed a
placeholder row (``status_code=200, response_json={}``) *before* the business
change and rewrote it with the real outcome only afterwards, so a crash in
between left the placeholder durably committed.  The previous startup
migration stamped every legacy row ``completed``, and from then on any request
reusing such a key replayed an empty ``200 {}`` success.

The contract enforced here:

* legacy placeholder rows (empty-object body, or any row without a complete
  final status code and serializable body) are deleted by the migration, so
  their keys execute fresh instead of replaying an empty success;
* legitimate completed records survive the migration untouched and keep
  replaying their stored response;
* databases already upgraded by the previous (buggy) migration are repaired:
  poisoned ``completed`` rows are purged while live ``in_progress`` rows are
  never touched;
* the migration is idempotent: running it repeatedly converges to the same
  state, on existing databases and on fresh ones.
"""
import importlib
import json
import os
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select, text

from conftest import make_lot


# Schema of the idempotent_operations table as written by the old two-phase
# implementation: no state/locked_at/completed_at columns and a NOT NULL
# payload (status_code=200, response_json={} for the placeholder).
LEGACY_DDL = """
CREATE TABLE idempotent_operations (
    idempotency_key VARCHAR(128) PRIMARY KEY,
    operation_type VARCHAR(64) NOT NULL,
    request_hash VARCHAR(128) NOT NULL,
    status_code INTEGER NOT NULL,
    response_json JSON NOT NULL,
    created_at DATETIME
)
"""

LEGACY_INSERT = """
INSERT INTO idempotent_operations (
    idempotency_key, operation_type, request_hash, status_code, response_json,
    created_at
) VALUES (:key, :op, :hash, :status, :body, :created)
"""


def _load_app(tmp_path, monkeypatch):
    """Bind the app modules to a fresh SQLite file, mirroring conftest."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/migration.db")

    import app.database
    import app.models
    import app.services
    import app.main

    importlib.reload(app.database)
    importlib.reload(app.models)
    importlib.reload(app.services)
    importlib.reload(app.main)
    return app.database, app.models, app.services, app.main


def _rows_by_key(database, models):
    with database.SessionLocal() as session:
        return {
            row.idempotency_key: row
            for row in session.scalars(select(models.IdempotentOperation)).all()
        }


def _qc_body(expected_version=1):
    return {
        "item": "assay",
        "value": "99.5",
        "unit": "%",
        "lower_limit": "95",
        "upper_limit": "105",
        "rule_version": "QC-SOP-1.0",
        "expected_version": expected_version,
    }


def test_legacy_placeholders_purged_and_legit_rows_completed(tmp_path, monkeypatch):
    database, models, _, _ = _load_app(tmp_path, monkeypatch)

    with database.engine.begin() as conn:
        conn.execute(text(LEGACY_DDL))
        conn.execute(text(LEGACY_INSERT), [
            # Placeholders left by a crash of the old two-phase code.
            {"key": "placeholder-1", "op": "register_qc_record", "hash": "h1",
             "status": 200, "body": "{}", "created": "2026-01-01 00:00:00"},
            {"key": "placeholder-2", "op": "commit_revision", "hash": "h2",
             "status": 200, "body": "{}", "created": "2026-01-02 00:00:00"},
            # Legitimate completed records: final status + non-empty body.
            {"key": "legit-1", "op": "create_plan", "hash": "h3", "status": 201,
             "body": json.dumps({"plan": {"id": "p1"}, "revision": {"id": "r1"}}),
             "created": "2026-01-03 00:00:00"},
            {"key": "legit-2", "op": "recall_lot", "hash": "h4", "status": 200,
             "body": json.dumps({"lot_id": "l1", "status": "recalled"}),
             "created": "2026-01-04 00:00:00"},
            # A legit record without created_at still survives; locked_at is
            # backfilled by the migration.
            {"key": "legit-3", "op": "record_deviation", "hash": "h5", "status": 201,
             "body": json.dumps({"id": "d1"}), "created": None},
        ])

    database.init_db()

    rows = _rows_by_key(database, models)
    assert set(rows) == {"legit-1", "legit-2", "legit-3"}, \
        "placeholder rows must be purged, legitimate completed records kept"

    legit = rows["legit-1"]
    assert legit.state == "completed"
    assert legit.status_code == 201
    assert legit.response_json == {"plan": {"id": "p1"}, "revision": {"id": "r1"}}
    # completed_at is backfilled from created_at for legacy rows.
    assert legit.completed_at is not None
    assert legit.completed_at == legit.created_at
    assert rows["legit-3"].locked_at is not None

    # The schema was brought up to date: new columns exist, payload nullable.
    columns = {c["name"]: c for c in inspect(database.engine).get_columns("idempotent_operations")}
    assert {"state", "locked_at", "completed_at"} <= set(columns)
    assert columns["status_code"]["nullable"] is True
    assert columns["response_json"]["nullable"] is True

    # Repeating the migration changes nothing.
    database.init_db()
    rows_again = _rows_by_key(database, models)
    assert set(rows_again) == {"legit-1", "legit-2", "legit-3"}
    assert rows_again["legit-1"].response_json == legit.response_json


def test_migrated_placeholder_key_does_not_replay_empty_success(tmp_path, monkeypatch):
    database, models, services, main = _load_app(tmp_path, monkeypatch)
    database.init_db()

    with TestClient(main.app) as client:
        lot_a = make_lot(client, "LEGACY-A")
        lot_b = make_lot(client, "LEGACY-B")

        body_a = _qc_body()
        body_b = _qc_body()
        request_a = {"lot_id": lot_a["id"], **body_a}
        request_b = {"lot_id": lot_b["id"], **body_b}
        stored_b = {
            "qc_record": {"id": "cached-qc-record", "lot_id": lot_b["id"]},
            "event": {"id": "cached-event"},
            "lot": {"id": lot_b["id"], "quality_status": "released"},
        }

        # Simulate the legacy table the old two-phase code could leave behind:
        # one placeholder for lot A's key, one legitimate record for lot B's.
        with database.engine.begin() as conn:
            conn.execute(text("DROP TABLE idempotent_operations"))
            conn.execute(text(LEGACY_DDL))
            conn.execute(text(LEGACY_INSERT), [
                {"key": "legacy-qc", "op": "register_qc_record",
                 "hash": services.stable_hash(request_a),
                 "status": 200, "body": "{}", "created": "2026-01-01 00:00:00"},
                {"key": "legit-qc", "op": "register_qc_record",
                 "hash": services.stable_hash(request_b),
                 "status": 201, "body": json.dumps(stored_b),
                 "created": "2026-01-02 00:00:00"},
            ])

        database.init_db()

        # The placeholder key executes the operation for real instead of
        # replaying an empty 200 success.
        resp = client.post(
            f"/stock-lots/{lot_a['id']}/qc-records",
            json=body_a,
            headers={"Idempotency-Key": "legacy-qc"},
        )
        assert resp.status_code == 201
        assert resp.json()["lot"]["quality_status"] == "released"
        assert resp.json()["qc_record"]["lot_id"] == lot_a["id"]

        # Replaying the same key now returns the real stored response and
        # does not execute the business change a second time.
        replay = client.post(
            f"/stock-lots/{lot_a['id']}/qc-records",
            json=body_a,
            headers={"Idempotency-Key": "legacy-qc"},
        )
        assert replay.status_code == 201
        assert replay.json() == resp.json()
        assert len(client.get(f"/stock-lots/{lot_a['id']}/qc-records").json()) == 1

        # The legitimate record survived the migration and replays verbatim,
        # without touching lot B.
        replay_b = client.post(
            f"/stock-lots/{lot_b['id']}/qc-records",
            json=body_b,
            headers={"Idempotency-Key": "legit-qc"},
        )
        assert replay_b.status_code == 201
        assert replay_b.json() == stored_b
        assert client.get(f"/stock-lots/{lot_b['id']}").json()["quality_status"] == "pending"


def test_poisoned_current_schema_is_repaired_without_touching_in_progress(
    tmp_path, monkeypatch
):
    database, models, _, _ = _load_app(tmp_path, monkeypatch)
    database.init_db()

    now = datetime.now(timezone.utc)
    with database.SessionLocal() as session:
        # What the previous migration produced from a legacy placeholder.
        session.add(models.IdempotentOperation(
            idempotency_key="poisoned", operation_type="recall_lot",
            request_hash="h1", state="completed", status_code=200,
            response_json={}, locked_at=now, completed_at=now,
        ))
        # A healthy completed record.
        session.add(models.IdempotentOperation(
            idempotency_key="healthy", operation_type="recall_lot",
            request_hash="h2", state="completed", status_code=200,
            response_json={"lot_id": "l1", "status": "recalled"},
            locked_at=now, completed_at=now,
        ))
        # A live in-progress row: owned by a running request, must survive.
        session.add(models.IdempotentOperation(
            idempotency_key="in-flight", operation_type="recall_lot",
            request_hash="h3", state="in_progress", status_code=None,
            response_json=None, locked_at=now,
        ))
        session.commit()

    database.init_db()

    rows = _rows_by_key(database, models)
    assert set(rows) == {"healthy", "in-flight"}, \
        "poisoned completed rows are purged; in-progress rows are never touched"
    assert rows["healthy"].response_json == {"lot_id": "l1", "status": "recalled"}
    assert rows["in-flight"].state == "in_progress"

    # Repeating the migration changes nothing.
    database.init_db()
    assert set(_rows_by_key(database, models)) == {"healthy", "in-flight"}


@pytest.mark.skipif(
    os.getenv("RUN_PG_IDEMPOTENCY_TESTS") != "1",
    reason="PostgreSQL migration test; set RUN_PG_IDEMPOTENCY_TESTS=1 "
           "with DATABASE_URL pointing at a test PostgreSQL",
)
def test_postgres_legacy_placeholders_purged():
    """The legacy-table migration against a real PostgreSQL database."""
    import app.database
    import app.models
    import app.services
    import app.main

    # Rebind to the PostgreSQL DATABASE_URL regardless of test ordering.
    importlib.reload(app.database)
    importlib.reload(app.models)
    importlib.reload(app.services)
    importlib.reload(app.main)
    database, models = app.database, app.models
    assert database.engine.dialect.name == "postgresql"

    with database.engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS idempotent_operations"))
        conn.execute(text("""
            CREATE TABLE idempotent_operations (
                idempotency_key VARCHAR(128) PRIMARY KEY,
                operation_type VARCHAR(64) NOT NULL,
                request_hash VARCHAR(128) NOT NULL,
                status_code INTEGER NOT NULL,
                response_json JSON NOT NULL,
                created_at TIMESTAMPTZ
            )
        """))
        conn.execute(text(
            "INSERT INTO idempotent_operations ("
            "  idempotency_key, operation_type, request_hash, status_code,"
            "  response_json, created_at"
            ") VALUES "
            "('pg-placeholder', 'register_qc_record', 'h1', 200, CAST('{}' AS JSON), now()),"
            "('pg-legit', 'create_plan', 'h2', 201,"
            " CAST('{\"plan\": {\"id\": \"p1\"}}' AS JSON), now())"
        ))

    database.init_db()

    rows = _rows_by_key(database, models)
    assert set(rows) == {"pg-legit"}
    assert rows["pg-legit"].state == "completed"
    assert rows["pg-legit"].status_code == 201
    assert rows["pg-legit"].response_json == {"plan": {"id": "p1"}}
    assert rows["pg-legit"].completed_at is not None

    # Repeating the migration changes nothing.
    database.init_db()
    assert set(_rows_by_key(database, models)) == {"pg-legit"}
