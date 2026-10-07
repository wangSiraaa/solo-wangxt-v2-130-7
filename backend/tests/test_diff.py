from types import SimpleNamespace

from app.services.diff import (
    ADDED,
    MODIFIED,
    REMOVED,
    build_diff_report,
    compute_entity_diff,
    compute_topology_diff,
)


def point(pid, code, name=None):
    return {"id": pid, "code": code, "name": name}


def observation(
    oid,
    line_code,
    from_point_id,
    to_point_id,
    observed_delta_m=1.0,
    distance_m=1000.0,
    direction="forward",
    pair_group=None,
    weight_override=None,
):
    return {
        "id": oid,
        "line_code": line_code,
        "from_point_id": from_point_id,
        "to_point_id": to_point_id,
        "observed_delta_m": observed_delta_m,
        "distance_m": distance_m,
        "direction": direction,
        "pair_group": pair_group,
        "weight_override": weight_override,
    }


def datum(did, point_id, elevation_m=100.0, sigma_m=0.001):
    return {"id": did, "point_id": point_id, "elevation_m": elevation_m, "sigma_m": sigma_m}


def rule(rid, name="mm", body=None):
    return {"id": rid, "name": name, "rule": body or {"method": "millimeter_sqrt_km", "c_km": 1.0}}


def payload(points=None, observations=None, datums=None, weight_rules=None):
    return {
        "points": points or [],
        "observations": observations or [],
        "datums": datums or [],
        "weight_rules": weight_rules or [],
    }


def versions(points=None, observations=None, datums=None, weight_rules=None):
    """Build an entity -> {id: lock_version} map for the pure diff functions."""
    return {
        "point": dict(points or {}),
        "observation": dict(observations or {}),
        "datum": dict(datums or {}),
        "weight_rule": dict(weight_rules or {}),
    }


def snapshot(payload_obj, version=1, snapshot_id=10, input_versions=None):
    return SimpleNamespace(
        id=snapshot_id,
        version=version,
        kind="observations_rules",
        observations_sha256="base-obs-hash",
        rules_sha256="base-rules-hash",
        created_at=None,
        immutable=True,
        payload=payload_obj,
        input_versions=input_versions,
    )


def draft_state(payload_obj, observations_hash="draft-obs-hash", rules_hash="draft-rules-hash", versions_map=None):
    summary = {
        "point_count": len(payload_obj["points"]),
        "observation_count": len(payload_obj["observations"]),
        "datum_count": len(payload_obj["datums"]),
        "weight_rule_count": len(payload_obj["weight_rules"]),
        "distance_total_m": sum(o["distance_m"] for o in payload_obj["observations"]),
    }
    state = {
        "payload": payload_obj,
        "observations_hash": observations_hash,
        "rules_hash": rules_hash,
        "summary": summary,
    }
    if versions_map is not None:
        state["versions"] = versions_map
    return state


def _by_id(entries):
    return {e["id"]: e for e in entries}


def test_unchanged_draft_has_no_changes():
    state = payload(
        points=[point(1, "A"), point(2, "B")],
        observations=[observation(1, "L1", 1, 2)],
        datums=[datum(1, 1)],
        weight_rules=[rule(1)],
    )
    vers = versions(points={1: 1, 2: 1}, observations={1: 1}, datums={1: 1}, weight_rules={1: 1})
    snap = snapshot(state, input_versions=vers)
    report = build_diff_report(snap, draft_state(state, "base-obs-hash", "base-rules-hash", vers), 1)
    assert report["has_changes"] is False
    assert report["weight_only"] is False
    assert report["draft"]["matches_base_snapshot"] is True
    assert report["topology"]["topology_changed"] is False
    assert all(section["total"] == 0 for section in report["counts"].values())


def test_only_weight_rule_touched_shows_only_rule_change():
    base = payload(
        points=[point(1, "A"), point(2, "B")],
        observations=[observation(1, "L1", 1, 2)],
        datums=[datum(1, 1)],
        weight_rules=[rule(1, body={"method": "millimeter_sqrt_km", "c_km": 1.0})],
    )
    draft = payload(
        points=[point(1, "A"), point(2, "B")],
        observations=[observation(1, "L1", 1, 2)],
        datums=[datum(1, 1)],
        weight_rules=[rule(1, body={"method": "distance_inverse_km", "c_km": 2.5})],
    )
    base_vers = versions(points={1: 1, 2: 1}, observations={1: 1}, datums={1: 1}, weight_rules={1: 1})
    draft_vers = versions(points={1: 1, 2: 1}, observations={1: 1}, datums={1: 1}, weight_rules={1: 2})
    report = build_diff_report(snapshot(base, input_versions=base_vers), draft_state(draft, versions_map=draft_vers), 1)
    assert report["weight_only"] is True
    rules = report["changes"]["weight_rules"]
    assert len(rules) == 1
    assert rules[0]["change_type"] == MODIFIED
    assert rules[0]["base_lock_version"] == 1
    assert rules[0]["draft_lock_version"] == 2
    changed = {f["field"] for f in rules[0]["fields"]}
    assert changed == {"rule"}
    # No geometry, datum or point noise.
    assert report["changes"]["points"] == []
    assert report["changes"]["observations"] == []
    assert report["changes"]["datums"] == []
    assert report["topology"]["topology_changed"] is False


def test_weight_override_only_is_weight_only():
    base = payload(points=[point(1, "A"), point(2, "B")], observations=[observation(1, "L1", 1, 2)])
    draft = payload(
        points=[point(1, "A"), point(2, "B")],
        observations=[observation(1, "L1", 1, 2, weight_override=0.5)],
    )
    draft_vers = versions(points={1: 1, 2: 1}, observations={1: 2})
    report = build_diff_report(snapshot(base), draft_state(draft, versions_map=draft_vers), 1)
    assert report["weight_only"] is True
    obs = report["changes"]["observations"]
    assert len(obs) == 1
    assert [f["field"] for f in obs[0]["fields"]] == ["weight_override"]
    assert obs[0]["draft_lock_version"] == 2


def test_delta_and_length_modifications_are_reported_per_field():
    points = [point(1, "A"), point(2, "B")]
    base = payload(points=points, observations=[observation(1, "L1", 1, 2, observed_delta_m=1.0, distance_m=1000.0)])
    draft = payload(
        points=points,
        observations=[observation(1, "L1", 1, 2, observed_delta_m=1.004, distance_m=1200.0)],
    )
    base_vers = versions(points={1: 1, 2: 1}, observations={1: 1})
    draft_vers = versions(points={1: 1, 2: 1}, observations={1: 3})
    changes = compute_entity_diff(base, draft, base_vers, draft_vers)["observations"]
    entry = _by_id(changes)[1]
    assert entry["change_type"] == MODIFIED
    assert entry["base_lock_version"] == 1 and entry["draft_lock_version"] == 3
    fields = {f["field"]: (f["before"], f["after"]) for f in entry["fields"]}
    assert fields["observed_delta_m"] == (1.0, 1.004)
    assert fields["distance_m"] == (1000.0, 1200.0)
    report = build_diff_report(snapshot(base, input_versions=base_vers), draft_state(draft, versions_map=draft_vers), 1)
    assert report["weight_only"] is False


def test_deleting_bridging_observation_splits_topology():
    # A - B - C with L1 bridging {A,B} and {C}: removing L1 leaves two components.
    points = [point(1, "A"), point(2, "B"), point(3, "C")]
    base = payload(
        points=points,
        observations=[observation(1, "L1", 1, 2), observation(2, "L2", 2, 3)],
    )
    draft = payload(points=points, observations=[observation(2, "L2", 2, 3)])
    base_vers = versions(points={1: 1, 2: 1, 3: 1}, observations={1: 1, 2: 1})
    # Deactivated L1 still exists in the draft table at lock version 2.
    draft_vers = versions(points={1: 1, 2: 1, 3: 1}, observations={1: 2, 2: 1})

    topo = compute_topology_diff(base, draft, base_vers, draft_vers)
    assert topo["component_count_base"] == 1
    assert topo["component_count_draft"] == 2
    assert topo["topology_changed"] is True
    removed = topo["edges"]["removed"]
    assert len(removed) == 1
    assert removed[0]["line_code"] == "L1"
    assert removed[0]["bridging_removed"] is True
    assert removed[0]["endpoints_survive"] is True
    assert removed[0]["draft_lock_version"] == 2
    assert topo["bridging_removed_count"] == 1

    changes = compute_entity_diff(base, draft, base_vers, draft_vers)["observations"]
    assert _by_id(changes)[1]["change_type"] == REMOVED
    assert _by_id(changes)[1]["base_lock_version"] == 1
    assert _by_id(changes)[1]["draft_lock_version"] == 2


def test_hard_deleted_bridge_has_no_draft_version():
    points = [point(1, "A"), point(2, "B"), point(3, "C")]
    base = payload(points=points, observations=[observation(1, "L1", 1, 2), observation(2, "L2", 2, 3)])
    draft = payload(points=points, observations=[observation(2, "L2", 2, 3)])
    # Hard delete: the deactivated row is gone too, so no draft version for L1.
    draft_vers = versions(points={1: 1, 2: 1, 3: 1}, observations={2: 1})
    changes = compute_entity_diff(base, draft, None, draft_vers)["observations"]
    assert _by_id(changes)[1]["draft_lock_version"] is None
    assert _by_id(changes)[1].get("deactivated") is None


def test_removing_non_bridging_edge_keeps_component_count():
    points = [point(1, "A"), point(2, "B"), point(3, "C")]
    base = payload(
        points=points,
        observations=[
            observation(1, "L1", 1, 2),
            observation(2, "L2", 2, 3),
            observation(3, "L3", 1, 3),
        ],
    )
    draft = payload(
        points=points,
        observations=[observation(1, "L1", 1, 2), observation(2, "L2", 2, 3)],
    )
    topo = compute_topology_diff(base, draft)
    removed = topo["edges"]["removed"]
    assert len(removed) == 1
    assert removed[0]["bridging_removed"] is False
    assert topo["component_count_base"] == topo["component_count_draft"] == 1
    assert topo["topology_changed"] is True


def test_same_code_reused_after_delete_is_add_plus_remove_not_modification():
    # Point id=1 code "X" is deleted; a brand-new point reuses code "X" with id=9.
    base = payload(points=[point(1, "X"), point(2, "Y")])
    draft = payload(points=[point(2, "Y"), point(9, "X", name="new X")])
    changes = compute_entity_diff(base, draft)["points"]
    types = {e["id"]: e["change_type"] for e in changes}
    assert types == {1: REMOVED, 9: ADDED}
    assert not any(e["change_type"] == MODIFIED for e in changes)
    added = next(e for e in changes if e["id"] == 9)
    assert added["base_lock_version"] is None and added["draft_lock_version"] is None
    removed = next(e for e in changes if e["id"] == 1)
    assert removed["base_lock_version"] is None and removed["draft_lock_version"] is None


def test_same_line_code_reused_is_not_paired_with_old_segment():
    # Old L1 (1->2) removed; new L1 (3->4) inserted as a different stable id.
    points = [point(1, "A"), point(2, "B"), point(3, "C"), point(4, "D")]
    base = payload(points=points, observations=[observation(1, "L1", 1, 2)])
    draft = payload(points=points, observations=[observation(7, "L1", 3, 4)])
    changes = compute_entity_diff(base, draft)["observations"]
    types = {e["id"]: e["change_type"] for e in changes}
    assert types == {1: REMOVED, 7: ADDED}
    topo = compute_topology_diff(base, draft)
    # One ghost edge removed, one brand-new edge added — never a modification.
    assert {e["observation_id"]: e["change_type"] for e in topo["edges"]["removed"]} == {1: REMOVED}
    assert {e["observation_id"]: e["change_type"] for e in topo["edges"]["added"]} == {7: ADDED}


def test_datum_change_is_reported_not_weight_only():
    base = payload(points=[point(1, "A")], datums=[datum(1, 1, elevation_m=100.0)])
    draft = payload(points=[point(1, "A")], datums=[datum(1, 1, elevation_m=100.01)])
    base_vers = versions(points={1: 1}, datums={1: 1})
    draft_vers = versions(points={1: 1}, datums={1: 2})
    report = build_diff_report(snapshot(base, input_versions=base_vers), draft_state(draft, versions_map=draft_vers), 1)
    assert report["weight_only"] is False
    entry = report["changes"]["datums"][0]
    assert entry["change_type"] == MODIFIED
    assert [f["field"] for f in entry["fields"]] == ["elevation_m"]
    assert entry["point_id"] == 1
    assert entry["draft_lock_version"] == 2


def test_added_point_and_observation_have_identity():
    base = payload(points=[point(1, "A")], observations=[])
    draft = payload(
        points=[point(1, "A"), point(2, "B")],
        observations=[observation(5, "L9", 1, 2)],
    )
    draft_vers = versions(points={1: 1, 2: 1}, observations={5: 1})
    changes = compute_entity_diff(base, draft, None, draft_vers)
    point_added = _by_id(changes["points"])[2]
    assert point_added["change_type"] == ADDED
    assert point_added["code"] == "B"
    obs_added = _by_id(changes["observations"])[5]
    assert obs_added["change_type"] == ADDED
    assert obs_added["from_code"] == "A" and obs_added["to_code"] == "B"
    topo = compute_topology_diff(base, draft, None, draft_vers)
    assert [n["code"] for n in topo["nodes"]["added"]] == ["B"]
    assert len(topo["edges"]["added"]) == 1


def test_snapshot_versions_are_listed_for_modified_observation():
    points = [point(1, "A"), point(2, "B")]
    base = payload(points=points, observations=[observation(1, "L1", 1, 2, observed_delta_m=2.0)])
    draft = payload(points=points, observations=[observation(1, "L1", 1, 2, observed_delta_m=2.002)])
    base_vers = versions(points={1: 1, 2: 1}, observations={1: 4})
    draft_vers = versions(points={1: 1, 2: 1}, observations={1: 6})
    entry = compute_entity_diff(base, draft, base_vers, draft_vers)["observations"][0]
    assert entry["base_lock_version"] == 4
    assert entry["draft_lock_version"] == 6


def test_renaming_rule_and_body_together_is_weight_only():
    base = payload(weight_rules=[rule(1, "mm", {"method": "millimeter_sqrt_km", "c_km": 1.0})])
    draft = payload(weight_rules=[rule(1, "renamed", {"method": "distance_inverse_km", "c_km": 2.0})])
    report = build_diff_report(snapshot(base), draft_state(draft), 1)
    assert report["weight_only"] is True


def test_renaming_rule_without_body_change_is_not_weight_only():
    body = {"method": "millimeter_sqrt_km", "c_km": 1.0}
    base = payload(weight_rules=[rule(1, "mm", body)])
    draft = payload(weight_rules=[rule(1, "renamed-mm", body)])
    report = build_diff_report(snapshot(base), draft_state(draft), 1)
    assert report["weight_only"] is False
    assert [f["field"] for f in report["changes"]["weight_rules"][0]["fields"]] == ["name"]
