import hashlib
import json
import os
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app import models
from app.solver import (
    Lot,
    build_spec_from_snapshot,
    decimal_value,
    solve,
    validate_and_build_spec,
)


class ServiceError(Exception):
    def __init__(self, status_code: int, code: str, message: str, details=None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


def stable_hash(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str, ensure_ascii=False).encode()
    ).hexdigest()


def serialize_request(request: dict) -> dict:
    return json.loads(json.dumps(request, default=json_default))


def json_default(value):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    raise TypeError(f"unsupported type {type(value)}")


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def lot_event_dict(event: models.LotEvent) -> dict:
    created_at = event.created_at
    return {
        "id": event.id,
        "lot_id": event.lot_id,
        "event_type": event.event_type,
        "qc_record_id": event.qc_record_id,
        "reason": event.reason,
        "version_after": event.version_after,
        "created_at": created_at.isoformat() if isinstance(created_at, datetime) else None,
    }


def _decimal_str(value) -> str:
    return format(Decimal(str(value)).normalize(), "f")


def qc_record_dict(record: models.QcRecord) -> dict:
    created_at = record.created_at
    return {
        "id": record.id,
        "lot_id": record.lot_id,
        "item": record.item,
        "value": _decimal_str(record.value),
        "unit": record.unit,
        "lower_limit": _decimal_str(record.lower_limit),
        "upper_limit": _decimal_str(record.upper_limit),
        "rule_version": record.rule_version,
        "in_range": record.in_range,
        "created_at": created_at.isoformat() if isinstance(created_at, datetime) else None,
    }


def _latest_lot_event(db: Session, lot_id: str) -> models.LotEvent | None:
    return db.scalars(
        select(models.LotEvent)
        .where(models.LotEvent.lot_id == lot_id)
        .order_by(models.LotEvent.version_after.desc())
        .limit(1)
    ).first()


def blocking_lot_evidence(db: Session, lots) -> list[dict]:
    """Per-lot quality blocking evidence: current status plus its latest event."""
    evidence = []
    for lot in lots:
        event = _latest_lot_event(db, lot.id)
        evidence.append({
            "lot_id": lot.id,
            "lot_code": lot.code,
            "quality_status": lot.quality_status,
            "latest_event": lot_event_dict(event) if event else None,
        })
    return evidence


def ensure_lots_released(db: Session, lots, status_code: int) -> None:
    blocked = [lot for lot in lots if lot.quality_status != "released"]
    if blocked:
        raise ServiceError(
            status_code,
            "lot_not_released",
            "one or more lots are not in released quality status",
            {"conflicts": [{"type": "quality_release",
                            "evidence": blocking_lot_evidence(db, blocked)}]},
        )


def eligible_lots(db: Session, request: dict, scheduled_at: datetime, replace_revision_id: str | None = None):
    ids = request["stock_lot_ids"]
    lots = db.scalars(
        select(models.StockLot).where(models.StockLot.id.in_(ids)).with_for_update()
    ).all()
    if len({lot.id for lot in lots}) != len(set(ids)):
        raise ServiceError(404, "lot_not_found", "one or more stock lots do not exist")
    # The user explicitly lists eligible lots.  Expiry is a hard precondition;
    # it is not relaxed because it is supplied as temporal evidence, not a mixing
    # equation.
    expired = [
        {"lot_id": lot.id, "lot_code": lot.code, "expires_at": lot.expires_at.isoformat()}
        for lot in lots if ensure_utc(lot.expires_at) <= ensure_utc(scheduled_at)
    ]
    if expired:
        raise ServiceError(
            422,
            "expired_lot",
            "one or more lots expire at or before scheduled_at",
            {"conflicts": [{"type": "expiry", "evidence": expired}]},
        )
    # Plans and new revisions may only reference lots that passed QC release.
    ensure_lots_released(db, lots, 422)
    replaceable_by_lot = {lot_id: 0 for lot_id in ids}
    if replace_revision_id:
        replaceable_rows = db.scalars(
            select(models.Reservation).where(
                models.Reservation.revision_id == replace_revision_id,
                models.Reservation.status == "active",
                models.Reservation.lot_id.in_(ids),
            )
        ).all()
        for row in replaceable_rows:
            replaceable_by_lot[row.lot_id] += row.volume
    availability = {
        lot.id: lot.available_volume - lot.reserved_volume + replaceable_by_lot.get(lot.id, 0)
        for lot in lots
    }
    return lots, availability


def solve_revision(db: Session, request: dict, replace_revision_id: str | None = None) -> dict:
    scheduled_at = datetime.fromisoformat(request["scheduled_at"])
    lots, availability = eligible_lots(db, request, scheduled_at, replace_revision_id)
    spec = validate_and_build_spec(request, availability, lots)
    return solve(spec)


def create_plan_with_revision(db: Session, request: dict) -> tuple[models.Plan, models.PlanRevision]:
    solution = solve_revision(db, request)
    plan = models.Plan(name=request["name"])
    db.add(plan)
    db.flush()
    revision = models.PlanRevision(
        plan_id=plan.id,
        revision_number=1,
        request_json=serialize_request(request),
        solution_json=solution,
        feasible=bool(solution["feasible"]),
        status="draft",
    )
    db.add(revision)
    db.flush()
    plan.current_revision_id = revision.id
    return plan, revision


def create_next_revision(
    db: Session, plan: models.Plan, request: dict | None
) -> models.PlanRevision:
    previous = db.get(models.PlanRevision, plan.current_revision_id) if plan.current_revision_id else None
    if previous is None:
        raise ServiceError(404, "revision_not_found", "plan has no current revision")
    if request is None:
        request = deepcopy(previous.request_json)
    else:
        request["name"] = plan.name
    replace_id = previous.id if previous.status == "committed" else None
    solution = solve_revision(db, request, replace_id)
    revision = models.PlanRevision(
        plan_id=plan.plan_id if hasattr(plan, "plan_id") else plan.id,
        revision_number=previous.revision_number + 1,
        request_json=serialize_request(request),
        solution_json=solution,
        feasible=bool(solution["feasible"]),
        status="draft",
        supersedes_id=previous.id,
    )
    db.add(revision)
    db.flush()
    plan.current_revision_id = revision.id
    return revision


# How long a concurrent request with the same key waits for the first one to
# finish before returning 409 itself.
IDEMPOTENT_LOCK_TIMEOUT_SECONDS = float(os.getenv("IDEMPOTENT_LOCK_TIMEOUT_SECONDS", "30"))
# An in_progress row older than this is treated as a crashed predecessor; its
# business transaction (if any) is already gone, so the key can be retaken.
IDEMPOTENT_STALE_TIMEOUT_SECONDS = float(os.getenv("IDEMPOTENT_STALE_TIMEOUT_SECONDS", "120"))


class IdempotentInProgress(Exception):
    """Raised when another request with the same key is still running."""


class IdempotentHandle:
    """Lifecycle handle for a single idempotent request.

    ``replay`` is a finished response to return verbatim; when ``None`` the
    caller owns the key and must either call ``complete`` or roll back.
    """

    def __init__(self, db: Session, record: models.IdempotentOperation | None,
                 replay: tuple[int, dict] | None):
        self._db = db
        self.record = record
        self.replay = replay

    @property
    def owns_operation(self) -> bool:
        return self.replay is None

    def complete(self, status_code: int, response: dict) -> None:
        """Attach the final response to the idempotency row.

        The row is only flushed, never committed here: the caller performs the
        single commit that persists business state and the replay record
        atomically.
        """
        assert self.record is not None and self.replay is None
        self.record.state = "completed"
        self.record.status_code = status_code
        self.record.response_json = response
        self.record.completed_at = datetime.now(timezone.utc)
        self._db.flush()


def _is_postgres(db: Session) -> bool:
    return db.get_bind().dialect.name == "postgresql"


def _lock_id(key: str) -> int:
    """Map an arbitrary key to a signed 64-bit integer for pg_advisory_xact_lock."""
    digest = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big")
    if digest >= 2**63:
        digest -= 2**64
    return digest


def _replay_row(row: models.IdempotentOperation) -> tuple[int, dict]:
    return row.status_code, row.response_json


@contextmanager
def idempotent_operation(db: Session, key: str | None, operation_type: str, request: dict):
    """Serialize a state-changing operation on an ``Idempotency-Key``.

    On PostgreSQL the key is guarded by a transaction-scoped advisory lock so
    that:
      * the first request performs the business change once and commits the
        outcome (status code + full response) together with the change;
      * a same-key/same-body request waits for it and then replays the exact
        stored response;
      * a same-key/different-body request gets ``409 idempotency_conflict``;
      * a request that fails or crashes rolls back together with its
        in_progress marker, so no replayable placeholder is ever persisted.

    Yields an :class:`IdempotentHandle`.
    """
    if not key:
        yield IdempotentHandle(db, None, None)
        return

    request_hash = stable_hash(request)

    def _raise_conflict() -> None:
        raise ServiceError(409, "idempotency_conflict",
                           "idempotency key was used for a different request")

    # Fast path without taking the advisory lock: read what is already there.
    existing = db.get(models.IdempotentOperation, key)
    if existing is not None and (
        existing.operation_type != operation_type
        or existing.request_hash != request_hash
    ):
        # The key is bound to a different operation/body, finished or not.
        _raise_conflict()
    if existing is not None and existing.state == "completed":
        yield IdempotentHandle(db, existing, _replay_row(existing))
        return

    if _is_postgres(db):
        # Serialize every non-completed observation of this key through a
        # transaction-scoped advisory lock.  The lock is released by COMMIT or
        # ROLLBACK, including when the worker process is killed.
        if db.in_transaction():
            db.rollback()
        timeout_ms = max(1, int(IDEMPOTENT_LOCK_TIMEOUT_SECONDS * 1000))
        db.execute(text("SET LOCAL lock_timeout = :ms"), {"ms": timeout_ms})
        try:
            db.execute(
                text("SELECT pg_advisory_xact_lock(:id)"), {"id": _lock_id(key)}
            )
        except Exception as exc:  # lock_timeout / deadlock_detected etc.
            db.rollback()
            raise IdempotentInProgress(str(exc)) from exc
        # Re-read under the lock: the holder may have completed or crashed.
        existing = db.get(models.IdempotentOperation, key, with_for_update=True)

    if existing is not None:
        if existing.operation_type != operation_type or existing.request_hash != request_hash:
            _raise_conflict()
        if existing.state == "completed":
            yield IdempotentHandle(db, existing, _replay_row(existing))
            return
        # state == in_progress
        age = None
        if existing.locked_at is not None:
            locked_at = existing.locked_at
            if locked_at.tzinfo is None:
                locked_at = locked_at.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - locked_at.astimezone(timezone.utc)).total_seconds()
        if age is None or age < IDEMPOTENT_STALE_TIMEOUT_SECONDS:
            # On PostgreSQL the advisory lock should prevent reaching this
            # branch; it mainly covers lock-free dialects (SQLite) and
            # lock_timeout.  The request may retry shortly.
            db.rollback()
            raise IdempotentInProgress("another request with this key is in progress")
        # Predecessor crashed and never committed anything: take over its key.
        existing.operation_type = operation_type
        existing.request_hash = request_hash
        existing.state = "in_progress"
        existing.status_code = None
        existing.response_json = None
        existing.locked_at = datetime.now(timezone.utc)
        existing.completed_at = None
        record = existing
    else:
        record = models.IdempotentOperation(
            idempotency_key=key,
            operation_type=operation_type,
            request_hash=request_hash,
            state="in_progress",
            status_code=None,
            response_json=None,
            locked_at=datetime.now(timezone.utc),
        )
        db.add(record)
        # Publish the in_progress marker and (on PostgreSQL) hold the advisory
        # lock across the business work.  No caller-visible side effect exists
        # yet: the row is uncommitted and therefore invisible to other sessions.
        db.flush()

    try:
        yield IdempotentHandle(db, record, None)
    except Exception:
        # Rolling back undoes the in_progress marker together with any partial
        # business change; nothing replayable survives a failure.
        db.rollback()
        raise


def commit_revision(db: Session, revision_id: str, payload: dict) -> dict:
    revision = db.scalar(
        select(models.PlanRevision)
        .where(models.PlanRevision.id == revision_id)
        .with_for_update()
    )
    if revision is None:
        raise ServiceError(404, "revision_not_found", "revision does not exist")
    if revision.status not in ("draft", "committed"):
        raise ServiceError(409, "revision_not_committable",
                           f"revision status {revision.status} cannot be committed")
    if payload.get("force", False):
        raise ServiceError(400, "force_not_allowed", "force commit is not supported")
    if not revision.solution_json.get("feasible"):
        raise ServiceError(
            409,
            "infeasible_revision",
            "revision has minimal relaxed conflicts and cannot reserve inventory",
            {"conflicts": revision.solution_json.get("conflicts", [])},
        )
    if revision.status == "committed":
        # Idempotent replay of the same revision, without changing reservations.
        reservations = db.scalars(
            select(models.Reservation).where(models.Reservation.revision_id == revision.id,
                                             models.Reservation.status == "active")
        ).all()
        lots = {lot.id: lot for lot in db.scalars(
            select(models.StockLot).where(models.StockLot.id.in_(
                [res.lot_id for res in reservations]
            ))
        ).all()}
        return {
            "revision_id": revision.id,
            "status": "committed",
            "reservations": {res.lot_id: res.volume for res in reservations},
            "lot_versions": {lot_id: lots[lot_id].version for lot_id in lots},
        }

    if int(payload["expected_revision_version"]) != 1:
        raise ServiceError(409, "revision_version_conflict", "unexpected revision version")

    usage = revision.solution_json["usage_by_lot"]
    lot_ids = list(usage)
    lots = {lot.id: lot for lot in db.scalars(
        select(models.StockLot).where(models.StockLot.id.in_(lot_ids)).with_for_update()
    ).all()}
    if set(lots) != set(lot_ids):
        raise ServiceError(409, "lot_not_found", "one or more planned lots disappeared")

    # Re-validate quality release at commit time: a lot quarantined or recalled
    # after the revision was drafted must block the reservation.
    ensure_lots_released(db, list(lots.values()), 409)

    expected_versions = payload.get("expected_lot_versions", {})
    for lot_id, expected in expected_versions.items():
        if lot_id not in lots:
            raise ServiceError(404, "lot_not_found", f"lot {lot_id} is not part of this revision")
        if lots[lot_id].version != int(expected):
            raise ServiceError(409, "lot_version_conflict",
                               f"lot {lot_id} changed", {"current_version": lots[lot_id].version})

    plan = db.get(models.Plan, revision.plan_id)
    old_revision = db.get(models.PlanRevision, plan.current_revision_id)
    old_reservations = []
    if old_revision and old_revision.id != revision.id and old_revision.status == "committed":
        old_reservations = db.scalars(
            select(models.Reservation)
            .where(models.Reservation.revision_id == old_revision.id,
                   models.Reservation.status == "active")
            .with_for_update()
        ).all()
        old_lot_ids = {res.lot_id for res in old_reservations}
        old_lots = {lot.id: lot for lot in db.scalars(
            select(models.StockLot).where(models.StockLot.id.in_(old_lot_ids)).with_for_update()
        ).all()}
        # Release old reservation first inside the same row-level transaction.
        for res in old_reservations:
            old_lots[res.lot_id].reserved_volume -= res.volume
            res.status = "superseded"
        old_revision.status = "superseded"

    for lot_id, volume in usage.items():
        lot = lots[lot_id]
        if lot.reserved_volume + volume > lot.available_volume:
            # Should be impossible after releasing the old revision, but keeps CAS strict.
            raise ServiceError(409, "inventory_cas_conflict",
                               f"insufficient unreserved volume for lot {lot_id}")
        lot.reserved_volume += volume
        lot.version += 1
        db.add(models.Reservation(revision_id=revision.id, lot_id=lot_id, volume=volume))

    revision.status = "committed"
    plan.current_revision_id = revision.id
    db.flush()
    return {
        "revision_id": revision.id,
        "status": "committed",
        "reservations": {lot_id: int(volume) for lot_id, volume in usage.items()},
        "lot_versions": {lot_id: lot.version for lot_id, lot in lots.items()},
    }


def register_qc_record(
    db: Session, lot_id: str, payload: dict
) -> tuple[models.QcRecord, models.LotEvent, models.StockLot]:
    """Append an immutable QC record and transition the lot in one transaction.

    The lot row is locked and the expected_version CAS is checked before the
    record and the released/quarantined event are appended atomically.
    """
    lot = db.scalar(
        select(models.StockLot).where(models.StockLot.id == lot_id).with_for_update()
    )
    if lot is None:
        raise ServiceError(404, "lot_not_found", "stock lot does not exist")
    if lot.version != int(payload["expected_version"]):
        raise ServiceError(409, "lot_version_conflict", f"lot {lot_id} changed",
                           {"current_version": lot.version})
    if lot.quality_status != "pending":
        raise ServiceError(
            409,
            "lot_not_testable",
            f"lot quality status {lot.quality_status} cannot accept new qc records",
            {"quality_status": lot.quality_status},
        )
    value = Decimal(str(payload["value"]))
    lower = Decimal(str(payload["lower_limit"]))
    upper = Decimal(str(payload["upper_limit"]))
    if lower > upper:
        raise ServiceError(422, "invalid_limits", "lower_limit must be <= upper_limit")
    in_range = lower <= value <= upper
    record = models.QcRecord(
        lot_id=lot.id,
        item=payload["item"],
        value=value,
        unit=payload["unit"],
        lower_limit=lower,
        upper_limit=upper,
        rule_version=payload["rule_version"],
        in_range=in_range,
    )
    db.add(record)
    db.flush()
    lot.quality_status = "released" if in_range else "quarantined"
    lot.version += 1
    event = models.LotEvent(
        lot_id=lot.id,
        event_type=lot.quality_status,
        qc_record_id=record.id,
        version_after=lot.version,
    )
    db.add(event)
    db.flush()
    return record, event, lot


def _aliquot_sort_key(node_id: str):
    try:
        return (0, int(str(node_id).rsplit("-", 1)[1]))
    except (ValueError, IndexError):
        return (1, str(node_id))


def _recall_reachability(solution: dict, lot_id: str) -> tuple[list[str], list[dict]]:
    """Aliquot nodes and source-chain edges reachable from the recalled lot."""
    chain = solution.get("source_chain") or {}
    edges = chain.get("edges", [])
    node_types = {node["id"]: node.get("type") for node in chain.get("nodes", [])}
    adjacency: dict[str, list[dict]] = {}
    for edge in edges:
        adjacency.setdefault(edge["source"], []).append(edge)
    affected_edges: list[dict] = []
    seen = {lot_id}
    queue = [lot_id]
    while queue:
        node = queue.pop(0)
        for edge in adjacency.get(node, []):
            affected_edges.append(edge)
            target = edge["target"]
            if target not in seen:
                seen.add(target)
                queue.append(target)
    aliquots = sorted(
        (node for node in seen if node_types.get(node) == "aliquot"),
        key=_aliquot_sort_key,
    )
    return aliquots, affected_edges


def recall_lot(
    db: Session, lot_id: str, payload: dict
) -> tuple[models.StockLot, models.LotEvent, list[dict]]:
    """Recall a released lot and freeze unexecuted reservations atomically.

    Every committed revision holding active/deviation reservations on the lot
    is compromised: all of its unexecuted reservations are frozen in the same
    transaction, and the affected plans, aliquots and source edges are
    reported for traceability.
    """
    lot = db.scalar(
        select(models.StockLot).where(models.StockLot.id == lot_id).with_for_update()
    )
    if lot is None:
        raise ServiceError(404, "lot_not_found", "stock lot does not exist")
    if lot.version != int(payload["expected_version"]):
        raise ServiceError(409, "lot_version_conflict", f"lot {lot_id} changed",
                           {"current_version": lot.version})
    if lot.quality_status != "released":
        raise ServiceError(
            409,
            "lot_not_recallable",
            f"only released lots can be recalled (current: {lot.quality_status})",
            {"quality_status": lot.quality_status},
        )
    lot.quality_status = "recalled"
    lot.version += 1
    event = models.LotEvent(
        lot_id=lot.id,
        event_type="recalled",
        reason=payload.get("reason"),
        version_after=lot.version,
    )
    db.add(event)

    lot_reservations = db.scalars(
        select(models.Reservation)
        .where(models.Reservation.lot_id == lot.id,
               models.Reservation.status.in_(["active", "deviation"]))
        .with_for_update()
    ).all()
    revision_ids = sorted({res.revision_id for res in lot_reservations})
    revisions = {}
    if revision_ids:
        revisions = {rev.id: rev for rev in db.scalars(
            select(models.PlanRevision)
            .where(models.PlanRevision.id.in_(revision_ids),
                   models.PlanRevision.status == "committed")
            .with_for_update()
        ).all()}

    affected = []
    for revision_id in revision_ids:
        revision = revisions.get(revision_id)
        if revision is None:
            continue
        # Freeze every unexecuted reservation of the compromised revision.
        rev_reservations = db.scalars(
            select(models.Reservation)
            .where(models.Reservation.revision_id == revision.id,
                   models.Reservation.status.in_(["active", "deviation"]))
            .with_for_update()
        ).all()
        frozen_ids = []
        frozen_volume = 0
        for res in rev_reservations:
            res.status = "frozen"
            frozen_ids.append(res.id)
            frozen_volume += res.volume
        aliquot_ids, edges = _recall_reachability(revision.solution_json, lot.id)
        plan = db.get(models.Plan, revision.plan_id)
        affected.append({
            "plan_id": plan.id,
            "plan_name": plan.name,
            "revision_id": revision.id,
            "revision_number": revision.revision_number,
            "frozen_volume": frozen_volume,
            "frozen_reservation_ids": sorted(frozen_ids),
            "affected_aliquot_ids": aliquot_ids,
            "affected_edges": edges,
        })
    db.flush()
    return lot, event, affected


def _find_intermediate(revision: models.PlanRevision, intermediate_id: str):
    for row in revision.solution_json.get("intermediates", []):
        if row["id"] == intermediate_id:
            return row
    return None


def _current_state(db: Session, plan_id: str):
    state = db.scalars(
        select(models.ExecutionState).where(models.ExecutionState.plan_id == plan_id)
    ).one_or_none()
    return state


def _state_plan_and_revision(db: Session, revision_id: str):
    revision = db.get(models.PlanRevision, revision_id)
    if revision is None:
        raise ServiceError(404, "revision_not_found", "revision does not exist")
    if revision.status != "committed":
        raise ServiceError(409, "revision_not_committed", "only committed revisions can execute")
    plan = db.get(models.Plan, revision.plan_id)
    if plan.current_revision_id != revision.id:
        raise ServiceError(409, "revision_not_current", "deviation must be recorded on current revision")
    return plan, revision


def _lot_snapshot_for_deviation(db: Session, revision: models.PlanRevision, measured: dict,
                                affected_aliquots: set[str], state=None,
                                measured_id: str | None = None):
    """Construct a replacement solver inventory.

    Existing measured intermediate is offered as direct-only stock for its
    pending affected aliquots; unused external stock excludes amounts that
    remain consumed by unaffected parts of the committed plan.
    """
    solution = revision.solution_json
    request = deepcopy(revision.request_json)
    state_json = state.state_json if state is not None else {}
    current_sources = {
        item["aliquot_id"]: item for item in state_json.get("active_sources", [])
    }
    # Validate that every current aliquot source is reconstructible from the
    # committed plan and prior deviation graph.  Actual replacement stock is
    # added below only from currently unreserved inventory.
    for aliquot in solution.get("aliquots", []):
        aliquot_id = aliquot["aliquot_id"]
        current = current_sources.get(aliquot_id, aliquot)
        if current.get("route") not in ("intermediate", "direct", "unmet"):
            raise ServiceError(500, "invalid_execution_state",
                               f"unknown source route for {aliquot_id}")

    db_lots = db.scalars(select(models.StockLot)).all()
    db_by_id = {lot.id: lot for lot in db_lots}
    active_reservations = db.scalars(
        select(models.Reservation).join(models.PlanRevision).where(
            models.Reservation.status.in_(["active", "deviation"]),
            models.PlanRevision.plan_id == revision.plan_id,
        )
    ).all()
    reserved_for_plan = {lot_id: 0 for lot_id in solution["usage_by_lot"]}
    for res in active_reservations:
        reserved_for_plan[res.lot_id] = reserved_for_plan.get(res.lot_id, 0) + res.volume

    lots = []
    allowed = []
    for lot_id in revision.request_json["stock_lot_ids"]:
        record = db_by_id[lot_id]
        original_used = solution["usage_by_lot"].get(lot_id, 0)
        # The committed intermediate's raw inputs are already represented by the
        # measured virtual vessel.  They must not be added back as free stock;
        # additional work can only consume inventory outside all active
        # reservations (including prior deviation reservations).
        available = max(0, record.available_volume - reserved_for_plan.get(lot_id, original_used))
        if available > 0:
            lots.append(Lot(
                id=lot_id,
                code=record.code,
                concentration=Decimal(str(record.concentration)),
                available_volume=available,
                rank=900000 + len(lots),
                direct_only=False,
            ))
            allowed.append(lot_id)

    if measured["measured_volume"] > 0 and measured.get("concentration_in_range", True):
        virtual_id = measured_id or f"measured::{measured['intermediate_id']}"
        lots.insert(0, Lot(
            id=virtual_id,
            code=f"measured {measured['intermediate_id']}",
            concentration=decimal_value(measured["measured_concentration"]),
            available_volume=measured["measured_volume"],
            rank=0,
            direct_only=True,
            virtual=True,
        ))
        allowed.insert(0, virtual_id)

    # Re-solve only affected aliquots, no further intermediate mixing: measured
    # vessel is direct-only and new stock may be direct-mixed into final vials.
    request["aliquot_count"] = len(affected_aliquots)
    request["max_intermediate_containers"] = 0
    request["intermediate_container_capacity"] = None
    request["stock_lot_ids"] = allowed
    request["forbidden_pairs"] = [
        pair for pair in request.get("forbidden_pairs", [])
        if pair["lot_a"] in allowed and pair["lot_b"] in allowed
    ]
    request["contamination_orders"] = [
        order for order in request.get("contamination_orders", [])
        if order["before_lot"] in allowed and order["after_lot"] in allowed
    ]
    spec = build_spec_from_snapshot(request, lots)
    replacement = solve(spec, time_limit=float(os.getenv("SOLVER_TIME_LIMIT_SECONDS", "5")))
    # Renumber replacement aliquot ids to the original affected ids.
    affected_ids = sorted(affected_aliquots, key=lambda x: int(x.rsplit("-", 1)[1]))
    _remap_replacement(replacement, measured["intermediate_id"], affected_ids)
    return replacement, request



def _remap_replacement(solution, measured_id, aliquot_ids):
    old_to_new = {f"aliquot-{i + 1}": aliquot_id for i, aliquot_id in enumerate(aliquot_ids)}
    solution["aliquots"] = []
    for transfer in solution.get("transfers", []):
        if transfer.get("to_aliquot_id") in old_to_new:
            transfer["to_aliquot_id"] = old_to_new[transfer["to_aliquot_id"]]
    for edge in solution.get("source_chain", {}).get("edges", []):
        if edge["target"] in old_to_new:
            edge["target"] = old_to_new[edge["target"]]
            edge["id"] = f"{edge['source']}->{edge['target']}"
    nodes = []
    for node in solution.get("source_chain", {}).get("nodes", []):
        if node["id"] in old_to_new:
            node["id"] = old_to_new[node["id"]]
        nodes.append(node)
    solution["source_chain"]["nodes"] = nodes
    # Rebuild aliquot source records from graph edges.
    for aliquot_id in aliquot_ids:
        sources = [e for e in solution["source_chain"]["edges"] if e["target"] == aliquot_id]
        solution["aliquots"].append({
            "aliquot_id": aliquot_id,
            "route": "direct" if sources else "unmet",
            "volume": solution["aliquot_volume"],
        })


def record_deviation(db: Session, revision_id: str, payload: dict) -> tuple[models.Deviation, dict]:
    plan, revision = _state_plan_and_revision(db, revision_id)
    previous_state = _current_state(db, plan.id)
    intermediate = _find_intermediate(revision, payload["intermediate_id"])
    if intermediate is None:
        raise ServiceError(404, "intermediate_not_found",
                           "intermediate is not part of this revision")

    affected_aliquots = {
        row["aliquot_id"] for row in revision.solution_json["aliquots"]
        if row.get("intermediate_id") == intermediate["id"]
    }
    if not affected_aliquots:
        raise ServiceError(422, "intermediate_unused",
                           "the measured intermediate has no downstream aliquots")

    measured_concentration = payload.get("measured_concentration") or intermediate["concentration"]
    cmin = Decimal(revision.solution_json["target_concentration"]["min"])
    cmax = Decimal(revision.solution_json["target_concentration"]["max"])
    cval = Decimal(str(measured_concentration))
    in_range = cmin <= cval <= cmax
    measured = {
        "intermediate_id": intermediate["id"],
        "measured_volume": int(payload["measured_volume"]),
        "planned_volume": int(intermediate["volume"]),
        "measured_concentration": format(cval, "f"),
        "concentration_in_range": in_range,
    }

    prior_deviation_count = 0
    if previous_state is not None:
        prior_deviation_count = len(previous_state.state_json.get("deviations", []))
    measured_virtual_id = (
        f"measured::{intermediate['id']}"
        if prior_deviation_count == 0
        else f"measured::{intermediate['id']}#{prior_deviation_count + 1}"
    )
    replacement, replacement_request = _lot_snapshot_for_deviation(
        db, revision, measured, affected_aliquots, previous_state, measured_virtual_id
    )

    base_chain = None
    if previous_state is not None:
        base_chain = deepcopy(previous_state.state_json.get("source_chain"))
    result = build_deviation_result(
        revision, intermediate, measured, replacement, affected_aliquots,
        base_chain, measured_virtual_id
    )
    deviation = models.Deviation(
        plan_id=plan.id,
        revision_id=revision.id,
        intermediate_id=intermediate["id"],
        measured_volume=measured["measured_volume"],
        measured_concentration=cval,
        note=payload.get("note"),
        result_json=result,
    )
    db.add(deviation)

    state = _current_state(db, plan.id)
    if state is None:
        state = models.ExecutionState(plan_id=plan.id, revision_id=revision.id, state_json={
            "deviations": [], "active_sources": {},
            "source_chain": revision.solution_json["source_chain"]
        })
        db.add(state)
        db.flush()
    state.state_json = result["current_execution"]
    state.state_json["deviations"] = state.state_json.get("deviations", [])
    state.state_json["deviations"].append({
        "intermediate_id": intermediate["id"],
        "measured_volume": measured["measured_volume"],
        "measured_concentration": measured["measured_concentration"],
        "replacement_feasible": replacement["feasible"],
    })
    state.version += 1

    if replacement["feasible"]:
        # Additional reservations for new stock; measured virtual stock consumes
        # existing plan inventory and therefore is not reserved again.
        new_lot_ids = [lot_id for lot_id in replacement["usage_by_lot"]
                       if not lot_id.startswith("measured::")]
        new_lots = {lot.id: lot for lot in db.scalars(
            select(models.StockLot)
            .where(models.StockLot.id.in_(new_lot_ids))
            .with_for_update()
        ).all()}
        for lot_id, volume in replacement["usage_by_lot"].items():
            if lot_id.startswith("measured::"):
                continue
            lot = new_lots[lot_id]
            if lot is None or lot.reserved_volume + volume > lot.available_volume:
                raise ServiceError(409, "inventory_cas_conflict",
                                   f"insufficient stock while reserving replacement {lot_id}")
            lot.reserved_volume += volume
            lot.version += 1
            db.add(models.Reservation(revision_id=revision.id, lot_id=lot_id,
                                      volume=volume, status="deviation"))

    db.flush()
    return deviation, result


def build_deviation_result(revision, intermediate, measured, replacement, affected_aliquots,
                           base_chain=None, measured_virtual_id=None):
    original_chain = deepcopy(base_chain or revision.solution_json["source_chain"])
    measured_virtual_id = measured_virtual_id or f"measured::{intermediate['id']}"
    # Mark active edges into the affected aliquots as superseded. Historical
    # measurement/evidence edges remain in the immutable source chain.
    for edge in original_chain["edges"]:
        if edge["target"] in affected_aliquots and edge.get("status") not in ("superseded", "actual"):
            edge["status"] = "superseded"
    measured_source_id = intermediate["id"]
    if base_chain:
        prior_measured = [
            node["id"] for node in base_chain.get("nodes", [])
            if node.get("type") == "measured_intermediate"
            and node.get("origin_intermediate_id") == intermediate["id"]
        ]
        if prior_measured:
            measured_source_id = prior_measured[-1]
    measured_node = {
        "id": measured_virtual_id,
        "type": "measured_intermediate",
        "origin_intermediate_id": intermediate["id"],
        "volume": measured["measured_volume"],
        "concentration": measured["measured_concentration"],
        "concentration_in_range": measured["concentration_in_range"],
    }
    original_chain["nodes"].append(measured_node)
    original_chain["edges"].append({
        "id": f"{measured_source_id}->{measured_virtual_id}",
        "source": measured_source_id,
        "target": measured_virtual_id,
        "volume": measured["measured_volume"],
        "operation": "measurement",
        "status": "actual",
    })
    for edge in replacement.get("source_chain", {}).get("edges", []):
        edge = deepcopy(edge)
        edge.setdefault("id", f"{edge['source']}->{edge['target']}")
        original_chain["edges"].append(edge)
    node_ids = {node["id"] for node in original_chain["nodes"]}
    for node in replacement.get("source_chain", {}).get("nodes", []):
        if node["id"] not in node_ids and not node["id"].startswith("measured::"):
            original_chain["nodes"].append(node)

    return {
        "feasible": replacement["feasible"],
        "measured": measured,
        "affected_aliquot_ids": sorted(affected_aliquots, key=lambda x: int(x.rsplit("-", 1)[1])),
        "replacement_plan": replacement,
        "source_chain": original_chain,
        "current_execution": {
            "revision_id": revision.id,
            "latest_deviation_intermediate_id": intermediate["id"],
            "affected_aliquot_ids": sorted(affected_aliquots, key=lambda x: int(x.rsplit("-", 1)[1])),
            "active_sources": replacement.get("aliquots", []),
            "source_chain": original_chain,
        },
        "conflicts": replacement.get("conflicts", []),
    }
