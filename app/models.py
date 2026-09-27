import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.database import Base


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class StockLot(Base):
    __tablename__ = "stock_lots"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    code: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    concentration: Mapped[float] = mapped_column(Numeric(30, 12), nullable=False)
    available_volume: Mapped[int] = mapped_column(Integer, nullable=False)
    reserved_volume: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    direct_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # pending -> released | quarantined; released -> recalled
    quality_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    reservations: Mapped[list["Reservation"]] = relationship(back_populates="lot")
    qc_records: Mapped[list["QcRecord"]] = relationship(back_populates="lot")
    events: Mapped[list["LotEvent"]] = relationship(back_populates="lot")


class QcRecord(Base):
    """Immutable quality-control measurement registered for a stock lot."""

    __tablename__ = "qc_records"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    lot_id: Mapped[str] = mapped_column(ForeignKey("stock_lots.id"), nullable=False, index=True)
    item: Mapped[str] = mapped_column(String(128), nullable=False)
    value: Mapped[float] = mapped_column(Numeric(30, 12), nullable=False)
    unit: Mapped[str] = mapped_column(String(32), nullable=False)
    lower_limit: Mapped[float] = mapped_column(Numeric(30, 12), nullable=False)
    upper_limit: Mapped[float] = mapped_column(Numeric(30, 12), nullable=False)
    rule_version: Mapped[str] = mapped_column(String(64), nullable=False)
    in_range: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    lot: Mapped[StockLot] = relationship(back_populates="qc_records")


class LotEvent(Base):
    """Append-only quality state event; one row per atomic version transition."""

    __tablename__ = "lot_events"
    __table_args__ = (UniqueConstraint("lot_id", "version_after"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    lot_id: Mapped[str] = mapped_column(ForeignKey("stock_lots.id"), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)  # released|quarantined|recalled
    qc_record_id: Mapped[str | None] = mapped_column(ForeignKey("qc_records.id"), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    version_after: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    lot: Mapped[StockLot] = relationship(back_populates="events")
    qc_record: Mapped[QcRecord | None] = relationship()


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    current_revision_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    revisions: Mapped[list["PlanRevision"]] = relationship(back_populates="plan")


class PlanRevision(Base):
    __tablename__ = "plan_revisions"
    __table_args__ = (UniqueConstraint("plan_id", "revision_number"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    plan_id: Mapped[str] = mapped_column(ForeignKey("plans.id"), nullable=False, index=True)
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    request_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    solution_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    feasible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    supersedes_id: Mapped[str | None] = mapped_column(ForeignKey("plan_revisions.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    plan: Mapped[Plan] = relationship(back_populates="revisions")
    reservations: Mapped[list["Reservation"]] = relationship(back_populates="revision")


class Reservation(Base):
    __tablename__ = "reservations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    revision_id: Mapped[str] = mapped_column(
        ForeignKey("plan_revisions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    lot_id: Mapped[str] = mapped_column(ForeignKey("stock_lots.id"), nullable=False, index=True)
    volume: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    revision: Mapped[PlanRevision] = relationship(back_populates="reservations")
    lot: Mapped[StockLot] = relationship(back_populates="reservations")


class IdempotentOperation(Base):
    """Outcome of an idempotent state-changing request.

    Lifecycle of a row:
      in_progress -> completed : the business change and the final response
                                  (status code + serializable body) are written
                                  in the *same* commit, so a completed row is
                                  always safe to replay;
      in_progress -> (deleted)  : the request failed or the process died, so no
                                  replayable placeholder is ever left behind.

    ``locked_at`` serializes concurrent requests carrying the same key: the
    holder of an in_progress row owns the operation, everyone else waits.
    """

    __tablename__ = "idempotent_operations"

    idempotency_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    operation_type: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    # in_progress | completed
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="in_progress")
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    locked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ExecutionState(Base):
    __tablename__ = "execution_states"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    plan_id: Mapped[str] = mapped_column(ForeignKey("plans.id"), nullable=False, index=True)
    revision_id: Mapped[str] = mapped_column(ForeignKey("plan_revisions.id"), nullable=False, index=True)
    state_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Deviation(Base):
    __tablename__ = "deviations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    plan_id: Mapped[str] = mapped_column(ForeignKey("plans.id"), nullable=False, index=True)
    revision_id: Mapped[str] = mapped_column(ForeignKey("plan_revisions.id"), nullable=False, index=True)
    intermediate_id: Mapped[str] = mapped_column(String(128), nullable=False)
    measured_volume: Mapped[int] = mapped_column(Integer, nullable=False)
    measured_concentration: Mapped[float | None] = mapped_column(Numeric(30, 12), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
