"""CP-SAT based integer-volume lab mixing planner.

The network is deliberately limited to stock -> optional one homogeneous
intermediate vessel -> final aliquot, plus stock -> final direct mixing.  It is
enough to express common multi-step dilution/blending work while keeping every
volume integral and every concentration constraint linear.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Any

from ortools.sat.python import cp_model


SCALE_LIMIT = 12


@dataclass(frozen=True)
class Lot:
    id: str
    code: str
    concentration: Decimal
    available_volume: int
    rank: int
    direct_only: bool = False
    virtual: bool = False


@dataclass
class PlanSpec:
    target_min: Decimal
    target_max: Decimal
    aliquot_volume: int
    aliquot_count: int
    final_container_capacity: int
    intermediate_container_capacity: int | None
    max_intermediate_containers: int
    pipette_min_volume: int
    pipette_increment: int
    scheduled_at: Any
    lots: list[Lot]
    forbidden_pairs: list[tuple[str, str]]
    contamination_orders: list[tuple[str, str]]


class PlanningRequestError(ValueError):
    pass


def decimal_value(value: str | Decimal) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def concentration_scale(values: list[str | Decimal]) -> int:
    exponents = []
    for value in values:
        dec = decimal_value(value)
        exponent = -dec.as_tuple().exponent
        if exponent > 0:
            exponents.append(exponent)
    scale = 10 ** max(exponents, default=0)
    if scale > 10**SCALE_LIMIT:
        raise PlanningRequestError(
            f"concentration cannot have more than {SCALE_LIMIT} decimal places"
        )
    return scale


def scaled(dec: Decimal, scale: int) -> int:
    return int((dec * scale).to_integral_value())


def normalize_concentration(value: str | Decimal, scale: int) -> int:
    dec = decimal_value(value)
    if dec < 0:
        raise PlanningRequestError("concentration must be non-negative")
    return scaled(dec, scale)


def concentration_string(value: int, scale: int) -> str:
    return format(Decimal(value) / Decimal(scale), "f")


def validate_and_build_spec(
    request: dict[str, Any],
    availability: dict[str, int],
    lot_records: list[Any],
) -> PlanSpec:
    target_min = decimal_value(request["target_min_concentration"])
    target_max = decimal_value(request["target_max_concentration"])
    if target_min < 0 or target_min > target_max:
        raise PlanningRequestError("invalid target concentration interval")

    increment = int(request["pipette_increment"])
    minimum = int(request["pipette_min_volume"])
    volume = int(request["aliquot_volume"])
    if increment <= 0 or minimum <= 0:
        raise PlanningRequestError("pipette minimum and increment must be positive")
    if minimum < increment:
        raise PlanningRequestError("pipette_min_volume must be >= pipette_increment")
    if volume < minimum:
        raise PlanningRequestError("aliquot_volume is below pipette minimum")
    if volume % increment:
        raise PlanningRequestError("aliquot_volume must be a multiple of pipette_increment")
    if volume > int(request["final_container_capacity"]):
        raise PlanningRequestError("aliquot_volume exceeds final container capacity")

    max_k = int(request["max_intermediate_containers"])
    cap = request.get("intermediate_container_capacity")
    if max_k > 0 and cap is None:
        raise PlanningRequestError(
            "intermediate_container_capacity is required when max_intermediate_containers > 0"
        )
    if cap is not None:
        cap = int(cap)
        if cap < minimum:
            raise PlanningRequestError("intermediate container is below pipette minimum")

    ids = [lot.id for lot in lot_records]
    if set(ids) != set(request["stock_lot_ids"]):
        raise PlanningRequestError("stock_lot_ids must match submitted inventory records")

    forbidden, seen_pairs = [], set()
    for pair in request.get("forbidden_pairs", []):
        a, b = pair["lot_a"], pair["lot_bot"] if "lot_bot" in pair else pair["lot_b"]
        if a not in ids or b not in ids or a == b:
            raise PlanningRequestError(f"invalid forbidden pair: {a}, {b}")
        key = tuple(sorted((a, b)))
        if key in seen_pairs:
            raise PlanningRequestError(f"duplicate forbidden pair: {key}")
        seen_pairs.add(key)
        forbidden.append((a, b))

    orders, seen_orders = [], set()
    for order in request.get("contamination_orders", []):
        a, b = order["before_lot"], order["after_lot"]
        if a not in ids or b not in ids or a == b:
            raise PlanningRequestError(f"invalid contamination order: {a}, {b}")
        key = (a, b)
        if key in seen_orders:
            raise PlanningRequestError(f"duplicate contamination order: {key}")
        seen_orders.add(key)
        orders.append(key)

    ordered_records = sorted(
        lot_records,
        key=lambda lot: (lot.expires_at, str(lot.concentration), lot.code, lot.id),
    )
    lots: list[Lot] = []
    for rank, record in enumerate(ordered_records):
        lots.append(
            Lot(
                id=record.id,
                code=record.code,
                concentration=Decimal(str(record.concentration)),
                available_volume=max(0, int(availability.get(record.id, record.available_volume))),
                rank=rank,
                direct_only=bool(getattr(record, "direct_only", False)),
            )
        )

    return PlanSpec(
        target_min=target_min,
        target_max=target_max,
        aliquot_volume=volume,
        aliquot_count=int(request["aliquot_count"]),
        final_container_capacity=int(request["final_container_capacity"]),
        intermediate_container_capacity=cap,
        max_intermediate_containers=max_k,
        pipette_min_volume=minimum,
        pipette_increment=increment,
        scheduled_at=request["scheduled_at"],
        lots=lots,
        forbidden_pairs=forbidden,
        contamination_orders=orders,
    )


def build_spec_from_snapshot(
    request: dict[str, Any],
    lots: list[Lot],
) -> PlanSpec:
    increment = int(request["pipette_increment"])
    minimum = int(request["pipette_min_volume"])
    volume = int(request["aliquot_volume"])
    if minimum < increment or volume < minimum or volume % increment:
        raise PlanningRequestError("invalid pipette or aliquot parameters")
    if volume > int(request["final_container_capacity"]):
        raise PlanningRequestError("aliquot_volume exceeds final container capacity")
    cap = request.get("intermediate_container_capacity")
    return PlanSpec(
        target_min=decimal_value(request["target_min_concentration"]),
        target_max=decimal_value(request["target_max_concentration"]),
        aliquot_volume=volume,
        aliquot_count=int(request["aliquot_count"]),
        final_container_capacity=int(request["final_container_capacity"]),
        intermediate_container_capacity=None if cap is None else int(cap),
        max_intermediate_containers=int(request["max_intermediate_containers"]),
        pipette_min_volume=minimum,
        pipette_increment=increment,
        scheduled_at=request["scheduled_at"],
        lots=lots,
        forbidden_pairs=[(p["lot_a"], p["lot_b"]) for p in request.get("forbidden_pairs", [])],
        contamination_orders=[
            (o["before_lot"], o["after_lot"]) for o in request.get("contamination_orders", [])
        ],
    )


def solve(spec: PlanSpec, *, time_limit: float | None = None) -> dict[str, Any]:
    time_limit = time_limit if time_limit is not None else float(
        os.getenv("SOLVER_TIME_LIMIT_SECONDS", "5")
    )
    conc_values = [spec.target_min, spec.target_max] + [lot.concentration for lot in spec.lots]
    scale = concentration_scale(conc_values)
    cmin = normalize_concentration(spec.target_min, scale)
    cmax = normalize_concentration(spec.target_max, scale)
    lot_by_id = {lot.id: lot for lot in spec.lots}
    I = len(spec.lots)
    A = spec.aliquot_count
    K = spec.max_intermediate_containers
    V = spec.aliquot_volume
    inc = spec.pipette_increment
    minimum = spec.pipette_min_volume
    max_lot_volume = max([lot.available_volume for lot in spec.lots] + [V])
    max_intermediate_volume = spec.intermediate_container_capacity or max(V, inc)

    model = cp_model.CpModel()
    # x[i,0,a]: direct stock additions into final aliquot a.
    # x[i,j+1,a] is unused for j>0; intermediates are shared through f[j,a].
    x = {}
    xpos = {}
    direct_active = {}
    for i, lot in enumerate(spec.lots):
        for a in range(A):
            var = model.NewIntVar(0, max_lot_volume, f"x_{i}_0_{a}")
            model.AddModuloEquality(0, var, inc)
            x[i, 0, a] = var
            active = model.NewBoolVar(f"xactive_{i}_0_{a}")
            model.Add(var > 0).OnlyEnforceIf(active)
            model.Add(var == 0).OnlyEnforceIf(active.Not())
            model.Add(var >= minimum).OnlyEnforceIf(active)
            direct_active[i, a] = active
            xpos[i, a] = model.NewIntVar(0, max(0, I - 1), f"xpos_{i}_{a}")

    y = {}
    y_active = {}
    ypos = {}
    for i, lot in enumerate(spec.lots):
        for j in range(K):
            var = model.NewIntVar(0, max_lot_volume, f"y_{i}_{j}")
            model.AddModuloEquality(0, var, inc)
            y[i, j] = var
            active = model.NewBoolVar(f"yactive_{i}_{j}")
            model.Add(var > 0).OnlyEnforceIf(active)
            model.Add(var == 0).OnlyEnforceIf(active.Not())
            model.Add(var >= minimum).OnlyEnforceIf(active)
            y_active[i, j] = active
            ypos[i, j] = model.NewIntVar(0, max(0, I - 1), f"ypos_{i}_{j}")
            if lot.direct_only:
                model.Add(var == 0)

    W = []
    used_intermediate = []
    for j in range(K):
        w = model.NewIntVar(0, max_intermediate_volume, f"W_{j}")
        active = model.NewBoolVar(f"intermediate_{j}")
        model.AddModuloEquality(0, w, inc)
        model.Add(w > 0).OnlyEnforceIf(active)
        model.Add(w == 0).OnlyEnforceIf(active.Not())
        model.Add(w >= minimum).OnlyEnforceIf(active)
        model.Add(w == sum(y[i, j] for i in range(I)))
        W.append(w)
        used_intermediate.append(active)

    d, q, u = [], [], []
    f = {}
    for a in range(A):
        da = model.NewBoolVar(f"direct_{a}")
        qa = model.NewBoolVar(f"intermediate_fed_{a}")
        ua = model.NewBoolVar(f"unmet_{a}")
        model.Add(da + qa + ua == 1)
        d.append(da)
        q.append(qa)
        u.append(ua)
        direct_sum = sum(x[i, 0, a] for i in range(I))
        model.Add(direct_sum == V).OnlyEnforceIf(da)
        model.Add(direct_sum == 0).OnlyEnforceIf(da.Not())
        for j in range(K):
            feed = model.NewBoolVar(f"feed_{j}_{a}")
            f[j, a] = feed
        model.Add(sum(f[j, a] for j in range(K)) == 1).OnlyEnforceIf(qa)
        model.Add(sum(f[j, a] for j in range(K)) == 0).OnlyEnforceIf(qa.Not())

    # Shared homogeneous intermediate flow and quantization.
    waste = []
    container_violations = []
    for j in range(K):
        feed_count = model.NewIntVar(0, A, f"feed_count_{j}")
        model.Add(feed_count == sum(f[j, a] for a in range(A)))
        model.Add(feed_count > 0).OnlyEnforceIf(used_intermediate[j])
        model.Add(feed_count == 0).OnlyEnforceIf(used_intermediate[j].Not())
        used_volume = model.NewIntVar(0, max_intermediate_volume, f"used_{j}")
        model.Add(used_volume == V * feed_count)
        w = model.NewIntVar(0, max_intermediate_volume, f"waste_{j}")
        model.Add(w == W[j] - used_volume)
        waste.append(w)
        container_v = None
        if spec.intermediate_container_capacity:
            container_v = model.NewBoolVar(f"container_v_{j}")
            model.Add(W[j] <= spec.intermediate_container_capacity).OnlyEnforceIf(
                [used_intermediate[j], container_v.Not()]
            )
            model.Add(container_v == 0).OnlyEnforceIf(used_intermediate[j].Not())
        if container_v is not None:
            container_violations.append((container_v, j))
        for i in range(I):
            component = model.NewIntVar(0, max(V, 1), f"component_{i}_{j}")
            # V * y[i,j] == W[j] * component: all aliquots receive an integral
            # amount of every stock, and therefore every aliquot is homogeneous.
            product = model.NewIntVar(0, max_intermediate_volume * max(V, 1),
                                      f"component_product_{i}_{j}")
            model.AddMultiplicationEquality(product, [W[j], component])
            model.Add(V * y[i, j] == product).OnlyEnforceIf(used_intermediate[j])
            model.Add(component == 0).OnlyEnforceIf(used_intermediate[j].Not())

    violations: list[tuple[cp_model.IntVar | bool, str, dict[str, Any]]] = []
    violations.extend((b, "unmet_aliquot", {"aliquot_index": a}) for a, b in enumerate(u))

    virtual_waste = []
    # Inventory availability.
    inventory_violations = {}
    for i, lot in enumerate(spec.lots):
        usage = sum(y[i, j] for j in range(K)) + sum(x[i, 0, a] for a in range(A))
        v = model.NewBoolVar(f"inventory_v_{i}")
        inventory_violations[i] = v
        model.Add(usage <= lot.available_volume).OnlyEnforceIf(v.Not())
        violations.append((v, "inventory", {"lot_id": lot.id, "lot_code": lot.code}))
        if lot.virtual:
            residual = model.NewIntVar(0, lot.available_volume, f"virtual_waste_{i}")
            model.Add(residual == lot.available_volume - usage)
            virtual_waste.append(residual)

    # Concentration windows.
    for a in range(A):
        v = model.NewBoolVar(f"direct_concentration_v_{a}")
        mass = sum(normalize_concentration(lot.concentration, scale) * x[i, 0, a]
                  for i, lot in enumerate(spec.lots))
        model.Add(mass >= cmin * V).OnlyEnforceIf([d[a], v.Not()])
        model.Add(mass <= cmax * V).OnlyEnforceIf([d[a], v.Not()])
        model.Add(v == 0).OnlyEnforceIf(d[a].Not())
        violations.append((v, "concentration", {"aliquot_index": a, "route": "direct"}))

    for j in range(K):
        v = model.NewBoolVar(f"intermediate_concentration_v_{j}")
        mass = sum(normalize_concentration(lot.concentration, scale) * y[i, j]
                  for i, lot in enumerate(spec.lots))
        model.Add(mass >= cmin * W[j]).OnlyEnforceIf([used_intermediate[j], v.Not()])
        model.Add(mass <= cmax * W[j]).OnlyEnforceIf([used_intermediate[j], v.Not()])
        model.Add(v == 0).OnlyEnforceIf(used_intermediate[j].Not())
        violations.append((v, "concentration", {"intermediate_index": j, "route": "intermediate"}))

    for container_v, j in container_violations:
        violations.append((container_v, "intermediate_container_capacity", {"intermediate_index": j}))

    def lot_index(lot_id: str) -> int:
        return next(i for i, lot in enumerate(spec.lots) if lot.id == lot_id)

    forbidden_direct = {}
    forbidden_intermediate = {}
    for pair_index, (a_id, b_id) in enumerate(spec.forbidden_pairs):
        ai, bi = lot_index(a_id), lot_index(b_id)
        for aliquot in range(A):
            v = model.NewBoolVar(f"forbid_direct_{pair_index}_{aliquot}")
            model.Add(
                direct_active[ai, aliquot] + direct_active[bi, aliquot] <= 1
            ).OnlyEnforceIf(v.Not())
            model.AddBoolOr([direct_active[ai, aliquot].Not(),
                             direct_active[bi, aliquot].Not(), v])
            forbidden_direct[pair_index, aliquot] = v
            violations.append((v, "forbidden_pair", {
                "lot_a": a_id, "lot_b": b_id, "container": f"aliquot-{aliquot + 1}"
            }))
        for j in range(K):
            v = model.NewBoolVar(f"forbid_intermediate_{pair_index}_{j}")
            model.Add(y_active[ai, j] + y_active[bi, j] <= 1).OnlyEnforceIf(v.Not())
            model.AddBoolOr([y_active[ai, j].Not(), y_active[bi, j].Not(), v])
            forbidden_intermediate[pair_index, j] = v
            violations.append((v, "forbidden_pair", {
                "lot_a": a_id, "lot_b": b_id, "container": f"intermediate-{j + 1}"
            }))

    for order_index, (before_id, after_id) in enumerate(spec.contamination_orders):
        bi, ai = lot_index(before_id), lot_index(after_id)
        for aliquot in range(A):
            v = model.NewBoolVar(f"order_direct_{order_index}_{aliquot}")
            both = [direct_active[bi, aliquot], direct_active[ai, aliquot]]
            model.Add(xpos[bi, aliquot] < xpos[ai, aliquot]).OnlyEnforceIf(both + [v.Not()])
            model.AddBoolOr([direct_active[bi, aliquot].Not(), direct_active[ai, aliquot], v])
            model.Add(v <= direct_active[bi, aliquot])
            violations.append((v, "contamination_order", {
                "before_lot": before_id, "after_lot": after_id,
                "container": f"aliquot-{aliquot + 1}"
            }))
        for j in range(K):
            v = model.NewBoolVar(f"order_intermediate_{order_index}_{j}")
            both = [y_active[bi, j], y_active[ai, j]]
            model.Add(ypos[bi, j] < ypos[ai, j]).OnlyEnforceIf(both + [v.Not()])
            model.AddBoolOr([y_active[bi, j].Not(), y_active[ai, j], v])
            model.Add(v <= y_active[bi, j])
            violations.append((v, "contamination_order", {
                "before_lot": before_id, "after_lot": after_id,
                "container": f"intermediate-{j + 1}"
            }))

    unmet_count = sum(u)
    other_violation_count = sum(var for var, _, _ in violations if not isinstance(var, bool) and
                                var.Name().startswith("unmet_") is False)
    waste_total = sum(waste) + sum(virtual_waste)
    stock_transfer_count = (
        sum(direct_active.values()) + sum(y_active.values()) +
        sum(f[j, a] for j in range(K) for a in range(A))
    )
    # Deterministic batch tie-break: earlier expiry / lexicographically earlier
    # lots have smaller rank and therefore a smaller weighted-volume score.
    # Coefficients below are derived from finite upper bounds so the four
    # criteria are strictly lexicographic without overflowing into each other.
    max_total_volume = max(
        A * V,
        sum(lot.available_volume for lot in spec.lots),
        K * max_intermediate_volume,
        1,
    )
    batch_score = sum(
        (lot.rank + 1)
        * (sum(y[i, j] for j in range(K)) + sum(x[i, 0, a] for a in range(A)))
        for i, lot in enumerate(spec.lots)
    )
    max_batch_score = max(1, (I + 1) * I * max_total_volume)
    step_coefficient = max_batch_score + 1
    max_step_count = I * (A + K) + A + K
    waste_coefficient = step_coefficient * (max_step_count + 1) + 1
    max_waste = A * V + K * max_intermediate_volume + sum(
        lot.available_volume for lot in spec.lots if lot.virtual
    )
    conflict_coefficient = waste_coefficient * (max_waste + 1) + 1
    # Leaving an aliquot unmet is preferred to violating its chemistry/process
    # constraints, but never preferred to the maximum number of other conflicts.
    other_count_bound = max(1, len(violations) - A + 1)
    unmet_penalty = conflict_coefficient
    other_penalty = conflict_coefficient * (other_count_bound + 1)
    model.Minimize(
        unmet_penalty * unmet_count
        + other_penalty * other_violation_count
        + waste_coefficient * waste_total
        + step_coefficient * stock_transfer_count
        + batch_score
    )

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 20260922
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {
            "feasible": False,
            "status": "solver_failed",
            "message": "CP-SAT could not establish feasibility",
            "conflicts": [],
            "score": None,
        }

    def value(var) -> int:
        return int(solver.Value(var))

    conflicts = []
    for var, kind, evidence in violations:
        if isinstance(var, bool) or not value(var):
            continue
        conflicts.append({"type": kind, "evidence": dict(evidence)})

    # Enrich conflict witnesses after reconstruction.
    intermediates, direct_aliquots = [], []
    for j in range(K):
        if value(W[j]) == 0:
            continue
        inputs = []
        for i, lot in enumerate(spec.lots):
            volume = value(y[i, j])
            if volume:
                inputs.append({
                    "lot_id": lot.id,
                    "lot_code": lot.code,
                    "volume": volume,
                    "order": value(ypos[i, j]),
                    "rank": lot.rank,
                })
        inputs.sort(key=lambda x: (x["order"], x["rank"], x["lot_id"]))
        mass = sum(int(normalize_concentration(lot_by_id[row["lot_id"]].concentration, scale))
                   * row["volume"] for row in inputs)
        wvol = value(W[j])
        conc = _divide_int(mass, wvol, scale)
        intermediates.append({
            "id": f"intermediate-{j + 1}",
            "index": j,
            "volume": wvol,
            "waste": value(waste[j]),
            "concentration": conc,
            "inputs": inputs,
        })

    for a in range(A):
        if value(d[a]) == 0:
            direct_aliquots.append(None)
            continue
        inputs = []
        for i, lot in enumerate(spec.lots):
            volume = value(x[i, 0, a])
            if volume:
                inputs.append({
                    "lot_id": lot.id,
                    "lot_code": lot.code,
                    "volume": volume,
                    "order": value(xpos[i, a]),
                    "rank": lot.rank,
                })
        inputs.sort(key=lambda x: (x["order"], x["rank"], x["lot_id"]))
        mass = sum(int(normalize_concentration(lot_by_id[row["lot_id"]].concentration, scale))
                   * row["volume"] for row in inputs)
        direct_aliquots.append({
            "id": f"aliquot-{a + 1}",
            "index": a,
            "volume": V,
            "concentration": _divide_int(mass, V, scale),
            "inputs": inputs,
        })

    aliquot_sources: list[dict[str, Any]] = []
    for a in range(A):
        if value(u[a]):
            aliquot_sources.append({"aliquot_id": f"aliquot-{a + 1}", "route": "unmet"})
        elif value(d[a]):
            aliquot_sources.append({"aliquot_id": f"aliquot-{a + 1}", "route": "direct"})
        else:
            source = next(j for j in range(K) if value(f[j, a]))
            aliquot_sources.append({
                "aliquot_id": f"aliquot-{a + 1}",
                "route": "intermediate",
                "intermediate_id": f"intermediate-{source + 1}",
                "volume": V,
            })

    _enrich_conflicts(conflicts, intermediates, direct_aliquots, spec, scale)
    usage_by_lot = {
        lot.id: sum(value(y[i, j]) for j in range(K))
        + sum(value(x[i, 0, a]) for a in range(A))
        for i, lot in enumerate(spec.lots)
    }
    steps, graph = _build_artifacts(intermediates, direct_aliquots, aliquot_sources, spec, scale)
    feasible = value(unmet_count) == 0 and value(other_violation_count) == 0

    return {
        "feasible": feasible,
        "status": "feasible" if feasible else "conflict",
        "target_concentration": {
            "min": format(spec.target_min, "f"),
            "max": format(spec.target_max, "f"),
        },
        "aliquot_volume": V,
        "aliquot_count": A,
        "waste_volume": sum(value(w) for w in waste) + sum(value(w) for w in virtual_waste),
        "step_count": value(stock_transfer_count),
        "batch_score": value(batch_score),
        "score": {
            "waste_volume": sum(value(w) for w in waste) + sum(value(w) for w in virtual_waste),
            "step_count": value(stock_transfer_count),
            "batch_score": value(batch_score),
            "objective": int(solver.ObjectiveValue()),
        },
        "usage_by_lot": {lot_id: volume for lot_id, volume in usage_by_lot.items() if volume},
        "intermediates": intermediates,
        "aliquots": aliquot_sources,
        "transfers": steps,
        "source_chain": graph,
        "conflicts": conflicts,
        "concentration_scale": scale,
    }


def _divide_int(numerator: int, denominator: int, scale: int) -> str:
    with localcontext() as ctx:
        ctx.prec = 28
        return format(Decimal(numerator) / Decimal(denominator) / Decimal(scale), "f")


def _enrich_conflicts(conflicts, intermediates, direct_aliquots, spec, scale) -> None:
    inter_by_id = {row["id"]: row for row in intermediates}
    direct_by_id = {row["id"]: row for row in direct_aliquots if row}
    for conflict in conflicts:
        evidence = conflict["evidence"]
        container = evidence.get("container")
        row = inter_by_id.get(container) or direct_by_id.get(container)
        if row:
            evidence["actual_concentration"] = row["concentration"]
            evidence["actual_volume"] = row["volume"]
            evidence["lot_ids"] = [item["lot_id"] for item in row["inputs"]]
        if conflict["type"] == "inventory":
            # Evidence lot id is already present; add requested target witness.
            evidence["required_total_volume"] = spec.aliquot_count * spec.aliquot_volume


def _build_artifacts(intermediates, direct_aliquots, aliquot_sources, spec, scale):
    transfers: list[dict[str, Any]] = []
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    step = 1

    for lot in spec.lots:
        nodes[lot.id] = {"id": lot.id, "type": "lot", "code": lot.code,
                         "concentration": format(lot.concentration, "f")}

    for inter in intermediates:
        nodes[inter["id"]] = {
            "id": inter["id"], "type": "intermediate", "volume": inter["volume"],
            "concentration": inter["concentration"],
        }
        for row in inter["inputs"]:
            transfers.append({
                "step_no": step, "operation": "stock_to_intermediate",
                "from_lot_id": row["lot_id"], "to_container_id": inter["id"],
                "volume": row["volume"],
            })
            edges.append({"id": f"{row['lot_id']}->{inter['id']}", "source": row["lot_id"],
                          "target": inter["id"], "volume": row["volume"], "step_no": step})
            step += 1

    for aliquot in direct_aliquots:
        if not aliquot:
            continue
        nodes[aliquot["id"]] = {
            "id": aliquot["id"], "type": "aliquot", "volume": aliquot["volume"],
            "concentration": aliquot["concentration"],
        }
        for row in aliquot["inputs"]:
            transfers.append({
                "step_no": step, "operation": "stock_to_aliquot",
                "from_lot_id": row["lot_id"], "to_aliquot_id": aliquot["id"],
                "volume": row["volume"],
            })
            edges.append({"id": f"{row['lot_id']}->{aliquot['id']}", "source": row["lot_id"],
                          "target": aliquot["id"], "volume": row["volume"], "step_no": step})
            step += 1

    for source in aliquot_sources:
        if source["route"] != "intermediate":
            if source["route"] == "unmet":
                nodes[source["aliquot_id"]] = {
                    "id": source["aliquot_id"], "type": "aliquot", "volume": spec.aliquot_volume,
                    "concentration": None, "unmet": True,
                }
            continue
        aliquot_id = source["aliquot_id"]
        inter_id = source["intermediate_id"]
        inter = next(row for row in intermediates if row["id"] == inter_id)
        nodes[aliquot_id] = {
            "id": aliquot_id, "type": "aliquot", "volume": spec.aliquot_volume,
            "concentration": inter["concentration"],
        }
        transfers.append({
            "step_no": step, "operation": "intermediate_to_aliquot",
            "from_container_id": inter_id, "to_aliquot_id": aliquot_id,
            "volume": spec.aliquot_volume,
        })
        edges.append({"id": f"{inter_id}->{aliquot_id}", "source": inter_id,
                      "target": aliquot_id, "volume": spec.aliquot_volume, "step_no": step})
        step += 1

    return transfers, {"nodes": list(nodes.values()), "edges": edges}
