from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StockLotCreate(BaseModel):
    code: str = Field(min_length=1, max_length=128)
    concentration: str = Field(description="Decimal concentration in arbitrary but consistent units")
    available_volume: int = Field(ge=0)
    expires_at: datetime
    direct_only: bool = False

    @field_validator("concentration")
    @classmethod
    def concentration_nonnegative_decimal(cls, value: str) -> str:
        from decimal import InvalidOperation, Decimal

        try:
            amount = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("concentration must be a decimal string") from exc
        if amount < 0:
            raise ValueError("concentration must be non-negative")
        return value


class StockLotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    code: str
    concentration: str
    available_volume: int
    reserved_volume: int
    expires_at: datetime
    direct_only: bool
    quality_status: str
    version: int


class QcRecordCreate(BaseModel):
    item: str = Field(min_length=1, max_length=128)
    value: str = Field(description="Decimal measured value")
    unit: str = Field(min_length=1, max_length=32)
    lower_limit: str
    upper_limit: str
    rule_version: str = Field(min_length=1, max_length=64)
    expected_version: int = Field(ge=1)

    @field_validator("value", "lower_limit", "upper_limit")
    @classmethod
    def finite_decimal(cls, value: str) -> str:
        from decimal import InvalidOperation, Decimal

        try:
            amount = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("must be a decimal string") from exc
        if not amount.is_finite():
            raise ValueError("must be a finite decimal")
        return value

    @model_validator(mode="after")
    def limits_ordered(self) -> "QcRecordCreate":
        from decimal import Decimal

        if Decimal(self.lower_limit) > Decimal(self.upper_limit):
            raise ValueError("lower_limit must be less than or equal to upper_limit")
        return self


class QcRecordOut(BaseModel):
    id: str
    lot_id: str
    item: str
    value: str
    unit: str
    lower_limit: str
    upper_limit: str
    rule_version: str
    in_range: bool
    created_at: datetime


class LotEventOut(BaseModel):
    id: str
    lot_id: str
    event_type: str
    qc_record_id: str | None
    reason: str | None
    version_after: int
    created_at: datetime


class QcRegisterResponse(BaseModel):
    qc_record: QcRecordOut
    event: LotEventOut
    lot: StockLotOut


class RecallRequest(BaseModel):
    expected_version: int = Field(ge=1)
    reason: str | None = Field(default=None, max_length=2000)


class AffectedPlanOut(BaseModel):
    plan_id: str
    plan_name: str
    revision_id: str
    revision_number: int
    frozen_volume: int
    frozen_reservation_ids: list[str]
    affected_aliquot_ids: list[str]
    affected_edges: list[dict[str, Any]]


class RecallResponse(BaseModel):
    lot_id: str
    lot_code: str
    status: Literal["recalled"]
    version: int
    event: LotEventOut
    affected_plans: list[AffectedPlanOut]


class ForbiddenPair(BaseModel):
    lot_a: str
    lot_b: str


class ContaminationOrder(BaseModel):
    before_lot: str
    after_lot: str


class PlanCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    target_min_concentration: str
    target_max_concentration: str
    aliquot_volume: int = Field(gt=0)
    aliquot_count: int = Field(gt=0)
    final_container_capacity: int = Field(gt=0)
    intermediate_container_capacity: int | None = Field(default=None, gt=0)
    max_intermediate_containers: int = Field(default=4, ge=0, le=100)
    pipette_min_volume: int = Field(gt=0)
    pipette_increment: int = Field(gt=0)
    scheduled_at: datetime
    stock_lot_ids: list[str] = Field(min_length=1)
    forbidden_pairs: list[ForbiddenPair] = Field(default_factory=list)
    contamination_orders: list[ContaminationOrder] = Field(default_factory=list)


class RevisionCreateRequest(BaseModel):
    scheduled_at: datetime | None = None
    request: PlanCreateRequest | None = None


class CommitRequest(BaseModel):
    expected_revision_version: int = Field(
        default=1, ge=1, description="Revision draft version; always 1 for this implementation"
    )
    expected_lot_versions: dict[str, int] = Field(default_factory=dict)
    force: bool = Field(default=False, description="Must be false; relaxed conflict plans cannot commit")


class PlanOut(BaseModel):
    id: str
    name: str
    current_revision_id: str | None


class RevisionOut(BaseModel):
    id: str
    plan_id: str
    revision_number: int
    feasible: bool
    status: str
    solution: dict[str, Any]
    created_at: datetime


class CommitResponse(BaseModel):
    revision_id: str
    status: Literal["committed"]
    reservations: dict[str, int]
    lot_versions: dict[str, int]


class DeviationRequest(BaseModel):
    intermediate_id: str = Field(min_length=1)
    measured_volume: int = Field(ge=0)
    measured_concentration: str | None = None

    @field_validator("measured_concentration")
    @classmethod
    def concentration_decimal(cls, value: str | None) -> str | None:
        if value is None:
            return value
        from decimal import InvalidOperation, Decimal

        try:
            amount = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("measured_concentration must be a decimal string") from exc
        if amount < 0:
            raise ValueError("measured_concentration must be non-negative")
        return value


class DeviationOut(BaseModel):
    id: str
    revision_id: str
    intermediate_id: str
    measured_volume: int
    measured_concentration: str | None
    feasible: bool
    affected_aliquot_ids: list[str]
    result: dict[str, Any]
    created_at: datetime


class HealthOut(BaseModel):
    status: str
