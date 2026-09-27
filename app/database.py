import logging
import os
from collections.abc import Generator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql+psycopg2://lab:lab@localhost:5432/lab_planning"
)


class Base(DeclarativeBase):
    pass


connect_args = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _upgrade_idempotent_operations()


def _table_columns(inspector, table: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table)}


def _empty_body_predicate() -> str:
    """SQL fragment matching the empty-object body of a legacy placeholder.

    The old two-phase implementation committed its placeholder as
    ``status_code=200, response_json={}`` *before* the business change and
    only afterwards rewrote the row with the real outcome, so a crash in
    between left the empty object durably committed.  Every endpoint of this
    API persists a non-empty body on success, and live ``in_progress`` rows
    carry ``NULL`` payloads, so an empty object can never be a legitimate
    completed record.
    """
    if engine.dialect.name == "postgresql":
        return "response_json::jsonb = '{}'::jsonb"
    # SQLAlchemy's JSON type serializes {} to the exact text '{}'.
    return "response_json = '{}'"


def _purge_placeholder_rows(conn, current_schema: bool) -> int:
    """Delete incomplete placeholder rows left by the old two-phase code.

    Only rows without a replayable outcome are removed: a missing final
    status code, a missing body, or the empty-object placeholder body.
    Legitimate completed records (final status code + non-empty serializable
    body) are kept.  With the current schema, ``in_progress`` rows are never
    touched: their ``NULL`` payload means a live (or recently crashed)
    request still owns the key, and the runtime stale-timeout logic handles
    those.  Deleting here (rather than quarantining) frees the key so a
    retry re-executes the operation instead of replaying an empty success.
    """
    where = (
        "status_code IS NULL OR response_json IS NULL OR "
        + _empty_body_predicate()
    )
    if current_schema:
        where = f"state = 'completed' AND ({where})"
    result = conn.execute(text(f"DELETE FROM idempotent_operations WHERE {where}"))
    purged = result.rowcount or 0
    if purged:
        logger.warning(
            "startup migration purged %s incomplete idempotent_operations "
            "placeholder row(s) left by the legacy two-phase implementation; "
            "their idempotency keys will re-execute on retry instead of "
            "replaying an empty 200 response",
            purged,
        )
    return purged


def _upgrade_idempotent_operations() -> None:
    """Bring an idempotent_operations table created by earlier versions forward.

    The idempotency store changed from a placeholder row (status_code=200,
    response_json={}) committed *before* the business change to an
    in_progress -> completed state machine whose completed row is committed in
    the same transaction as the business change.  This is a lightweight startup
    migration; production deployments should use Alembic.

    Besides adding the new columns, the migration removes the incomplete
    placeholders the old code could leave durably committed (crash between
    the placeholder commit and the final update).  Previously they were
    stamped ``completed`` here, which made every later request with the same
    Idempotency-Key replay an empty ``200 {}`` success.  Only rows carrying a
    complete final status code and a non-empty serializable body survive as
    legitimate completed records.  The purge also runs when the schema is
    already current, repairing databases upgraded by the previous migration;
    it matches nothing on healthy databases, so repeated runs converge to
    the same state.
    """
    inspector = inspect(engine)
    if "idempotent_operations" not in inspector.get_table_names():
        return

    columns = _table_columns(inspector, "idempotent_operations")
    required = {"state", "locked_at", "completed_at"}
    if required.issubset(columns):
        with engine.begin() as conn:
            _purge_placeholder_rows(conn, current_schema=True)
        return

    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            # Purge placeholders BEFORE stamping every legacy row 'completed'.
            _purge_placeholder_rows(conn, current_schema=False)
            if "state" not in columns:
                # Every legacy row that survived the purge above is a
                # finalized outcome of the old code, so 'completed' is the
                # correct initial state for all of them.
                conn.execute(text(
                    "ALTER TABLE idempotent_operations ADD COLUMN state VARCHAR(32) "
                    "NOT NULL DEFAULT 'completed'"
                ))
            if "locked_at" not in columns:
                conn.execute(text(
                    "ALTER TABLE idempotent_operations ADD COLUMN locked_at "
                    "TIMESTAMPTZ NOT NULL DEFAULT now()"
                ))
            if "completed_at" not in columns:
                conn.execute(text(
                    "ALTER TABLE idempotent_operations ADD COLUMN completed_at TIMESTAMPTZ"
                ))
            conn.execute(text(
                "UPDATE idempotent_operations SET completed_at = created_at "
                "WHERE completed_at IS NULL"
            ))
            # The completed payload must be nullable so that in_progress rows
            # (body produced only at the end) can exist.
            conn.execute(text(
                "ALTER TABLE idempotent_operations ALTER COLUMN status_code DROP NOT NULL"
            ))
            conn.execute(text(
                "ALTER TABLE idempotent_operations ALTER COLUMN response_json DROP NOT NULL"
            ))
        return

    # SQLite cannot ALTER most column constraints: rebuild the table, copying
    # only legitimate completed records and dropping placeholder rows.
    with engine.begin() as conn:
        conn.execute(text(
            """
            CREATE TABLE idempotent_operations_new (
                idempotency_key VARCHAR(128) PRIMARY KEY,
                operation_type VARCHAR(64) NOT NULL,
                request_hash VARCHAR(128) NOT NULL,
                state VARCHAR(32) NOT NULL,
                status_code INTEGER,
                response_json JSON,
                locked_at DATETIME NOT NULL,
                completed_at DATETIME,
                created_at DATETIME
            )
            """
        ))
        conn.execute(text(
            """
            INSERT INTO idempotent_operations_new (
                idempotency_key, operation_type, request_hash, state, status_code,
                response_json, locked_at, completed_at, created_at
            )
            SELECT idempotency_key, operation_type, request_hash, 'completed',
                   status_code, response_json, COALESCE(created_at, CURRENT_TIMESTAMP),
                   created_at, created_at
            FROM idempotent_operations
            WHERE NOT (status_code IS NULL OR response_json IS NULL OR """
            + _empty_body_predicate() + ")"
        ))
        conn.execute(text("DROP TABLE idempotent_operations"))
        conn.execute(text("ALTER TABLE idempotent_operations_new RENAME TO idempotent_operations"))
