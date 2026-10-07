"""Snapshot-vs-draft input diff review.

Read-only comparison between the latest immutable snapshot (or an explicitly
chosen one) and the current draft inputs. The diff never creates a Job, never
creates a snapshot and never mutates lock versions — it only serializes the
draft state and compares it against the snapshot payload.

Stable identity rule
--------------------
Every entity is matched by its stable primary key (``id``), never by its
human-readable code. A deleted point/observation and a later-created one that
happen to reuse the same ``code``/``line_code`` are therefore reported as one
removal plus one addition, never silently paired as a "modification".
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.schema import (
    Datum,
    Observation,
    Point,
    Project,
    Snapshot,
    WeightRule,
)
from app.services.network import build_components
from app.services.snapshots import _serialize_project_state

ADDED = "added"
REMOVED = "removed"
MODIFIED = "modified"

POINT_FIELDS = ("code", "name")
# Geometry-bearing / measured inputs. weight_override is a weight input, not a
# measured height difference, so it is tracked as its own field (the graph only
# highlights topology/geometry field changes).
OBSERVATION_FIELDS = (
    "line_code",
    "from_point_id",
    "to_point_id",
    "observed_delta_m",
    "distance_m",
    "direction",
    "pair_group",
    "weight_override",
)
DATUM_FIELDS = ("point_id", "elevation_m", "sigma_m")
RULE_FIELDS = ("name", "rule")
# Fields whose change actually alters weighting (as opposed to renaming a rule).
WEIGHT_INPUT_FIELDS = frozenset({"rule", "weight_override"})


def _index(rows: Iterable[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {int(row["id"]): row for row in rows}


def _field_changes(old: dict[str, Any], new: dict[str, Any], fields: tuple[str, ...]) -> list[dict[str, Any]]:
    """Per-field comparison with strict equality.

    Both sides are already JSON-normalized (Decimal -> float) so a value that
    was not edited compares equal and never produces review noise.
    """
    changes: list[dict[str, Any]] = []
    for field in fields:
        before = old.get(field)
        after = new.get(field)
        if before != after:
            changes.append({"field": field, "before": before, "after": after})
    return changes


def _versions(base_version: int | None, draft_version: int | None) -> dict[str, int | None]:
    return {
        "base_lock_version": base_version,
        "draft_lock_version": draft_version,
    }


def _diff_entity(
    entity_type: str,
    base_rows: list[dict[str, Any]],
    draft_rows: list[dict[str, Any]],
    fields: tuple[str, ...],
    identity: Any,
    base_versions: dict[int, int] | None = None,
    draft_versions: dict[int, int] | None = None,
) -> list[dict[str, Any]]:
    base = _index(base_rows)
    draft = _index(draft_rows)
    base_versions = base_versions or {}
    draft_versions = draft_versions or {}
    entries: list[dict[str, Any]] = []
    for entity_id in sorted(set(base) | set(draft)):
        old = base.get(entity_id)
        new = draft.get(entity_id)
        if old is not None and new is None:
            change_type = REMOVED
            changed_fields: list[dict[str, Any]] = []
        elif old is None and new is not None:
            change_type = ADDED
            changed_fields = []
        else:
            changed_fields = _field_changes(old, new, fields)
            if not changed_fields:
                continue
            change_type = MODIFIED
        entry: dict[str, Any] = {
            "entity_type": entity_type,
            "change_type": change_type,
            "id": entity_id,
            **_versions(base_versions.get(entity_id), draft_versions.get(entity_id)),
            "fields": changed_fields,
        }
        entry.update(identity(old, new, entity_id))
        entries.append(entry)
    return entries


def _point_codes(payload: dict[str, Any]) -> dict[int, str]:
    return {int(p["id"]): p["code"] for p in payload.get("points", [])}


def _point_identity(base_payload: dict[str, Any], draft_payload: dict[str, Any]):
    def identity(old: dict[str, Any] | None, new: dict[str, Any] | None, entity_id: int) -> dict[str, Any]:
        row = new or old or {}
        return {"code": row.get("code"), "name": row.get("name")}

    return identity


def _observation_identity(base_payload: dict[str, Any], draft_payload: dict[str, Any]):
    base_codes = _point_codes(base_payload)
    draft_codes = _point_codes(draft_payload)

    def identity(old: dict[str, Any] | None, new: dict[str, Any] | None, entity_id: int) -> dict[str, Any]:
        row = new or old or {}
        codes = draft_codes if new is not None else base_codes
        return {
            "line_code": row.get("line_code"),
            "from_point_id": row.get("from_point_id"),
            "to_point_id": row.get("to_point_id"),
            "from_code": codes.get(row.get("from_point_id")) if row.get("from_point_id") is not None else None,
            "to_code": codes.get(row.get("to_point_id")) if row.get("to_point_id") is not None else None,
        }

    return identity


def _datum_identity(base_payload: dict[str, Any], draft_payload: dict[str, Any]):
    base_codes = _point_codes(base_payload)
    draft_codes = _point_codes(draft_payload)

    def identity(old: dict[str, Any] | None, new: dict[str, Any] | None, entity_id: int) -> dict[str, Any]:
        row = new or old or {}
        codes = draft_codes if new is not None else base_codes
        point_id = row.get("point_id")
        return {"point_id": point_id, "point_code": codes.get(point_id) if point_id is not None else None}

    return identity


def _rule_identity(base_payload: dict[str, Any], draft_payload: dict[str, Any]):
    def identity(old: dict[str, Any] | None, new: dict[str, Any] | None, entity_id: int) -> dict[str, Any]:
        row = new or old or {}
        return {"name": row.get("name")}

    return identity


def compute_entity_diff(
    base_payload: dict[str, Any],
    draft_payload: dict[str, Any],
    base_versions: dict[str, dict[int, int]] | None = None,
    draft_versions: dict[str, dict[int, int]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Compare points/datums/observations/weight-rules by stable id."""
    base_versions = base_versions or {}
    draft_versions = draft_versions or {}
    points = _diff_entity(
        "point",
        base_payload.get("points", []),
        draft_payload.get("points", []),
        POINT_FIELDS,
        _point_identity(base_payload, draft_payload),
        base_versions.get("point", {}),
        draft_versions.get("point", {}),
    )
    observations = _diff_entity(
        "observation",
        base_payload.get("observations", []),
        draft_payload.get("observations", []),
        OBSERVATION_FIELDS,
        _observation_identity(base_payload, draft_payload),
        base_versions.get("observation", {}),
        draft_versions.get("observation", {}),
    )
    datums = _diff_entity(
        "datum",
        base_payload.get("datums", []),
        draft_payload.get("datums", []),
        DATUM_FIELDS,
        _datum_identity(base_payload, draft_payload),
        base_versions.get("datum", {}),
        draft_versions.get("datum", {}),
    )
    weight_rules = _diff_entity(
        "weight_rule",
        base_payload.get("weight_rules", []),
        draft_payload.get("weight_rules", []),
        RULE_FIELDS,
        _rule_identity(base_payload, draft_payload),
        base_versions.get("weight_rule", {}),
        draft_versions.get("weight_rule", {}),
    )
    return {"points": points, "observations": observations, "datums": datums, "weight_rules": weight_rules}


def _edge_set(payload: dict[str, Any]) -> dict[tuple[int, int], dict[str, Any]]:
    edges: dict[tuple[int, int], dict[str, Any]] = {}
    for obs in payload.get("observations", []):
        key = (int(obs["from_point_id"]), int(obs["to_point_id"]))
        edges[key] = obs
    return edges


def _component_map(payload: dict[str, Any]) -> dict[int, int]:
    """Map point id -> connected component index for active observations."""
    points = [{"id": int(p["id"])} for p in payload.get("points", [])]
    observations = [
        {"from_point_id": int(o["from_point_id"]), "to_point_id": int(o["to_point_id"])}
        for o in payload.get("observations", [])
    ]
    components = build_components(points, observations)
    mapping: dict[int, int] = {}
    for index, local_indices in enumerate(components):
        for local_index in local_indices:
            mapping[int(points[local_index]["id"])] = index
    return mapping


def compute_topology_diff(
    base_payload: dict[str, Any],
    draft_payload: dict[str, Any],
    base_versions: dict[str, dict[int, int]] | None = None,
    draft_versions: dict[str, dict[int, int]] | None = None,
) -> dict[str, Any]:
    """Graph-level review: added/removed nodes and edges, component split/merge.

    A removed observation whose endpoints survive but no longer belong to the
    same draft component is flagged as a removed bridging segment.
    """
    base_versions = base_versions or {}
    draft_versions = draft_versions or {}
    base_point_versions = base_versions.get("point", {})
    draft_point_versions = draft_versions.get("point", {})
    base_obs_versions = base_versions.get("observation", {})
    draft_obs_versions = draft_versions.get("observation", {})

    base_nodes = {int(p["id"]): p for p in base_payload.get("points", [])}
    draft_nodes = {int(p["id"]): p for p in draft_payload.get("points", [])}
    base_codes = _point_codes(base_payload)
    draft_codes = _point_codes(draft_payload)
    base_edges = _edge_set(base_payload)
    draft_edges = _edge_set(draft_payload)

    def node_entry(point_id: int, change_type: str) -> dict[str, Any]:
        row = draft_nodes.get(point_id) or base_nodes.get(point_id) or {}
        return {
            "change_type": change_type,
            "id": point_id,
            "code": row.get("code"),
            "name": row.get("name"),
            "base_lock_version": base_point_versions.get(point_id),
            "draft_lock_version": draft_point_versions.get(point_id),
        }

    added_nodes = [node_entry(pid, ADDED) for pid in sorted(set(draft_nodes) - set(base_nodes))]
    removed_nodes = [node_entry(pid, REMOVED) for pid in sorted(set(base_nodes) - set(draft_nodes))]

    # Re-wired observations (same stable id, different endpoints) show up here
    # as an edge removal plus an edge addition, in addition to their per-field
    # modification entry.
    edge_keys_added = sorted(set(draft_edges) - set(base_edges))
    edge_keys_removed = sorted(set(base_edges) - set(draft_edges))

    draft_components = _component_map(draft_payload)

    def edge_entry(key: tuple[int, int], obs: dict[str, Any], change_type: str, removed: bool) -> dict[str, Any]:
        obs_id = int(obs["id"])
        a, b = key
        codes = base_codes if removed else draft_codes
        survives = a in draft_nodes and b in draft_nodes
        was_bridging = bool(survives and draft_components.get(a) != draft_components.get(b))
        return {
            "change_type": change_type,
            "observation_id": obs_id,
            "line_code": obs.get("line_code"),
            "from_point_id": a,
            "to_point_id": b,
            "from_code": codes.get(a),
            "to_code": codes.get(b),
            "endpoints_survive": survives,
            "bridging_removed": was_bridging,
            "base_lock_version": base_obs_versions.get(obs_id),
            "draft_lock_version": draft_obs_versions.get(obs_id),
        }

    added_edges = [edge_entry(key, draft_edges[key], ADDED, removed=False) for key in edge_keys_added]
    removed_edges = [edge_entry(key, base_edges[key], REMOVED, removed=True) for key in edge_keys_removed]

    base_component_count = len(_component_groups(base_payload))
    draft_component_count = len(_component_groups(draft_payload))

    return {
        "nodes": {"added": added_nodes, "removed": removed_nodes},
        "edges": {"added": added_edges, "removed": removed_edges},
        "component_count_base": base_component_count,
        "component_count_draft": draft_component_count,
        "bridging_removed_count": sum(1 for edge in removed_edges if edge["bridging_removed"]),
        "topology_changed": bool(
            added_nodes or removed_nodes or added_edges or removed_edges
            or base_component_count != draft_component_count
        ),
    }


def _component_groups(payload: dict[str, Any]) -> list[list[int]]:
    points = [{"id": int(p["id"])} for p in payload.get("points", [])]
    observations = [
        {"from_point_id": int(o["from_point_id"]), "to_point_id": int(o["to_point_id"])}
        for o in payload.get("observations", [])
    ]
    return [[int(points[i]["id"]) for i in group] for group in build_components(points, observations)]


def _count_section(entries: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "added": sum(1 for e in entries if e["change_type"] == ADDED),
        "removed": sum(1 for e in entries if e["change_type"] == REMOVED),
        "modified": sum(1 for e in entries if e["change_type"] == MODIFIED),
        "total": len(entries),
    }


def build_diff_report(
    base_snapshot: Snapshot,
    draft_state: dict[str, Any],
    project_lock_version: int,
    draft_versions_all: dict[str, dict[int, int]] | None = None,
) -> dict[str, Any]:
    """Pure assembly of the review response from a snapshot and serialized draft.

    ``draft_versions_all`` includes every draft row (active and deactivated), so
    removed/deactivated entities still carry their post-deactivation version.
    """
    base_payload = base_snapshot.payload
    draft_payload = draft_state["payload"]
    # Snapshot-era versions live in a separate, hash-free JSONB column. Older
    # snapshots predating that column simply report None on the base side.
    base_versions = base_snapshot.input_versions or {}
    draft_versions = draft_versions_all or draft_state.get("versions", {})

    changes = compute_entity_diff(base_payload, draft_payload, base_versions, draft_versions)
    topology = compute_topology_diff(base_payload, draft_payload, base_versions, draft_versions)

    # Flag removals that are draft-table deactivations rather than hard deletes.
    for entries in changes.values():
        for entry in entries:
            if entry["change_type"] == REMOVED and entry.get("draft_lock_version") is not None:
                entry["deactivated"] = True

    counts = {section: _count_section(entries) for section, entries in changes.items()}

    def has_structural_field(entry: dict[str, Any]) -> bool:
        # Weight-rule edits (including add/remove) never restructure geometry,
        # datums or measured inputs — they are weighting changes by definition.
        if entry["entity_type"] == "weight_rule":
            return False
        # On an observation everything except weight_override is structural
        # (measured height difference, length, geometry, code, direction).
        structural = {
            "line_code", "from_point_id", "to_point_id", "observed_delta_m",
            "distance_m", "direction", "pair_group",
        }
        if entry["change_type"] != MODIFIED:
            return True  # added/removed observations restructure inputs
        return any(change["field"] in structural for change in entry.get("fields", []))

    structural_change_total = counts["points"]["total"] + counts["datums"]["total"] + sum(
        1 for entry in [*changes["weight_rules"], *changes["observations"]] if has_structural_field(entry)
    )
    weight_input_total = sum(
        1
        for entry in [*changes["weight_rules"], *changes["observations"]]
        if any(change["field"] in WEIGHT_INPUT_FIELDS for change in entry.get("fields", []))
    )
    weight_only = structural_change_total == 0 and weight_input_total > 0

    total_changes = sum(section["total"] for section in counts.values())
    return {
        "base_snapshot": {
            "id": base_snapshot.id,
            "version": base_snapshot.version,
            "kind": base_snapshot.kind,
            "observations_sha256": base_snapshot.observations_sha256,
            "rules_sha256": base_snapshot.rules_sha256,
            "created_at": base_snapshot.created_at.isoformat() if isinstance(base_snapshot.created_at, datetime) else base_snapshot.created_at,
            "immutable": bool(base_snapshot.immutable),
        },
        "draft": {
            "project_lock_version": project_lock_version,
            "observations_sha256": draft_state["observations_hash"],
            "rules_sha256": draft_state["rules_hash"],
            "input_summary": draft_state["summary"],
            "matches_base_snapshot": (
                draft_state["observations_hash"] == base_snapshot.observations_sha256
                and draft_state["rules_hash"] == base_snapshot.rules_sha256
            ),
        },
        "changes": changes,
        "counts": counts,
        "topology": topology,
        "weight_only": bool(weight_only),
        "has_changes": total_changes > 0,
    }


def get_base_snapshot(db: Session, project_id: int, snapshot_id: int | None) -> Snapshot:
    """Resolve the comparison baseline: the given snapshot or the latest one.

    Read-only: issuing a review never creates the missing first snapshot.
    """
    if snapshot_id is not None:
        snapshot = db.get(Snapshot, snapshot_id)
        if snapshot is None or snapshot.project_id != project_id:
            raise HTTPException(404, f"snapshot {snapshot_id} not found for project {project_id}")
        return snapshot
    latest_id = db.scalar(select(func.max(Snapshot.id)).where(Snapshot.project_id == project_id))
    if latest_id is None:
        raise HTTPException(409, {"error": "no_snapshot", "message": "submit the first job to create a snapshot before reviewing drafts"})
    snapshot = db.get(Snapshot, latest_id)
    assert snapshot is not None
    return snapshot


def _all_draft_versions(db: Session, project_id: int) -> dict[str, dict[int, int]]:
    """Lock versions of every draft row (active and deactivated alike).

    Review-only data used for display; it never feeds snapshot hashing.
    """
    versions: dict[str, dict[int, int]] = {}
    for model, entity_type in (
        (Point, "point"),
        (Observation, "observation"),
        (Datum, "datum"),
        (WeightRule, "weight_rule"),
    ):
        result = db.execute(
            select(model.id, model.lock_version).where(model.project_id == project_id)
        )
        versions[entity_type] = {
            int(entity_id): int(lock_version) for entity_id, lock_version in result.all()
        }
    return versions


def build_draft_diff(db: Session, project_id: int, snapshot_id: int | None = None) -> dict[str, Any]:
    """Load baseline + draft and build the review report without any write."""
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, f"project {project_id} not found")
    snapshot = get_base_snapshot(db, project_id, snapshot_id)
    draft_state = _serialize_project_state(project_id, db)
    draft_versions = _all_draft_versions(db, project_id)
    report = build_diff_report(snapshot, draft_state, int(project.lock_version), draft_versions)
    # Explicitly discard anything the session might hold so a review can never
    # advance lock versions, create a snapshot or touch completed Jobs.
    db.rollback()
    return report
