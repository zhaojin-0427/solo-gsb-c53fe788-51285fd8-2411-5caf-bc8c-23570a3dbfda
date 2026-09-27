import os
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

from app.solver import Lot, PlanSpec, solve


def make_lots():
    return [
        Lot("high", "HIGH", Decimal("2"), 10_000, 0),
        Lot("low", "LOW", Decimal("0"), 10_000, 1),
    ]


def make_spec(K=2, cap=4000, count=3, pairs=None, orders=None):
    return PlanSpec(
        target_min=Decimal("0.8"),
        target_max=Decimal("1.2"),
        aliquot_volume=1000,
        aliquot_count=count,
        final_container_capacity=1500,
        intermediate_container_capacity=cap,
        max_intermediate_containers=K,
        pipette_min_volume=100,
        pipette_increment=50,
        scheduled_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
        lots=make_lots(),
        forbidden_pairs=pairs or [],
        contamination_orders=orders or [],
    )


def test_feasible_intermediate_plan_integer_volumes():
    result = solve(make_spec(), time_limit=10)
    assert result["feasible"] is True
    assert result["waste_volume"] == 0
    assert len(result["intermediates"]) == 1
    intermediate = result["intermediates"][0]
    assert sum(item["volume"] for item in intermediate["inputs"]) == intermediate["volume"]
    assert all(item["volume"] % 50 == 0 for item in intermediate["inputs"])
    assert Decimal(intermediate["concentration"]) == Decimal("1.2")
    assert len(result["aliquots"]) == 3


def test_direct_mix_when_no_intermediate():
    result = solve(make_spec(K=0, cap=None, count=1), time_limit=10)
    assert result["feasible"] is True
    assert result["intermediates"] == []
    assert result["aliquots"][0]["route"] == "direct"
    transfers = result["transfers"]
    assert sum(step["volume"] for step in transfers) == 1000
    assert all(step["volume"] >= 100 for step in transfers)


def test_minimal_inventory_conflict_returns_evidence():
    spec = make_spec(K=0, cap=None, count=1)
    spec.lots[0] = replace(spec.lots[0], available_volume=100)
    result = solve(spec, time_limit=10)
    assert result["feasible"] is False
    assert [conflict["type"] for conflict in result["conflicts"]] == ["unmet_aliquot"]


def test_contamination_order_is_enforced():
    result = solve(
        make_spec(K=0, cap=None, count=1, orders=[("low", "high")]),
        time_limit=10,
    )
    assert result["feasible"] is True
    transfers = result["transfers"]
    low_step = next(step for step in transfers if step["from_lot_id"] == "low")
    high_step = next(step for step in transfers if step["from_lot_id"] == "high")
    assert low_step["step_no"] < high_step["step_no"]


def test_inventory_conflict_witness_when_demand_exists():
    # All existing stock is concentration-correct but insufficient; the
    # minimum relaxation is to serve what remains and mark one aliquot unmet.
    lots = [Lot("a", "A", Decimal("1.0"), 1000, 0)]
    spec = PlanSpec(
        Decimal("0.8"), Decimal("1.2"), 1000, 2, 1500, None, 0,
        100, 50, datetime(2026, 10, 1, tzinfo=timezone.utc),
        lots, [], [],
    )
    result = solve(spec, time_limit=10)
    assert result["feasible"] is False
    assert [conflict["type"] for conflict in result["conflicts"]] == ["unmet_aliquot"]
    assert result["usage_by_lot"] == {"a": 1000}
