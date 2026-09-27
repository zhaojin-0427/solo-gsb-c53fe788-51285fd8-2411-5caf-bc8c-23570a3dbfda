import os
import tempfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")

    # Import after the environment variable is set so engine construction sees it.
    import importlib

    import app.database
    import app.models
    import app.services
    import app.main
    importlib.reload(app.database)
    importlib.reload(app.models)
    importlib.reload(app.services)
    importlib.reload(app.main)

    with TestClient(app.main.app) as test_client:
        yield test_client


def make_lot(client, code, concentration="2", **overrides):
    body = {
        "code": code,
        "concentration": concentration,
        "available_volume": 10000,
        "expires_at": "2026-12-31T00:00:00Z",
    }
    body.update(overrides)
    response = client.post("/stock-lots", json=body)
    assert response.status_code == 201
    return response.json()


def release_lot(client, lot, value="99.5", lower="95", upper="105"):
    response = client.post(
        f"/stock-lots/{lot['id']}/qc-records",
        json={
            "item": "assay",
            "value": value,
            "unit": "%",
            "lower_limit": lower,
            "upper_limit": upper,
            "rule_version": "QC-SOP-1.0",
            "expected_version": lot["version"],
        },
    )
    assert response.status_code == 201
    return response.json()


def plan_body(high_id, low_id, count=3):
    return {
        "name": "demo",
        "target_min_concentration": "0.8",
        "target_max_concentration": "1.2",
        "aliquot_volume": 1000,
        "aliquot_count": count,
        "final_container_capacity": 1500,
        "intermediate_container_capacity": 4000,
        "max_intermediate_containers": 2,
        "pipette_min_volume": 100,
        "pipette_increment": 50,
        "scheduled_at": "2026-10-01T00:00:00Z",
        "stock_lot_ids": [high_id, low_id],
        "forbidden_pairs": [],
        "contamination_orders": [],
    }


def committed_plan(client):
    """Two released lots, one feasible plan, one committed revision."""
    high = make_lot(client, "HIGH", "2")
    low = make_lot(client, "LOW", "0", expires_at="2026-10-31T00:00:00Z")
    release_lot(client, high)
    release_lot(client, low)
    plan = client.post("/plans", json=plan_body(high["id"], low["id"])).json()
    revision_id = plan["revision"]["id"]
    commit = client.post(
        f"/revisions/{revision_id}/commit",
        json={"expected_revision_version": 1, "expected_lot_versions": {}, "force": False},
    )
    assert commit.status_code == 200
    return high, low, plan["plan"], plan["revision"], commit.json()
