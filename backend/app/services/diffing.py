"""Snapshot-vs-draft input diffing.

The review query compares the *current draft* against an immutable snapshot
without creating a Job and without writing anything. Matching always uses the
stable numeric primary key (``id``) — never ``code``/``line_code`` — so two
different points that happen to share a name/code are never paired up.

Payload shape (see ``services.snapshots._serialize_project_state``)::

    {"points": [...], "observations": [...], "datums": [...], "weight_rules": [...]}

Only fields listed below participate in the comparison; ``lock_version`` is
metadata carried alongside each row for display, not a compared field.
"""
from __future__ import annotations

from typing import Any

# Compared fields per entity, in display order.
POINT_FIELDS: tuple[str, ...] = ("code", "name")
OBSERVATION_FIELDS: tuple[str, ...] = (
    "line_code",
    "from_point_id",
    "to_point_id",
    "observed_delta_m",
    "distance_m",
    "direction",
    "pair_group",
    "weight_override",
)
DATUM_FIELDS: tuple[str, ...] = ("point_id", "elevation_m", "sigma_m")
RULE_FIELDS: tuple[str, ...] = ("name", "rule")

# Numeric columns are persisted as Numeric(18,9)/Numeric(15,3); tolerate only
# sub-storage float round-trip noise so real 1 mm edits always show up.
_FLOAT_FIELDS = frozenset({"observed_delta_m", "distance_m", "elevation_m", "sigma_m", "weight_override"})
_FLOAT_EPSILON = 5.0e-10

_STRUCTURAL_EDGE_FIELDS = frozenset({"from_point_id", "to_point_id"})


def _values_equal(field: str, before: Any, after: Any) -> bool:
    if field in _FLOAT_FIELDS:
        if before is None or after is None:
            return before is None and after is None
        try:
            return abs(float(before) - float(after)) <= _FLOAT_EPSILON
        except (TypeError, ValueError):
            return before == after
    return before == after


def _row_identity(entity: str, row: dict[str, Any], point_codes: dict[int, str]) -> str:
    if entity == "points":
        return str(row.get("code") or f"point:{row['id']}")
    if entity == "observations":
        source = point_codes.get(int(row["from_point_id"]), f"#{row['from_point_id']}")
        target = point_codes.get(int(row["to_point_id"]), f"#{row['to_point_id']}")
        return f"{row.get('line_code', '?')} ({source}→{target})"
    if entity == "datums":
        fallback = f"#{row['point_id']}"
        return f"基准 {point_codes.get(int(row['point_id']), fallback)}"
    return str(row.get("name") or f"rule:{row['id']}")


def _diff_entities(
    entity: str,
    base_rows: list[dict[str, Any]],
    draft_rows: list[dict[str, Any]],
    fields: tuple[str, ...],
    point_codes: dict[int, str],
) -> dict[str, list[dict[str, Any]]]:
    # Match strictly on the stable numeric id. Same-name rows with different ids
    # (e.g. a re-surveyed benchmark reusing a code) must never be paired here.
    base_by_id = {int(row["id"]): row for row in base_rows}
    draft_by_id = {int(row["id"]): row for row in draft_rows}

    added: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    modified: list[dict[str, Any]] = []

    for row_id in sorted(draft_by_id.keys() - base_by_id.keys()):
        row = draft_by_id[row_id]
        added.append(
            {
                "id": row_id,
                "label": _row_identity(entity, row, point_codes),
                "lock_version": row.get("lock_version"),
                "base_lock_version": None,
                "fields": {field: row.get(field) for field in fields},
            }
        )
    for row_id in sorted(base_by_id.keys() - draft_by_id.keys()):
        row = base_by_id[row_id]
        removed.append(
            {
                "id": row_id,
                "label": _row_identity(entity, row, point_codes),
                "lock_version": None,
                "base_lock_version": row.get("lock_version"),
                "fields": {field: row.get(field) for field in fields},
            }
        )
    for row_id in sorted(base_by_id.keys() & draft_by_id.keys()):
        before_row, after_row = base_by_id[row_id], draft_by_id[row_id]
        changes = [
            {"field": field, "before": before_row.get(field), "after": after_row.get(field)}
            for field in fields
            if not _values_equal(field, before_row.get(field), after_row.get(field))
        ]
        if changes:
            modified.append(
                {
                    "id": row_id,
                    "label": _row_identity(entity, after_row, point_codes),
                    "lock_version": after_row.get("lock_version"),
                    "base_lock_version": before_row.get("lock_version"),
                    "changes": changes,
                }
            )
    return {"added": added, "removed": removed, "modified": modified}


def _edge_view(row: dict[str, Any], point_codes: dict[int, str], change_fields: list[str] | None = None) -> dict[str, Any]:
    view = {
        "observation_id": int(row["id"]),
        "line_code": row.get("line_code"),
        "source": point_codes.get(int(row["from_point_id"])),
        "target": point_codes.get(int(row["to_point_id"])),
    }
    if change_fields is not None:
        view["change_fields"] = change_fields
        view["structural"] = bool(_STRUCTURAL_EDGE_FIELDS.intersection(change_fields))
    return view


def _topology_diff(
    base: dict[str, Any],
    draft: dict[str, Any],
    obs_diff: dict[str, list[dict[str, Any]]],
    point_diff: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    base_codes = {int(p["id"]): p.get("code") for p in base["points"]}
    draft_codes = {int(p["id"]): p.get("code") for p in draft["points"]}
    # Draft geometry wins for surviving points; base codes remain for removed ones.
    codes = {**{pid: code for pid, code in base_codes.items() if code is not None}, **{pid: code for pid, code in draft_codes.items() if code is not None}}
    base_obs = {int(o["id"]): o for o in base["observations"]}
    draft_obs = {int(o["id"]): o for o in draft["observations"]}

    base_points_by_id = {int(p["id"]): p for p in base["points"]}
    draft_points_by_id = {int(p["id"]): p for p in draft["points"]}

    def node_view(row: dict[str, Any]) -> dict[str, Any]:
        return {"point_id": int(row["id"]), "code": row.get("code")}

    nodes = {
        "added": [node_view(draft_points_by_id[entry["id"]]) for entry in point_diff["added"]],
        "removed": [node_view(base_points_by_id[entry["id"]]) for entry in point_diff["removed"]],
        "modified": [
            {
                "point_id": entry["id"],
                "code": draft_codes.get(entry["id"]) or base_codes.get(entry["id"]),
                "change_fields": [change["field"] for change in entry["changes"]],
            }
            for entry in point_diff["modified"]
        ],
    }

    edges = {
        "added": [
            _edge_view(draft_obs[entry["id"]], codes)
            for entry in obs_diff["added"]
        ],
        "removed": [
            _edge_view(base_obs[entry["id"]], codes)
            for entry in obs_diff["removed"]
        ],
        "modified": [
            _edge_view(
                draft_obs[entry["id"]],
                codes,
                [change["field"] for change in entry["changes"]],
            )
            for entry in obs_diff["modified"]
        ],
    }
    return {"nodes": nodes, "edges": edges}


def _empty_payload() -> dict[str, Any]:
    return {"points": [], "observations": [], "datums": [], "weight_rules": []}


def _change_count(diff: dict[str, list[dict[str, Any]]]) -> int:
    return len(diff["added"]) + len(diff["removed"]) + len(diff["modified"])


def diff_payloads(base: dict[str, Any] | None, draft: dict[str, Any]) -> dict[str, Any]:
    """Pure, side-effect-free comparison of two serialized project states."""
    base = base or _empty_payload()
    base_codes = {int(p["id"]): p.get("code") for p in base.get("points", [])}
    draft_codes = {int(p["id"]): p.get("code") for p in draft.get("points", [])}
    codes = {**{pid: code for pid, code in base_codes.items() if code is not None}, **{pid: code for pid, code in draft_codes.items() if code is not None}}

    points = _diff_entities("points", base["points"], draft["points"], POINT_FIELDS, codes)
    observations = _diff_entities(
        "observations", base["observations"], draft["observations"], OBSERVATION_FIELDS, codes
    )
    datums = _diff_entities("datums", base["datums"], draft["datums"], DATUM_FIELDS, codes)
    weight_rules = _diff_entities(
        "weight_rules", base["weight_rules"], draft["weight_rules"], RULE_FIELDS, codes
    )
    topology = _topology_diff(base, draft, observations, points)

    totals = {
        "points": _change_count(points),
        "observations": _change_count(observations),
        "datums": _change_count(datums),
        "weight_rules": _change_count(weight_rules),
    }
    totals["total"] = sum(totals.values())
    # True only when the surveyor touched weight rules and nothing else — the
    # topology graph then stays completely unhighlighted.
    totals["weight_rules_only"] = (
        totals["total"] > 0
        and totals["points"] == 0
        and totals["observations"] == 0
        and totals["datums"] == 0
        and totals["weight_rules"] > 0
    )
    totals["topology_changed"] = bool(
        topology["nodes"]["added"]
        or topology["nodes"]["removed"]
        or topology["edges"]["added"]
        or topology["edges"]["removed"]
        or any(edge.get("structural") for edge in topology["edges"]["modified"])
    )

    return {
        "points": points,
        "observations": observations,
        "datums": datums,
        "weight_rules": weight_rules,
        "topology": topology,
        "totals": totals,
    }
