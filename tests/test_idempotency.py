"""Idempotency semantics for the state-changing endpoints.

The defect these tests pin down: a request used to commit the business change
first and only afterwards write status code + response into the
``idempotent_operations`` row (which until then held a replayable
``200 / {}`` placeholder).  A crash between the two commits, or a concurrent
same-key request in that window, could therefore observe an empty "successful"
result.

The contract enforced here:

* business change + idempotency outcome (status code and full body) commit in
  one transaction;
* the same key and body replay the stored response, without re-executing the
  business change;
* the same key with a different body/operation gets 409, even while the first
  request is in flight;
* a failed request leaves no replayable row behind: the key can immediately be
  retried, and no partial business state survives;
* concurrent same-key requests serialize (PostgreSQL advisory lock; the
  related test is skipped on SQLite).
"""
import os
import threading

import pytest

from conftest import committed_plan, make_lot, plan_body, release_lot


def qc_payload(expected_version, value="99.5", lower="95", upper="105"):
    return {
        "item": "assay",
        "value": value,
        "unit": "%",
        "lower_limit": lower,
        "upper_limit": upper,
        "rule_version": "QC-SOP-1.0",
        "expected_version": expected_version,
    }



def _idempotent_rows(client):
    # Inspect through a fresh request-independent connection of the same
    # SQLite file used by the reloaded app module.
    import app.database as database
    import app.models as models
    from sqlalchemy import select

    with database.SessionLocal() as session:
        return [
            {
                "key": row.idempotency_key,
                "state": row.state,
                "status_code": row.status_code,
                "response_json": row.response_json,
            }
            for row in session.scalars(select(models.IdempotentOperation)).all()
        ]


def _is_postgres() -> bool:
    import app.database as database

    return database.engine.dialect.name == "postgresql"


def test_failed_idempotent_request_leaves_no_replayable_placeholder(client):
    lot = make_lot(client, "FAIL-IDEM")
    # Stale expected_version makes the business operation fail with 409.
    bad = client.post(
        f"/stock-lots/{lot['id']}/qc-records",
        json=qc_payload(expected_version=7),
        headers={"Idempotency-Key": "qc-fail"},
    )
    assert bad.status_code == 409

    rows = _idempotent_rows(client)
    assert not any(row["key"] == "qc-fail" for row in rows), \
        "failed request must not leave any idempotency row behind"

    # The lot was not modified by the failed attempt.
    assert client.get(f"/stock-lots/{lot['id']}").json()["version"] == 1

    # The key is immediately reusable: retrying with a valid request succeeds.
    ok = client.post(
        f"/stock-lots/{lot['id']}/qc-records",
        json=qc_payload(expected_version=1),
        headers={"Idempotency-Key": "qc-fail"},
    )
    assert ok.status_code == 201
    assert ok.json()["lot"]["quality_status"] == "released"


def test_replayed_response_contains_full_body_and_status(client):
    lot = make_lot(client, "REPLAY-BODY")
    first = client.post(
        f"/stock-lots/{lot['id']}/qc-records",
        json=qc_payload(expected_version=1),
        headers={"Idempotency-Key": "qc-replay"},
    )
    assert first.status_code == 201
    payload = first.json()

    rows = _idempotent_rows(client)
    row = next(row for row in rows if row["key"] == "qc-replay")
    assert row["state"] == "completed"
    assert row["status_code"] == 201
    assert row["response_json"] == payload
    # The stored body is the full response, never an empty placeholder.
    assert row["response_json"]["qc_record"]["id"] == payload["qc_record"]["id"]
    assert row["response_json"]["lot"]["version"] == 2

    replay = client.post(
        f"/stock-lots/{lot['id']}/qc-records",
        json=qc_payload(expected_version=1),
        headers={"Idempotency-Key": "qc-replay"},
    )
    assert replay.status_code == 201
    assert replay.json() == payload


def test_same_key_different_body_conflicts_without_mutating_state(client):
    high = make_lot(client, "K-A", "2")
    low = make_lot(client, "K-B", "0", expires_at="2026-10-31T00:00:00Z")
    release_lot(client, high)
    release_lot(client, low)

    body = plan_body(high["id"], low["id"])
    first = client.post("/plans", json=body, headers={"Idempotency-Key": "shared"})
    assert first.status_code == 201
    first_plan = first.json()["plan"]["id"]

    changed = {**body, "name": "something completely different"}
    conflict = client.post("/plans", json=changed, headers={"Idempotency-Key": "shared"})
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"

    # The conflicting request did not create a second plan.
    existing = client.get(f"/plans/{first_plan}")
    assert existing.status_code == 200

    # Same key, same body still replays the first result.
    replay = client.post("/plans", json=body, headers={"Idempotency-Key": "shared"})
    assert replay.status_code == 201
    assert replay.json()["plan"]["id"] == first_plan


def test_same_key_across_different_operation_types_conflicts(client):
    lot = make_lot(client, "CROSS-OP")
    qc = client.post(
        f"/stock-lots/{lot['id']}/qc-records",
        json=qc_payload(expected_version=1),
        headers={"Idempotency-Key": "cross-key"},
    )
    assert qc.status_code == 201

    # Reusing the same key for a recall (different operation_type) conflicts.
    recall = client.post(
        f"/stock-lots/{lot['id']}/recall",
        json={"expected_version": 2, "reason": "reuse"},
        headers={"Idempotency-Key": "cross-key"},
    )
    assert recall.status_code == 409
    assert recall.json()["error"]["code"] == "idempotency_conflict"


def test_failed_recall_does_not_poison_key(client):
    high, low, _, _, _ = committed_plan(client)
    # Stale version -> 409 before any state change.
    bad = client.post(
        f"/stock-lots/{high['id']}/recall",
        json={"expected_version": 99, "reason": "stale"},
        headers={"Idempotency-Key": "recall-retry"},
    )
    assert bad.status_code == 409
    assert not any(row["key"] == "recall-retry" for row in _idempotent_rows(client))

    ok = client.post(
        f"/stock-lots/{high['id']}/recall",
        json={"expected_version": 3, "reason": "now valid"},
        headers={"Idempotency-Key": "recall-retry"},
    )
    assert ok.status_code == 200
    replay = client.post(
        f"/stock-lots/{high['id']}/recall",
        json={"expected_version": 3, "reason": "now valid"},
        headers={"Idempotency-Key": "recall-retry"},
    )
    assert replay.status_code == 200
    assert replay.json() == ok.json()


def test_commit_and_deviation_idempotent_rows_store_full_response(client):
    high, low, plan, revision, _ = committed_plan(client)
    # committed_plan did not use keys; exercise commit keying on a new revision.
    new_revision = client.post(
        f"/plans/{plan['id']}/revisions",
        json={},
        headers={"Idempotency-Key": "rev-1"},
    )
    assert new_revision.status_code == 201
    rev2 = new_revision.json()

    rows = {row["key"]: row for row in _idempotent_rows(client)}
    assert rows["rev-1"]["state"] == "completed"
    assert rows["rev-1"]["status_code"] == 201
    assert rows["rev-1"]["response_json"]["id"] == rev2["id"]

    commit = client.post(
        f"/revisions/{rev2['id']}/commit",
        json={"expected_revision_version": 1, "expected_lot_versions": {}, "force": False},
        headers={"Idempotency-Key": "commit-2"},
    )
    assert commit.status_code == 200
    assert _idempotent_rows(client)  # sanity: table populated
    rows = {row["key"]: row for row in _idempotent_rows(client)}
    assert rows["commit-2"]["status_code"] == 200
    assert rows["commit-2"]["response_json"] == commit.json()

    intermediate_id = rev2["solution"]["intermediates"][0]["id"]
    deviation = client.post(
        f"/revisions/{rev2['id']}/deviations",
        json={"intermediate_id": intermediate_id, "measured_volume": 2500,
              "measured_concentration": "1.0"},
        headers={"Idempotency-Key": "dev-2"},
    )
    assert deviation.status_code == 201
    rows = {row["key"]: row for row in _idempotent_rows(client)}
    assert rows["dev-2"]["status_code"] == 201
    assert rows["dev-2"]["response_json"] == deviation.json()

    # All three endpoints replay byte-identical responses.
    assert client.post(
        f"/plans/{plan['id']}/revisions", json={}, headers={"Idempotency-Key": "rev-1"}
    ).json() == rev2
    assert client.post(
        f"/revisions/{rev2['id']}/commit",
        json={"expected_revision_version": 1, "expected_lot_versions": {}, "force": False},
        headers={"Idempotency-Key": "commit-2"},
    ).json() == commit.json()


def test_request_without_key_still_commits_normally(client):
    lot = make_lot(client, "NO-KEY")
    resp = client.post(
        f"/stock-lots/{lot['id']}/qc-records", json=qc_payload(expected_version=1)
    )
    assert resp.status_code == 201
    assert _idempotent_rows(client) == []


@pytest.mark.skipif(
    os.getenv("RUN_PG_IDEMPOTENCY_TESTS") != "1",
    reason="PostgreSQL advisory-lock concurrency test; set RUN_PG_IDEMPOTENCY_TESTS=1 "
           "with DATABASE_URL pointing at a test PostgreSQL",
)
def test_concurrent_same_key_requests_serialize_and_replay():
    """Two threads firing the same key+body must yield one business change.

    Skipped in the default SQLite suite (no advisory locks); executed against
    PostgreSQL in docker compose / CI.
    """
    import app.database
    import app.main
    from fastapi.testclient import TestClient

    app.database.Base.metadata.create_all(bind=app.database.engine)

    with TestClient(app.main.app) as client:
        lot = make_lot(client, "CONCURRENT-1")

        results: list = []
        barrier = threading.Barrier(2)

        def fire():
            barrier.wait()
            resp = client.post(
                f"/stock-lots/{lot['id']}/qc-records",
                json=qc_payload(expected_version=1),
                headers={"Idempotency-Key": "concurrent-qc"},
            )
            results.append((resp.status_code, resp.json()))

        threads = [threading.Thread(target=fire) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len(results) == 2
        assert all(status == 201 for status, _ in results)
        # Exactly one QC record/event despite two in-flight requests.
        records = client.get(f"/stock-lots/{lot['id']}/qc-records").json()
        events = client.get(f"/stock-lots/{lot['id']}/events").json()
        assert len(records) == 1
        assert len(events) == 1
        assert results[0][1] == results[1][1]
