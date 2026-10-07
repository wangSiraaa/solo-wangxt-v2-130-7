"""Snapshot-vs-draft review tests.

The pure diffing tests run without a database. The endpoint tests use an
in-memory fake Session through FastAPI's dependency override, which lets the
acceptance checks prove that querying the diff performs no write: no snapshot
or Job is created and no lock_version advances.
"""
from __future__ import annotations

import warnings
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm.evaluator import EvaluatorCompiler

from app.api.routes import router
from app.core.db import get_db
from app.models.schema import Datum, Job, Observation, Point, Project, Snapshot, WeightRule
from app.services.diffing import diff_payloads

warnings.filterwarnings("ignore", category=DeprecationWarning)


# --------------------------------------------------------------------------- payloads

def point(point_id: int, code: str, name: str | None = None, lock_version: int = 1) -> dict[str, Any]:
    return {"id": point_id, "code": code, "name": name, "lock_version": lock_version}


def observation(
    obs_id: int,
    line_code: str,
    from_id: int,
    to_id: int,
    *,
    delta: float = 1.0,
    distance: float = 1000.0,
    override: float | None = None,
    lock_version: int = 1,
) -> dict[str, Any]:
    return {
        "id": obs_id,
        "line_code": line_code,
        "from_point_id": from_id,
        "to_point_id": to_id,
        "observed_delta_m": delta,
        "distance_m": distance,
        "direction": "forward",
        "pair_group": None,
        "weight_override": override,
        "lock_version": lock_version,
    }


def datum(datum_id: int, point_id: int, elevation: float = 100.0, sigma: float = 0.001, lock_version: int = 1) -> dict[str, Any]:
    return {
        "id": datum_id,
        "point_id": point_id,
        "elevation_m": elevation,
        "sigma_m": sigma,
        "lock_version": lock_version,
    }


def rule(rule_id: int, name: str = "default", rule: dict[str, Any] | None = None, lock_version: int = 1) -> dict[str, Any]:
    return {
        "id": rule_id,
        "name": name,
        "rule": rule or {"method": "millimeter_sqrt_km", "c_km": 1.0, "base_sigma_m": 0.001},
        "lock_version": lock_version,
    }


def payload(*, points=None, observations=None, datums=None, weight_rules=None) -> dict[str, Any]:
    return {
        "points": points or [],
        "observations": observations or [],
        "datums": datums or [],
        "weight_rules": weight_rules or [rule(1)],
    }


BRIDGE_BASE = payload(
    points=[point(1, "A"), point(2, "B"), point(3, "C")],
    observations=[
        observation(10, "L1", 1, 2),
        observation(11, "L2", 2, 3),
        observation(12, "L3", 1, 3, delta=2.004, distance=2000.0),
    ],
    datums=[datum(20, 1)],
    weight_rules=[rule(1)],
)


# --------------------------------------------------------------------------- pure diff

def test_weight_only_change_shows_rule_change_nothing_else():
    draft = payload(
        points=[point(1, "A"), point(2, "B"), point(3, "C")],
        observations=[
            observation(10, "L1", 1, 2),
            observation(11, "L2", 2, 3),
            observation(12, "L3", 1, 3, delta=2.004, distance=2000.0),
        ],
        datums=[datum(20, 1)],
        weight_rules=[
            rule(1, rule={"method": "distance_inverse_km", "c_km": 2.5, "base_sigma_m": 0.001}, lock_version=4),
        ],
    )
    result = diff_payloads(BRIDGE_BASE, draft)

    assert result["points"] == {"added": [], "removed": [], "modified": []}
    assert result["observations"] == {"added": [], "removed": [], "modified": []}
    assert result["datums"] == {"added": [], "removed": [], "modified": []}
    modified_rules = result["weight_rules"]["modified"]
    assert len(modified_rules) == 1
    assert modified_rules[0]["id"] == 1
    assert modified_rules[0]["base_lock_version"] == 1
    assert modified_rules[0]["lock_version"] == 4
    changed_fields = {change["field"] for change in modified_rules[0]["changes"]}
    assert changed_fields == {"rule"}
    assert result["totals"]["weight_rules_only"] is True
    assert result["totals"]["topology_changed"] is False
    # Nothing structural or highlighted on the graph.
    topology = result["topology"]
    assert not topology["nodes"]["added"] and not topology["nodes"]["removed"]
    assert not topology["edges"]["added"] and not topology["edges"]["removed"] and not topology["edges"]["modified"]


def test_deleted_bridge_segment_is_visible_as_topology_edge_removal():
    # Deactivate the only A-C chord in the draft.
    draft = payload(
        points=[point(1, "A"), point(2, "B"), point(3, "C")],
        observations=[
            observation(10, "L1", 1, 2),
            observation(11, "L2", 2, 3),
        ],
        datums=[datum(20, 1)],
        weight_rules=[rule(1)],
    )
    result = diff_payloads(BRIDGE_BASE, draft)

    removed_edges = result["topology"]["edges"]["removed"]
    assert len(removed_edges) == 1
    assert removed_edges[0]["observation_id"] == 12
    assert removed_edges[0]["line_code"] == "L3"
    assert removed_edges[0]["source"] == "A"
    assert removed_edges[0]["target"] == "C"
    assert result["observations"]["removed"][0]["id"] == 12
    assert result["observations"]["removed"][0]["base_lock_version"] == 1
    assert result["totals"]["topology_changed"] is True
    assert result["totals"]["weight_rules_only"] is False


def test_added_segment_and_modified_delta_distance_are_reported():
    draft = payload(
        points=[point(1, "A"), point(2, "B"), point(3, "C")],
        observations=[
            observation(10, "L1", 1, 2, delta=1.002),
            observation(11, "L2", 2, 3, distance=1350.0),
            observation(12, "L3", 1, 3, delta=2.004, distance=2000.0),
            observation(13, "L4", 3, 1, delta=-2.003, distance=2000.0),
        ],
        datums=[datum(20, 1, elevation=100.012)],
        weight_rules=[rule(1)],
    )
    result = diff_payloads(BRIDGE_BASE, draft)

    added_edges = result["topology"]["edges"]["added"]
    assert [edge["observation_id"] for edge in added_edges] == [13]
    mod_obs = {entry["id"]: {change["field"] for change in entry["changes"]} for entry in result["observations"]["modified"]}
    assert mod_obs[10] == {"observed_delta_m"}
    assert mod_obs[11] == {"distance_m"}
    mod_obs10 = next(entry for entry in result["observations"]["modified"] if entry["id"] == 10)
    change = mod_obs10["changes"][0]
    assert change["before"] == pytest.approx(1.0)
    assert change["after"] == pytest.approx(1.002)
    assert result["datums"]["modified"][0]["id"] == 20
    assert {c["field"] for c in result["datums"]["modified"][0]["changes"]} == {"elevation_m"}
    assert result["totals"]["topology_changed"] is True


def test_same_code_different_point_id_is_removed_and_added_not_matched():
    # A re-surveyed benchmark reuses code "B" but is a new row (id 4 != 2).
    base = payload(
        points=[point(1, "A"), point(2, "B")],
        observations=[observation(10, "L1", 1, 2)],
        datums=[datum(20, 2)],
    )
    draft = payload(
        points=[point(1, "A"), point(4, "B", name="new BM")],
        observations=[observation(10, "L1", 1, 4)],
        datums=[datum(20, 4)],
    )
    result = diff_payloads(base, draft)

    removed_points = {entry["id"] for entry in result["points"]["removed"]}
    added_points = {entry["id"] for entry in result["points"]["added"]}
    assert removed_points == {2}
    assert added_points == {4}
    assert result["points"]["modified"] == []
    # The rewired observation is a modification (same stable obs id) whose
    # endpoint change is flagged structural — not a silent same-name match.
    mod_obs = result["observations"]["modified"]
    assert len(mod_obs) == 1
    assert mod_obs[0]["id"] == 10
    assert {change["field"] for change in mod_obs[0]["changes"]} == {"to_point_id"}
    assert result["topology"]["edges"]["modified"][0]["structural"] is True
    # Same datum id re-pointed at the replacement benchmark: an id-tracked
    # modification (point_id 2 -> 4), never a same-name match.
    mod_datum = result["datums"]["modified"]
    assert len(mod_datum) == 1 and mod_datum[0]["id"] == 20
    assert {change["field"] for change in mod_datum[0]["changes"]} == {"point_id"}
    assert result["datums"]["added"] == [] and result["datums"]["removed"] == []


def test_no_baseline_treats_everything_as_added():
    result = diff_payloads(None, BRIDGE_BASE)
    assert {entry["id"] for entry in result["points"]["added"]} == {1, 2, 3}
    assert {entry["id"] for entry in result["observations"]["added"]} == {10, 11, 12}
    assert {entry["id"] for entry in result["datums"]["added"]} == {20}
    assert result["totals"]["total"] == 8
    assert result["totals"]["topology_changed"] is True


def test_storage_level_float_noise_does_not_falsely_show_as_modified():
    draft = payload(
        points=[point(1, "A"), point(2, "B"), point(3, "C")],
        observations=[
            observation(10, "L1", 1, 2, delta=1.0 + 1e-13),
            observation(11, "L2", 2, 3),
            observation(12, "L3", 1, 3, delta=2.004, distance=2000.0),
        ],
        datums=[datum(20, 1)],
        weight_rules=[rule(1)],
    )
    result = diff_payloads(BRIDGE_BASE, draft)
    assert result["observations"]["modified"] == []
    assert result["totals"]["total"] == 0
    assert result["totals"]["weight_rules_only"] is False


# --------------------------------------------------------------------------- endpoint fakes

class FakeResult:
    def __init__(self, rows: list[Any]):
        self._rows = rows

    def all(self) -> list[Any]:
        return list(self._rows)


class FakeScalarSelect:
    """Minimal interpreter for the exact select shapes used by snapshots.py."""

    def __init__(self, db: "FakeSession", statement: Any):
        self._db = db
        self._statement = statement

    def _target_model(self) -> type:
        description = self._statement.column_descriptions
        return description[0]["entity"]

    def _candidate_rows(self) -> list[Any]:
        model = self._target_model()
        return list(self._db.rows.get(model, {}).values())

    def _filtered_rows(self) -> list[Any]:
        rows = self._candidate_rows()
        whereclause = self._statement.whereclause
        if whereclause is not None:
            predicate = EvaluatorCompiler().process(whereclause)
            rows = [row for row in rows if predicate(row)]
        for clause in self._statement._order_by_clauses:
            descending = getattr(clause, "modifier", None) is not None and getattr(clause.modifier, "__name__", "") == "desc_op"
            column = getattr(clause, "name", None) or clause.element.name
            rows = sorted(rows, key=lambda row, column=column: getattr(row, column), reverse=descending)
        return rows

    def scalars_all(self) -> list[Any]:
        return self._filtered_rows()

    def scalar_one_or_none(self) -> Any | None:
        from sqlalchemy.sql.functions import Function

        column = self._statement._raw_columns[0]
        if isinstance(column, Function) and getattr(column, "name", None) == "max":
            return self._max(column)
        # Plain-entity scalar: first filtered/ordered row (latest snapshot lookup).
        rows = self._filtered_rows()
        return rows[0] if rows else None

    def _max(self, column: Any) -> Any:
        inner = list(column.clauses)[0]
        attr = inner.name
        values = [getattr(row, attr) for row in self._filtered_rows() if getattr(row, attr) is not None]
        return max(values) if values else None


class FakeSession:
    def __init__(self, rows: dict[type, dict[int, Any]]):
        self.rows = rows
        self.added: list[Any] = []
        self.commits = 0
        self.next_id = {model: (max(by_id) + 1 if by_id else 1) for model, by_id in rows.items()}

    def get(self, model: type, entity_id: int) -> Any | None:
        return self.rows.get(model, {}).get(entity_id)

    def scalar(self, statement: Any) -> Any:
        return FakeScalarSelect(self, statement).scalar_one_or_none()

    def scalars(self, statement: Any) -> FakeResult:
        return FakeResult(FakeScalarSelect(self, statement).scalars_all())

    def add(self, instance: Any) -> None:
        self.added.append(instance)

    def add_all(self, instances: list[Any]) -> None:
        self.added.extend(instances)

    def flush(self) -> None:
        for instance in self.added:
            model = type(instance)
            if getattr(instance, "id", None) is None:
                instance.id = self.next_id[model]
                self.next_id[model] += 1
            self.rows.setdefault(model, {})[instance.id] = instance
        self.added.clear()

    def commit(self) -> None:
        self.flush()
        self.commits += 1

    def rollback(self) -> None:
        self.added.clear()

    def close(self) -> None:
        pass


def _snapshot(project_id: int, version: int, snap_id: int, payload_data: dict[str, Any]) -> Snapshot:
    return Snapshot(
        id=snap_id,
        project_id=project_id,
        version=version,
        kind="observations_rules",
        observations_sha256="obs-hash",
        rules_sha256="rules-hash",
        input_summary={"observation_count": len(payload_data["observations"])},
        payload=payload_data,
        algorithm={"signature": "weighted-ls-v1"},
        created_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
        immutable=True,
    )


def _client(rows: dict[type, dict[int, Any]]) -> tuple[TestClient, FakeSession]:
    db = FakeSession(rows)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app), db


def _bridge_orm_rows(active_obs_ids=(10, 11, 12), rule_lock=1, rule_body=None) -> dict[type, dict[int, Any]]:
    project = Project(id=1, code="P1", name="bridge", lock_version=3)
    points = {
        p_id: Point(id=p_id, project_id=1, code=code, name=None, geom=None, lock_version=1)
        for p_id, code in ((1, "A"), (2, "B"), (3, "C"))
    }
    observations = {
        obs_id: Observation(
            id=obs_id,
            project_id=1,
            line_code=line,
            from_point_id=from_id,
            to_point_id=to_id,
            observed_delta_m=delta,
            distance_m=distance,
            direction="forward",
            pair_group=None,
            active=obs_id in active_obs_ids,
            weight_override=None,
            lock_version=1,
        )
        for obs_id, line, from_id, to_id, delta, distance in (
            (10, "L1", 1, 2, 1.0, 1000.0),
            (11, "L2", 2, 3, 1.0, 1000.0),
            (12, "L3", 1, 3, 2.004, 2000.0),
        )
    }
    datums = {20: Datum(id=20, project_id=1, point_id=1, elevation_m=100.0, sigma_m=0.001, active=True, lock_version=1)}
    body = rule_body or {"method": "millimeter_sqrt_km", "c_km": 1.0, "base_sigma_m": 0.001}
    rules = {1: WeightRule(id=1, project_id=1, name="default", rule=body, active=True, lock_version=rule_lock)}
    return {
        Project: {1: project},
        Point: points,
        Observation: observations,
        Datum: datums,
        WeightRule: rules,
    }


def test_endpoint_weight_only_change_creates_nothing_and_advances_no_version():
    snapshot = _snapshot(1, 1, 100, BRIDGE_BASE)
    rows = _bridge_orm_rows(
        rule_lock=4,
        rule_body={"method": "distance_inverse_km", "c_km": 2.5, "base_sigma_m": 0.001},
    )
    rows[Snapshot] = {100: snapshot}
    completed_job = Job(
        id=9,
        project_id=1,
        snapshot_id=100,
        generation_key="project:1:snapshot:100",
        status="completed",
        current_stage="publish_checks",
        lock_version=1,
    )
    rows[Job] = {9: completed_job}
    client, db = _client(rows)

    before = {
        "project_version": rows[Project][1].lock_version,
        "rule_version": rows[WeightRule][1].lock_version,
        "snapshot_count": len(rows[Snapshot]),
        "job_count": len(rows[Job]),
        "snapshot_payload_hash": snapshot.payload["weight_rules"][0]["rule"],
    }

    for _ in range(3):  # Repeated reviews must stay side-effect free.
        response = client.get("/api/projects/1/draft-diff")
        assert response.status_code == 200
        body = response.json()
        assert body["totals"]["weight_rules_only"] is True
        assert body["totals"]["topology_changed"] is False
        assert len(body["weight_rules"]["modified"]) == 1
        assert body["base_snapshot"]["version"] == 1
        assert body["query_side_effect"] == "read_only_no_job_no_snapshot"

    assert db.commits == 0
    assert db.added == []
    assert rows[Project][1].lock_version == before["project_version"]
    assert rows[WeightRule][1].lock_version == before["rule_version"]
    assert len(rows[Snapshot]) == before["snapshot_count"]
    assert len(rows[Job]) == before["job_count"]
    assert snapshot.payload["weight_rules"][0]["rule"] == before["snapshot_payload_hash"]
    assert snapshot.immutable is True
    # Completed job is untouched.
    assert rows[Job][9].status == "completed"


def test_endpoint_deleted_bridge_segment_visible_and_no_job_generated():
    snapshot = _snapshot(1, 1, 100, BRIDGE_BASE)
    rows = _bridge_orm_rows(active_obs_ids=(10, 11))
    rows[Snapshot] = {100: snapshot}
    client, db = _client(rows)

    response = client.get("/api/projects/1/draft-diff")
    assert response.status_code == 200
    body = response.json()

    removed = body["topology"]["edges"]["removed"]
    assert len(removed) == 1
    assert removed[0]["observation_id"] == 12
    assert body["totals"]["topology_changed"] is True
    assert body["totals"]["weight_rules_only"] is False
    assert "draft_summary" in body
    assert body["draft_summary"]["observation_count"] == 2
    assert body["draft_summary"]["point_count"] == 3
    # No Job, no snapshot, no writes of any kind.
    assert db.commits == 0
    assert db.added == []
    assert Job not in rows or all(job.snapshot_id == 100 for job in rows.get(Job, {}).values())
    assert snapshot.payload is BRIDGE_BASE or len(snapshot.payload["observations"]) == 3


def test_endpoint_explicit_baseline_must_belong_to_project():
    rows = _bridge_orm_rows()
    foreign = _snapshot(2, 1, 777, BRIDGE_BASE)
    rows[Snapshot] = {777: foreign}
    client, _db = _client(rows)
    response = client.get("/api/projects/1/draft-diff", params={"base_snapshot_id": 777})
    assert response.status_code == 404


def test_endpoint_without_any_snapshot_returns_null_baseline():
    rows = _bridge_orm_rows()
    client, db = _client(rows)
    response = client.get("/api/projects/1/draft-diff")
    assert response.status_code == 200
    body = response.json()
    assert body["base_snapshot"] is None
    assert {entry["id"] for entry in body["points"]["added"]} == {1, 2, 3}
    assert db.commits == 0
    assert Snapshot not in rows
