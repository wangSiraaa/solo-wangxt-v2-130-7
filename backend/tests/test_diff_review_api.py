"""Integration-level tests for the read-only draft-diff load path.

No database is required: a tiny in-memory fake session stands in for
SQLAlchemy. These tests guard the acceptance invariants:

* reviewing the diff never commits/advances lock versions;
* reviewing the diff never creates a Job or a Snapshot;
* the latest snapshot is used as the default baseline;
* lock versions come from a hash-free side channel, never from the payload;
* the end-to-end load keeps matching by stable id.
"""
from types import SimpleNamespace

import pytest

from app.services import diff as diff_module
from app.services.network import canonical_sha256

TABLE_TO_MODEL = {
    "points": "Point",
    "observations": "Observation",
    "datums": "Datum",
    "weight_rules": "WeightRule",
}


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class FakeSelect:
    """Minimal stand-in for sqlalchemy.select used by the review load path."""

    def __init__(self, *targets):
        first = targets[0] if targets else None
        name = getattr(first, "__name__", None)
        self._is_aggregate = isinstance(first, tuple) and name is None
        self._is_column_select = len(targets) > 1 and name is None and hasattr(first, "class_")
        self._fake_model = None if self._is_column_select else name
        self._table = (
            getattr(getattr(first, "class_", None), "__tablename__", None)
            if self._is_column_select
            else None
        )
        self._fake_active = False

    def where(self, *conditions):
        if not self._is_aggregate and self._fake_model is not None:
            self._fake_active = True
        return self

    def order_by(self, *_args):
        return self


class FakeSession:
    def __init__(self, project, snapshots, rows):
        self._project = project
        self._snapshots = {s.id: s for s in snapshots}
        self._rows = rows
        self.commits = 0
        self.rollbacks = 0
        self.added: list[object] = []

    def get(self, model, entity_id):
        if model.__name__ == "Project":
            return self._project if self._project.id == entity_id else None
        if model.__name__ == "Snapshot":
            return self._snapshots.get(entity_id)
        return None

    def scalar(self, _statement):
        return max(self._snapshots, default=None)

    def scalars(self, statement):
        filtered = [row for row in self._rows if row.model == statement._fake_model]
        if statement._fake_active:
            filtered = [row for row in filtered if row.active]
        return FakeResult(sorted(filtered, key=lambda r: r.id))

    def execute(self, statement):
        if statement._fake_model is None:
            model_name = TABLE_TO_MODEL[statement._table]
            pairs = [(row.id, row.lock_version) for row in self._rows if row.model == model_name]
            return FakeResult(pairs)
        raise AssertionError("unexpected execute target")

    def add(self, instance):
        self.added.append(instance)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def flush(self):
        pass


class Row(SimpleNamespace):
    pass


@pytest.fixture
def patched(monkeypatch):
    import app.services.snapshots as snapshots_module

    fake_select = lambda *targets: FakeSelect(*targets)  # noqa: E731
    monkeypatch.setattr(diff_module, "select", fake_select)
    monkeypatch.setattr(snapshots_module, "select", fake_select)
    monkeypatch.setattr(diff_module, "func", SimpleNamespace(max=lambda column: ("max", column)))
    yield


# ---- fixtures ---------------------------------------------------------------

def r(model, **kwargs):
    return Row(model=model, active=True, **kwargs)


def base_rows():
    return [
        r("Point", id=1, code="A", name=None, lock_version=1),
        r("Point", id=2, code="B", name=None, lock_version=1),
        r("Point", id=3, code="C", name=None, lock_version=1),
        r("Observation", id=1, project_id=1, line_code="L1", from_point_id=1, to_point_id=2,
          observed_delta_m=1.0, distance_m=1000.0, direction="forward", pair_group=None,
          weight_override=None, lock_version=1),
        r("Observation", id=2, project_id=1, line_code="L2", from_point_id=2, to_point_id=3,
          observed_delta_m=1.0, distance_m=1000.0, direction="forward", pair_group=None,
          weight_override=None, lock_version=1),
        r("Datum", id=1, project_id=1, point_id=1, elevation_m=100.0, sigma_m=0.001, lock_version=1),
        r("WeightRule", id=1, project_id=1, name="mm",
          rule={"method": "millimeter_sqrt_km", "c_km": 1.0}, lock_version=1),
    ]


def _base_payload(rows):
    def rows_of(model):
        return [row for row in rows if row.model == model and row.active]

    return {
        "points": [{"id": p.id, "code": p.code, "name": p.name} for p in rows_of("Point")],
        "observations": [
            {
                "id": o.id, "line_code": o.line_code, "from_point_id": o.from_point_id,
                "to_point_id": o.to_point_id, "observed_delta_m": float(o.observed_delta_m),
                "distance_m": float(o.distance_m), "direction": o.direction,
                "pair_group": o.pair_group,
                "weight_override": None if o.weight_override is None else float(o.weight_override),
            }
            for o in rows_of("Observation")
        ],
        "datums": [
            {"id": d.id, "point_id": d.point_id, "elevation_m": float(d.elevation_m), "sigma_m": float(d.sigma_m)}
            for d in rows_of("Datum")
        ],
        "weight_rules": [{"id": x.id, "name": x.name, "rule": x.rule} for x in rows_of("WeightRule")],
    }


def _versions(rows):
    out: dict[str, dict[int, int]] = {}
    for entity_type, model in (
        ("point", "Point"),
        ("observation", "Observation"),
        ("datum", "Datum"),
        ("weight_rule", "WeightRule"),
    ):
        out[entity_type] = {row.id: int(row.lock_version) for row in rows if row.model == model}
    return out


def _snapshot(snapshot_id, version, payload_obj, versions_map):
    obs_hash = canonical_sha256(payload_obj["observations"])
    rules_hash = canonical_sha256(
        {"datums": payload_obj["datums"], "weight_rules": payload_obj["weight_rules"]}
    )
    return SimpleNamespace(
        id=snapshot_id,
        version=version,
        kind="observations_rules",
        project_id=1,
        observations_sha256=obs_hash,
        rules_sha256=rules_hash,
        input_summary={},
        payload=payload_obj,
        input_versions=versions_map,
        algorithm={},
        immutable=True,
        created_at=None,
    )


# ---- tests ------------------------------------------------------------------

def test_reviewing_diff_is_read_only_and_defaults_to_latest_snapshot(patched):
    b_rows = base_rows()
    base_payload = _base_payload(b_rows)
    base_versions = _versions(b_rows)
    old = _snapshot(11, 1, {**base_payload, "datums": []}, base_versions)
    latest = _snapshot(12, 2, base_payload, base_versions)
    project = SimpleNamespace(id=1, lock_version=7)

    # Draft: L1 deactivated (lock bumped 1 -> 2), weight rule body edited (1 -> 2).
    draft_rows = base_rows()
    l1 = next(row for row in draft_rows if row.model == "Observation" and row.id == 1)
    l1.active = False
    l1.lock_version = 2
    wrule = next(row for row in draft_rows if row.model == "WeightRule" and row.id == 1)
    wrule.rule = {"method": "distance_inverse_km", "c_km": 2.0}
    wrule.lock_version = 2

    db = FakeSession(project, [old, latest], draft_rows)
    report = diff_module.build_draft_diff(db, project_id=1)

    assert report["base_snapshot"]["id"] == 12
    assert report["base_snapshot"]["version"] == 2
    removed = {e["id"]: e for e in report["changes"]["observations"]}
    assert removed[1]["change_type"] == "removed"
    assert removed[1]["base_lock_version"] == 1
    assert removed[1]["draft_lock_version"] == 2
    assert removed[1]["deactivated"] is True
    assert report["topology"]["component_count_base"] == 1
    assert report["topology"]["component_count_draft"] == 2
    assert report["topology"]["bridging_removed_count"] == 1
    assert report["draft"]["project_lock_version"] == 7

    # Read-only invariants.
    assert db.commits == 0
    assert db.added == []
    assert db.rollbacks == 1
    assert project.lock_version == 7
    assert l1.lock_version == 2
    assert wrule.lock_version == 2


def test_repeated_reviews_do_not_advance_versions(patched):
    b_rows = base_rows()
    base_payload = _base_payload(b_rows)
    latest = _snapshot(12, 2, base_payload, _versions(b_rows))
    project = SimpleNamespace(id=1, lock_version=3)

    draft_rows = base_rows()
    wrule = next(row for row in draft_rows if row.model == "WeightRule")
    wrule.rule = {"method": "distance_inverse_km", "c_km": 9.0}
    wrule.lock_version = 5

    db = FakeSession(project, [latest], draft_rows)
    for _ in range(3):
        report = diff_module.build_draft_diff(db, 1)
        assert report["draft"]["project_lock_version"] == 3
        assert report["changes"]["weight_rules"][0]["draft_lock_version"] == 5
        assert report["base_snapshot"]["version"] == 2
    assert db.commits == 0
    assert db.added == []
    assert db.rollbacks == 3
    assert project.lock_version == 3


def test_explicit_snapshot_baseline_and_404_for_other_project(patched):
    b_rows = base_rows()
    snap = _snapshot(12, 2, _base_payload(b_rows), _versions(b_rows))
    snap.project_id = 2
    db = FakeSession(SimpleNamespace(id=1, lock_version=1), [snap], b_rows)
    with pytest.raises(Exception) as exc:
        diff_module.build_draft_diff(db, 1, snapshot_id=12)
    assert exc.value.status_code == 404


def test_missing_first_snapshot_is_409_without_creating_one(patched):
    db = FakeSession(SimpleNamespace(id=1, lock_version=1), [], base_rows())
    with pytest.raises(Exception) as exc:
        diff_module.build_draft_diff(db, 1)
    assert exc.value.status_code == 409
    assert db.added == []
    assert db.commits == 0


def test_http_endpoint_is_get_and_read_only(patched):
    from fastapi.testclient import TestClient

    from app.core.db import get_db
    from app.main import app

    b_rows = base_rows()
    base_payload = _base_payload(b_rows)
    latest = _snapshot(12, 2, base_payload, _versions(b_rows))
    project = SimpleNamespace(id=1, lock_version=4)
    # Draft identical to snapshot: no changes, no noise.
    db = FakeSession(project, [latest], base_rows())

    def override_get_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app)
        response = client.get("/api/projects/1/draft-diff")
        assert response.status_code == 200
        body = response.json()
        assert body["base_snapshot"]["version"] == 2
        assert body["has_changes"] is False
        assert body["draft"]["matches_base_snapshot"] is True
        assert db.commits == 0
        assert db.added == []

        response2 = client.get("/api/projects/1/draft-diff")
        assert response2.status_code == 200
        assert response2.json()["draft"]["project_lock_version"] == 4
        assert project.lock_version == 4
    finally:
        app.dependency_overrides.clear()


def test_payload_lock_version_is_not_part_of_snapshot_hash(patched):
    """A pure optimistic-lock bump with identical inputs must keep hashes equal.

    This protects README invariant #3 (deduplication): the version lives in the
    hash-free input_versions column, never in the hashed payload.
    """
    from app.services.snapshots import _serialize_project_state

    rows = base_rows()
    db = FakeSession(SimpleNamespace(id=1, lock_version=1), [], rows)
    first = _serialize_project_state(1, db)

    # Bump only optimistic versions (no measured/rule input changes).
    for row in rows:
        row.lock_version = int(row.lock_version) + 1
    db2 = FakeSession(SimpleNamespace(id=1, lock_version=2), [], rows)
    second = _serialize_project_state(1, db2)

    assert first["payload"] == second["payload"]
    assert first["observations_hash"] == second["observations_hash"]
    assert first["rules_hash"] == second["rules_hash"]
    assert first["versions"] != second["versions"]
