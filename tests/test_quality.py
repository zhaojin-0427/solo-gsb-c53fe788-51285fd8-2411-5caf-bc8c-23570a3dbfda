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


def test_qc_record_in_range_releases_lot(client):
    lot = make_lot(client, "QC-1")
    assert lot["quality_status"] == "pending"

    result = release_lot(client, lot)
    assert result["qc_record"]["in_range"] is True
    assert result["qc_record"]["item"] == "assay"
    assert result["lot"]["quality_status"] == "released"
    assert result["lot"]["version"] == 2
    assert result["event"]["event_type"] == "released"
    assert result["event"]["version_after"] == 2
    assert result["event"]["qc_record_id"] == result["qc_record"]["id"]

    records = client.get(f"/stock-lots/{lot['id']}/qc-records")
    assert records.status_code == 200
    assert len(records.json()) == 1
    assert records.json()[0]["rule_version"] == "QC-SOP-1.0"
    assert records.json()[0]["value"] == "99.5"

    events = client.get(f"/stock-lots/{lot['id']}/events")
    assert events.status_code == 200
    assert [event["event_type"] for event in events.json()] == ["released"]


def test_qc_record_out_of_range_quarantines_and_blocks_plan(client):
    high = make_lot(client, "HIGH-OOS", "2")
    low = make_lot(client, "LOW-OK", "0", expires_at="2026-10-31T00:00:00Z")
    response = client.post(
        f"/stock-lots/{high['id']}/qc-records",
        json=qc_payload(expected_version=1, value="120"),
    )
    assert response.status_code == 201
    assert response.json()["qc_record"]["in_range"] is False
    assert response.json()["lot"]["quality_status"] == "quarantined"
    assert response.json()["event"]["event_type"] == "quarantined"
    release_lot(client, low)

    plan = client.post("/plans", json=plan_body(high["id"], low["id"]))
    assert plan.status_code == 422
    error = plan.json()["error"]
    assert error["code"] == "lot_not_released"
    evidence = error["details"]["conflicts"][0]["evidence"]
    assert [row["lot_id"] for row in evidence] == [high["id"]]
    assert evidence[0]["quality_status"] == "quarantined"
    assert evidence[0]["latest_event"]["event_type"] == "quarantined"


def test_pending_lot_blocks_plan_creation(client):
    high = make_lot(client, "PENDING-HIGH", "2")
    low = make_lot(client, "RELEASED-LOW", "0", expires_at="2026-10-31T00:00:00Z")
    release_lot(client, low)

    plan = client.post("/plans", json=plan_body(high["id"], low["id"]))
    assert plan.status_code == 422
    evidence = plan.json()["error"]["details"]["conflicts"][0]["evidence"]
    assert evidence[0]["lot_id"] == high["id"]
    assert evidence[0]["quality_status"] == "pending"
    assert evidence[0]["latest_event"] is None


def test_qc_version_conflict_and_state_guard(client):
    lot = make_lot(client, "CAS-1")
    conflict = client.post(
        f"/stock-lots/{lot['id']}/qc-records", json=qc_payload(expected_version=7)
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "lot_version_conflict"
    assert conflict.json()["error"]["details"]["current_version"] == 1

    release_lot(client, lot)
    again = client.post(
        f"/stock-lots/{lot['id']}/qc-records", json=qc_payload(expected_version=2)
    )
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "lot_not_testable"


def test_qc_registration_idempotent_replay(client):
    lot = make_lot(client, "IDEM-1")
    first = client.post(
        f"/stock-lots/{lot['id']}/qc-records",
        json=qc_payload(expected_version=1),
        headers={"Idempotency-Key": "qc-1"},
    )
    assert first.status_code == 201
    replay = client.post(
        f"/stock-lots/{lot['id']}/qc-records",
        json=qc_payload(expected_version=1),
        headers={"Idempotency-Key": "qc-1"},
    )
    assert replay.status_code == 201
    assert replay.json() == first.json()
    # No duplicated record or event was appended by the replay.
    assert len(client.get(f"/stock-lots/{lot['id']}/qc-records").json()) == 1
    assert len(client.get(f"/stock-lots/{lot['id']}/events").json()) == 1


def test_recall_freezes_committed_revision_and_replays(client):
    high, low, plan, revision, commit = committed_plan(client)
    assert commit["lot_versions"] == {high["id"]: 3, low["id"]: 3}

    recall = client.post(
        f"/stock-lots/{high['id']}/recall",
        json={"expected_version": 3, "reason": "supplier notification"},
        headers={"Idempotency-Key": "recall-1"},
    )
    assert recall.status_code == 200
    body = recall.json()
    assert body["status"] == "recalled"
    assert body["version"] == 4
    assert body["event"]["event_type"] == "recalled"
    assert body["event"]["reason"] == "supplier notification"

    assert len(body["affected_plans"]) == 1
    affected = body["affected_plans"][0]
    assert affected["plan_id"] == plan["id"]
    assert affected["revision_id"] == revision["id"]
    assert affected["revision_number"] == 1
    # Every unexecuted reservation of the compromised revision is frozen.
    assert affected["frozen_volume"] == sum(commit["reservations"].values())
    assert affected["affected_aliquot_ids"] == ["aliquot-1", "aliquot-2", "aliquot-3"]
    edge_sources = {edge["source"] for edge in affected["affected_edges"]}
    assert high["id"] in edge_sources
    edge_targets = {edge["target"] for edge in affected["affected_edges"]}
    assert {"aliquot-1", "aliquot-2", "aliquot-3"} <= edge_targets

    lot_view = client.get(f"/stock-lots/{high['id']}").json()
    assert lot_view["quality_status"] == "recalled"
    assert lot_view["version"] == 4

    replay = client.post(
        f"/stock-lots/{high['id']}/recall",
        json={"expected_version": 3, "reason": "supplier notification"},
        headers={"Idempotency-Key": "recall-1"},
    )
    assert replay.status_code == 200
    assert replay.json() == body
    events = client.get(f"/stock-lots/{high['id']}/events").json()
    assert [event["event_type"] for event in events] == ["released", "recalled"]

    again = client.post(
        f"/stock-lots/{high['id']}/recall",
        json={"expected_version": 4, "reason": "duplicate"},
    )
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "lot_not_recallable"


def test_recall_guards(client):
    high = make_lot(client, "G-HIGH", "2")
    # A pending lot cannot be recalled.
    resp = client.post(f"/stock-lots/{high['id']}/recall", json={"expected_version": 1})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "lot_not_recallable"

    release_lot(client, high)
    # Stale expected_version is rejected before any state change.
    resp = client.post(f"/stock-lots/{high['id']}/recall", json={"expected_version": 1})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "lot_version_conflict"

    # A quarantined lot cannot be recalled either.
    bad = make_lot(client, "G-BAD", "0", expires_at="2026-10-31T00:00:00Z")
    client.post(
        f"/stock-lots/{bad['id']}/qc-records",
        json=qc_payload(expected_version=1, value="120"),
    )
    resp = client.post(f"/stock-lots/{bad['id']}/recall", json={"expected_version": 2})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "lot_not_recallable"


def test_recall_idempotency_key_conflict(client):
    high, _, _, _, _ = committed_plan(client)
    first = client.post(
        f"/stock-lots/{high['id']}/recall",
        json={"expected_version": 3, "reason": "a"},
        headers={"Idempotency-Key": "recall-dup"},
    )
    assert first.status_code == 200
    conflict = client.post(
        f"/stock-lots/{high['id']}/recall",
        json={"expected_version": 3, "reason": "b"},
        headers={"Idempotency-Key": "recall-dup"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"


def test_commit_revalidates_quality_and_reports_blocking_lot(client):
    high = make_lot(client, "C-HIGH", "2")
    low = make_lot(client, "C-LOW", "0", expires_at="2026-10-31T00:00:00Z")
    release_lot(client, high)
    release_lot(client, low)
    plan = client.post("/plans", json=plan_body(high["id"], low["id"])).json()
    revision_id = plan["revision"]["id"]

    # Recall after the revision was drafted but before it is committed.
    recall = client.post(
        f"/stock-lots/{high['id']}/recall",
        json={"expected_version": 2, "reason": "hold"},
    )
    assert recall.status_code == 200
    assert recall.json()["affected_plans"] == []

    commit = client.post(
        f"/revisions/{revision_id}/commit",
        json={"expected_revision_version": 1, "expected_lot_versions": {}, "force": False},
    )
    assert commit.status_code == 409
    error = commit.json()["error"]
    assert error["code"] == "lot_not_released"
    evidence = error["details"]["conflicts"][0]["evidence"]
    assert [row["lot_id"] for row in evidence] == [high["id"]]
    assert evidence[0]["quality_status"] == "recalled"
    assert evidence[0]["latest_event"]["event_type"] == "recalled"
    assert evidence[0]["latest_event"]["reason"] == "hold"


def test_new_revision_blocked_after_recall(client):
    high, low, plan, revision, _ = committed_plan(client)
    recall = client.post(
        f"/stock-lots/{low['id']}/recall",
        json={"expected_version": 3, "reason": "contamination"},
    )
    assert recall.status_code == 200

    blocked = client.post(f"/plans/{plan['id']}/revisions", json={})
    assert blocked.status_code == 422
    error = blocked.json()["error"]
    assert error["code"] == "lot_not_released"
    evidence = error["details"]["conflicts"][0]["evidence"]
    assert [row["lot_id"] for row in evidence] == [low["id"]]
    assert evidence[0]["latest_event"]["event_type"] == "recalled"
