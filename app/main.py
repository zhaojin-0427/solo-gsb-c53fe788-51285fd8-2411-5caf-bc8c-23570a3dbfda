from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import models, schemas, services
from app.database import get_db, init_db
from app.solver import PlanningRequestError


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Laboratory Reagent Planning and Deviation Trace API",
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(services.ServiceError)
async def service_error_handler(request: Request, exc: services.ServiceError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
    )


@app.exception_handler(PlanningRequestError)
async def planning_request_error_handler(request: Request, exc: PlanningRequestError):
    return JSONResponse(
        status_code=422,
        content={"error": {"code": "invalid_plan_request", "message": str(exc), "details": {}}},
    )


@app.exception_handler(services.IdempotentInProgress)
async def idempotent_in_progress_handler(request: Request, exc: services.IdempotentInProgress):
    return JSONResponse(
        status_code=409,
        headers={"Retry-After": "2"},
        content={
            "error": {
                "code": "idempotency_in_progress",
                "message": "a request with this idempotency key is still being processed; "
                           "retry with the same key",
                "details": {},
            }
        },
    )


@app.get("/health", response_model=schemas.HealthOut)
def health():
    return {"status": "ok"}


def _stock_out(lot: models.StockLot) -> schemas.StockLotOut:
    return schemas.StockLotOut(
        id=lot.id,
        code=lot.code,
        concentration=format(Decimal(str(lot.concentration)), "f"),
        available_volume=lot.available_volume,
        reserved_volume=lot.reserved_volume,
        expires_at=lot.expires_at,
        direct_only=lot.direct_only,
        quality_status=lot.quality_status,
        version=lot.version,
    )


@app.post("/stock-lots", response_model=schemas.StockLotOut, status_code=201)
def create_stock_lot(payload: schemas.StockLotCreate, db: Session = Depends(get_db)):
    expires_at = payload.expires_at
    if expires_at.tzinfo is None:
        raise HTTPException(status_code=422, detail="expires_at must be timezone aware")
    if expires_at <= datetime.now(timezone.utc):
        raise HTTPException(status_code=422, detail="expires_at must be in the future")
    lot = models.StockLot(
        code=payload.code,
        concentration=Decimal(payload.concentration),
        available_volume=payload.available_volume,
        expires_at=expires_at,
        direct_only=payload.direct_only,
    )
    db.add(lot)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail=f"stock lot code {payload.code!r} already exists")
    db.refresh(lot)
    return _stock_out(lot)


@app.get("/stock-lots", response_model=list[schemas.StockLotOut])
def list_stock_lots(db: Session = Depends(get_db)):
    lots = db.query(models.StockLot).order_by(models.StockLot.created_at, models.StockLot.code).all()
    return [_stock_out(lot) for lot in lots]


@app.get("/stock-lots/{lot_id}", response_model=schemas.StockLotOut)
def get_stock_lot(lot_id: str, db: Session = Depends(get_db)):
    lot = db.get(models.StockLot, lot_id)
    if lot is None:
        raise HTTPException(status_code=404, detail="stock lot not found")
    return _stock_out(lot)


@app.post("/stock-lots/{lot_id}/qc-records", response_model=schemas.QcRegisterResponse,
          status_code=201)
def register_qc_record(
    lot_id: str,
    payload: schemas.QcRecordCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
):
    body = payload.model_dump(mode="json")
    request = {"lot_id": lot_id, **body}
    with services.idempotent_operation(db, idempotency_key, "register_qc_record", request) as idem:
        if idem.replay is not None:
            status, cached = idem.replay
            return JSONResponse(status_code=status, content=cached)

        record, event, lot = services.register_qc_record(db, lot_id, body)
        db.flush()
        response = {
            "qc_record": services.qc_record_dict(record),
            "event": services.lot_event_dict(event),
            "lot": _stock_out(lot).model_dump(mode="json"),
        }
        if idem.record is not None:
            idem.complete(201, response)
        # Business state and the replayable idempotency outcome commit together.
        db.commit()
    return JSONResponse(status_code=201, content=response)


@app.get("/stock-lots/{lot_id}/qc-records", response_model=list[schemas.QcRecordOut])
def list_qc_records(lot_id: str, db: Session = Depends(get_db)):
    if db.get(models.StockLot, lot_id) is None:
        raise HTTPException(status_code=404, detail="stock lot not found")
    records = db.scalars(
        select(models.QcRecord)
        .where(models.QcRecord.lot_id == lot_id)
        .order_by(models.QcRecord.created_at, models.QcRecord.id)
    ).all()
    return [services.qc_record_dict(record) for record in records]


@app.get("/stock-lots/{lot_id}/events", response_model=list[schemas.LotEventOut])
def list_lot_events(lot_id: str, db: Session = Depends(get_db)):
    if db.get(models.StockLot, lot_id) is None:
        raise HTTPException(status_code=404, detail="stock lot not found")
    events = db.scalars(
        select(models.LotEvent)
        .where(models.LotEvent.lot_id == lot_id)
        .order_by(models.LotEvent.version_after)
    ).all()
    return [services.lot_event_dict(event) for event in events]


@app.post("/stock-lots/{lot_id}/recall", response_model=schemas.RecallResponse)
def recall_lot(
    lot_id: str,
    payload: schemas.RecallRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
):
    body = payload.model_dump(mode="json")
    request = {"lot_id": lot_id, **body}
    with services.idempotent_operation(db, idempotency_key, "recall_lot", request) as idem:
        if idem.replay is not None:
            status, cached = idem.replay
            return JSONResponse(status_code=status, content=cached)

        lot, event, affected = services.recall_lot(db, lot_id, body)
        db.flush()
        response = {
            "lot_id": lot.id,
            "lot_code": lot.code,
            "status": lot.quality_status,
            "version": lot.version,
            "event": services.lot_event_dict(event),
            "affected_plans": affected,
        }
        if idem.record is not None:
            idem.complete(200, response)
        db.commit()
    return JSONResponse(status_code=200, content=response)


def _request_to_dict(payload: schemas.PlanCreateRequest) -> dict:
    return payload.model_dump(mode="json")


def _revision_out(revision: models.PlanRevision) -> schemas.RevisionOut:
    return schemas.RevisionOut(
        id=revision.id,
        plan_id=revision.plan_id,
        revision_number=revision.revision_number,
        feasible=revision.feasible,
        status=revision.status,
        solution=revision.solution_json,
        created_at=revision.created_at,
    )


@app.post("/plans", response_model=dict[str, Any], status_code=201)
def create_plan(
    payload: schemas.PlanCreateRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
):
    request = _request_to_dict(payload)
    with services.idempotent_operation(db, idempotency_key, "create_plan", request) as idem:
        if idem.replay is not None:
            status, cached = idem.replay
            return JSONResponse(status_code=status, content=cached)

        plan, revision = services.create_plan_with_revision(db, request)
        db.flush()
        response = {
            "plan": {"id": plan.id, "name": plan.name, "current_revision_id": revision.id},
            "revision": _revision_out(revision).model_dump(mode="json"),
        }
        if idem.record is not None:
            idem.complete(201, response)
        db.commit()
    return JSONResponse(status_code=201, content=response)


@app.get("/plans/{plan_id}")
def get_plan(plan_id: str, db: Session = Depends(get_db)):
    plan = db.get(models.Plan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="plan not found")
    revisions = db.query(models.PlanRevision).filter_by(plan_id=plan_id).order_by(
        models.PlanRevision.revision_number
    ).all()
    return {
        "plan": {"id": plan.id, "name": plan.name, "current_revision_id": plan.current_revision_id},
        "revisions": [_revision_out(r).model_dump(mode="json") for r in revisions],
    }


@app.get("/revisions/{revision_id}", response_model=schemas.RevisionOut)
def get_revision(revision_id: str, db: Session = Depends(get_db)):
    revision = db.get(models.PlanRevision, revision_id)
    if revision is None:
        raise HTTPException(status_code=404, detail="revision not found")
    return _revision_out(revision)


@app.post("/plans/{plan_id}/revisions", status_code=201)
def create_revision(
    plan_id: str,
    payload: schemas.RevisionCreateRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
):
    request = payload.model_dump(mode="json", exclude_none=True)
    if payload.request is not None:
        request["request"] = _request_to_dict(payload.request)
    request = {"plan_id": plan_id, **request}
    with services.idempotent_operation(db, idempotency_key, "create_revision", request) as idem:
        if idem.replay is not None:
            status, cached = idem.replay
            return JSONResponse(status_code=status, content=cached)

        plan = db.get(models.Plan, plan_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="plan not found")
        next_request = _request_to_dict(payload.request) if payload.request else None
        revision = services.create_next_revision(db, plan, next_request)
        db.flush()
        response = _revision_out(revision).model_dump(mode="json")
        if idem.record is not None:
            idem.complete(201, response)
        db.commit()
    return JSONResponse(status_code=201, content=response)


@app.post("/revisions/{revision_id}/commit", response_model=schemas.CommitResponse)
def commit_revision(
    revision_id: str,
    payload: schemas.CommitRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
):
    body = payload.model_dump()
    request = {"revision_id": revision_id, **body}
    with services.idempotent_operation(db, idempotency_key, "commit_revision", request) as idem:
        if idem.replay is not None:
            status, cached = idem.replay
            return JSONResponse(status_code=status, content=cached)

        result = services.commit_revision(db, revision_id, body)
        db.flush()
        if idem.record is not None:
            idem.complete(200, result)
        db.commit()
    return JSONResponse(status_code=200, content=result)


@app.post("/revisions/{revision_id}/deviations", status_code=201)
def record_deviation(
    revision_id: str,
    payload: schemas.DeviationRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
):
    body = payload.model_dump(mode="json", exclude_none=True)
    request = {"revision_id": revision_id, **body}
    with services.idempotent_operation(db, idempotency_key, "record_deviation", request) as idem:
        if idem.replay is not None:
            status, cached = idem.replay
            return JSONResponse(status_code=status, content=cached)

        deviation, result = services.record_deviation(db, revision_id, body)
        db.flush()
        response = {
            "id": deviation.id,
            "revision_id": deviation.revision_id,
            "intermediate_id": deviation.intermediate_id,
            "measured_volume": deviation.measured_volume,
            "measured_concentration": format(Decimal(str(deviation.measured_concentration)), "f")
            if deviation.measured_concentration is not None else None,
            "feasible": result["feasible"],
            "affected_aliquot_ids": result["affected_aliquot_ids"],
            "result": result,
            "created_at": deviation.created_at.isoformat(),
        }
        if idem.record is not None:
            idem.complete(201, response)
        db.commit()
    return JSONResponse(status_code=201, content=response)
