from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, TypeVar

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.schema import (
    AuditEvent,
    Datum,
    Job,
    Observation,
    Point,
    Project,
    Snapshot,
    WeightRule,
)
from app.services.network import canonical_sha256

T = TypeVar("T")


class StaleDraftError(HTTPException):
    def __init__(self, entity: str, expected: int, actual: int):
        super().__init__(
            status_code=409,
            detail={
                "error": "optimistic_lock_conflict",
                "entity": entity,
                "expected_lock_version": expected,
                "actual_lock_version": actual,
            },
        )


def apply_optimistic_update(
    db: Session,
    instance: T,
    changes: dict[str, Any],
    *,
    expected_version: int,
    actor: str = "surveyor",
    job_id: int | None = None,
) -> T:
    before = {column.name: getattr(instance, column.name) for column in instance.__table__.columns}
    actual = int(before["lock_version"])
    if actual != expected_version:
        raise StaleDraftError(instance.__class__.__name__.lower(), expected_version, actual)

    for key, value in changes.items():
        if key != "lock_version" and hasattr(instance, key):
            setattr(instance, key, value)
    instance.lock_version = actual + 1
    db.flush()
    after = {column.name: getattr(instance, column.name) for column in instance.__table__.columns}
    db.add(
        AuditEvent(
            entity_type=instance.__class__.__name__.lower(),
            entity_id=getattr(instance, "id"),
            action="update",
            expected_version=expected_version,
            lock_version_in=actual,
            lock_version_out=actual + 1,
            actor=actor,
            job_id=job_id,
            before=_jsonable(before),
            after=_jsonable(after),
        )
    )
    return instance


def _jsonable(value: Any) -> Any:
    from decimal import Decimal

    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _serialize_project_state(project_id: int, db: Session) -> dict[str, Any]:
    points = db.scalars(select(Point).where(Point.project_id == project_id).order_by(Point.id)).all()
    observations = db.scalars(
        select(Observation).where(Observation.project_id == project_id, Observation.active.is_(True)).order_by(Observation.id)
    ).all()
    datums = db.scalars(
        select(Datum).where(Datum.project_id == project_id, Datum.active.is_(True)).order_by(Datum.id)
    ).all()
    rules = db.scalars(
        select(WeightRule).where(WeightRule.project_id == project_id, WeightRule.active.is_(True)).order_by(WeightRule.id)
    ).all()

    # lock_version rides along purely for snapshot-vs-draft review display; it
    # is stripped before hashing so input hashes track measured inputs/rules,
    # not optimistic-lock bookkeeping (dedup semantics stay unchanged).
    payload = {
        "points": [
            {
                "id": p.id,
                "code": p.code,
                "name": p.name,
                "lock_version": p.lock_version,
            }
            for p in points
        ],
        "observations": [
            {
                "id": o.id,
                "line_code": o.line_code,
                "from_point_id": o.from_point_id,
                "to_point_id": o.to_point_id,
                # Immutable source observation retained separately from any adjusted delta.
                "observed_delta_m": float(o.observed_delta_m),
                "distance_m": float(o.distance_m),
                "direction": o.direction,
                "pair_group": o.pair_group,
                "weight_override": None if o.weight_override is None else float(o.weight_override),
                "lock_version": o.lock_version,
            }
            for o in observations
        ],
        "datums": [
            {
                "id": d.id,
                "point_id": d.point_id,
                "elevation_m": float(d.elevation_m),
                "sigma_m": float(d.sigma_m),
                "lock_version": d.lock_version,
            }
            for d in datums
        ],
        "weight_rules": [
            {
                "id": r.id,
                "name": r.name,
                "rule": r.rule,
                "lock_version": r.lock_version,
            }
            for r in rules
        ],
    }

    def _strip_versions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{key: value for key, value in row.items() if key != "lock_version"} for row in rows]

    observations_hash = canonical_sha256(_strip_versions(payload["observations"]))
    rules_hash = canonical_sha256(
        {"datums": _strip_versions(payload["datums"]), "weight_rules": _strip_versions(payload["weight_rules"])}
    )
    payload_hash = canonical_sha256({"observations": observations_hash, "rules": rules_hash})
    summary = {
        "point_count": len(points),
        "observation_count": len(observations),
        "datum_count": len(datums),
        "weight_rule_count": len(rules),
        "distance_total_m": sum(float(o.distance_m) for o in observations),
        "observed_delta_sha256": canonical_sha256(
            [(o.id, o.line_code, float(o.observed_delta_m)) for o in observations]
        ),
        "payload_sha256": payload_hash,
    }
    return {"payload": payload, "observations_hash": observations_hash, "rules_hash": rules_hash, "summary": summary}


def create_immutable_snapshot(db: Session, project_id: int) -> Snapshot:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "project not found")

    state = _serialize_project_state(project_id, db)
    # An unchanged resubmission must not create a new snapshot version.
    existing = db.scalar(
        select(Snapshot)
        .where(
            Snapshot.project_id == project_id,
            Snapshot.observations_sha256 == state["observations_hash"],
            Snapshot.rules_sha256 == state["rules_hash"],
        )
        .order_by(Snapshot.version.desc())
    )
    if existing is not None:
        return existing

    latest_version = db.scalar(select(func.max(Snapshot.version)).where(Snapshot.project_id == project_id)) or 0
    next_version = int(latest_version) + 1
    settings = get_settings()
    algorithm = {
        "signature": settings.algorithm_signature,
        "normal_equations": "sparse A^T W A, scipy.sparse.linalg.spsolve",
        "qr_diagnostic": "numpy.linalg.qr on selected component only; no regularization",
        "illconditioned_condition_number": settings.illconditioned_condition_number,
        "dense_qr_max_rows": settings.dense_qr_max_rows,
        "qr_rank_tol": settings.qr_rank_tol,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    snapshot = Snapshot(
        project_id=project_id,
        version=next_version,
        observations_sha256=state["observations_hash"],
        rules_sha256=state["rules_hash"],
        input_summary=state["summary"],
        payload=state["payload"],
        algorithm=algorithm,
        immutable=True,
    )
    db.add(snapshot)
    db.flush()
    return snapshot


def serialize_draft_state(db: Session, project_id: int) -> dict[str, Any]:
    """Read-only serialized view of the current draft inputs."""
    return _serialize_project_state(project_id, db)


def build_draft_diff(db: Session, project_id: int, base_snapshot_id: int | None = None) -> dict[str, Any]:
    """Compare the current draft against a snapshot without any write side effect.

    Strictly read-only: creates no Snapshot/Job, bumps no lock_version, and never
    mutates the (immutable) baseline snapshot.
    """
    from app.services.diffing import diff_payloads

    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "project not found")

    query = select(Snapshot).where(Snapshot.project_id == project_id)
    if base_snapshot_id is not None:
        snapshot = db.get(Snapshot, base_snapshot_id)
        if snapshot is None or snapshot.project_id != project_id:
            raise HTTPException(404, f"snapshot {base_snapshot_id} not found for project {project_id}")
    else:
        # Latest snapshot defaults to the last review baseline, ordered by version.
        snapshot = db.scalar(query.order_by(Snapshot.version.desc()))

    draft = _serialize_project_state(project_id, db)
    diff = diff_payloads(snapshot.payload if snapshot is not None else None, draft["payload"])
    diff["base_snapshot"] = (
        None
        if snapshot is None
        else {
            "id": snapshot.id,
            "version": snapshot.version,
            "created_at": snapshot.created_at,
            "observations_sha256": snapshot.observations_sha256,
            "rules_sha256": snapshot.rules_sha256,
            "input_summary": snapshot.input_summary,
        }
    )
    diff["draft_summary"] = draft["summary"]
    diff["project"] = {"id": project_id, "code": project.code, "lock_version": project.lock_version}
    diff["query_side_effect"] = "read_only_no_job_no_snapshot"
    return diff


def ensure_single_generation(db: Session, project_id: int, snapshot_id: int) -> tuple[Job, bool]:
    """Return existing job for a snapshot; duplicate submissions never fork generations."""
    generation_key = f"project:{project_id}:snapshot:{snapshot_id}"
    existing = db.scalar(select(Job).where(Job.generation_key == generation_key))
    if existing is not None:
        return existing, False

    job = Job(project_id=project_id, snapshot_id=snapshot_id, generation_key=generation_key)
    db.add(job)
    db.flush()
    for name in ("import_qc", "component_precheck", "solve", "publish_checks"):
        db.add(JobStage(job_id=job.id, name=name))
    db.flush()
    return job, True
