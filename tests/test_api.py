from conftest import make_lot, plan_body, release_lot


def test_plan_commit_and_deviation_trace(client):
    high = make_lot(client, "HIGH", "2")
    low = make_lot(client, "LOW", "0", expires_at="2026-10-31T00:00:00Z")
    release_lot(client, high)
    release_lot(client, low)

    first = client.post(
        "/plans", json=plan_body(high["id"], low["id"]),
        headers={"Idempotency-Key": "plan-1"},
    )
    assert first.status_code == 201
    plan_json = first.json()
    revision = plan_json["revision"]
    assert revision["feasible"] is True
    revision_id = revision["id"]

    replay = client.post(
        "/plans", json=plan_body(high["id"], low["id"]),
        headers={"Idempotency-Key": "plan-1"},
    )
    assert replay.status_code == 201
    assert replay.json()["revision"]["id"] == revision_id

    commit = client.post(
        f"/revisions/{revision_id}/commit",
        json={"expected_revision_version": 1, "expected_lot_versions": {}, "force": False},
        headers={"Idempotency-Key": "commit-1"},
    )
    assert commit.status_code == 200
    committed_lots = commit.json()["lot_versions"]
    # version 1: created, 2: QC released, 3: reservation committed
    assert all(version == 3 for version in committed_lots.values())

    intermediate_id = revision["solution"]["intermediates"][0]["id"]
    deviation = client.post(
        f"/revisions/{revision_id}/deviations",
        json={"intermediate_id": intermediate_id, "measured_volume": 2500,
              "measured_concentration": "1.0"},
        headers={"Idempotency-Key": "deviation-1"},
    )
    assert deviation.status_code == 201
    body = deviation.json()
    assert body["feasible"] is True
    assert len(body["affected_aliquot_ids"]) == 3
    edges = body["result"]["source_chain"]["edges"]
    assert any(edge.get("status") == "superseded" for edge in edges)
    assert any(edge["source"].startswith("measured::") for edge in edges)
