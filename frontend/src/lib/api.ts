const API_BASE = '';

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`${response.status}: ${text}`);
  }
  return response.json();
}

export interface Stage {
  name: string;
  status: string;
  attempt: number;
  detail: Record<string, unknown>;
}

export interface Job {
  id: number;
  status: string;
  current_stage: string;
  generation_key: string;
  snapshot_version: number;
  input_summary: Record<string, number | string>;
  algorithm: Record<string, unknown>;
  diagnostics: Record<string, any>;
  stages: Stage[];
}

export interface ResidualRow {
  line_code: string;
  observed_delta_m: number;
  adjusted_delta_m: number | null;
  correction_m: number | null;
  residual: number | null;
}

export type ChangeType = 'added' | 'removed' | 'modified';

export interface FieldChange {
  field: string;
  before: unknown;
  after: unknown;
}

export interface DiffEntry {
  entity_type: 'point' | 'observation' | 'datum' | 'weight_rule';
  change_type: ChangeType;
  id: number;
  base_lock_version: number | null;
  draft_lock_version: number | null;
  fields: FieldChange[];
  code?: string;
  name?: string | null;
  line_code?: string;
  point_id?: number;
  point_code?: string | null;
  from_point_id?: number;
  to_point_id?: number;
  from_code?: string | null;
  to_code?: string | null;
}

export interface DiffCounts {
  added: number;
  removed: number;
  modified: number;
  total: number;
}

export interface TopologyNodeChange {
  change_type: ChangeType;
  id: number;
  code: string;
  name?: string | null;
  lock_version?: number;
}

export interface TopologyEdgeChange {
  change_type: ChangeType;
  observation_id: number;
  line_code: string;
  from_point_id: number;
  to_point_id: number;
  from_code: string;
  to_code: string;
  endpoints_survive: boolean;
  bridging_removed: boolean;
}

export interface DraftDiff {
  base_snapshot: {
    id: number;
    version: number;
    kind: string;
    observations_sha256: string;
    rules_sha256: string;
    created_at: string | null;
    immutable: boolean;
  };
  draft: {
    project_lock_version: number;
    observations_sha256: string;
    rules_sha256: string;
    input_summary: Record<string, number | string>;
    matches_base_snapshot: boolean;
  };
  changes: {
    points: DiffEntry[];
    observations: DiffEntry[];
    datums: DiffEntry[];
    weight_rules: DiffEntry[];
  };
  counts: Record<'points' | 'observations' | 'datums' | 'weight_rules', DiffCounts>;
  topology: {
    nodes: { added: TopologyNodeChange[]; removed: TopologyNodeChange[] };
    edges: { added: TopologyEdgeChange[]; removed: TopologyEdgeChange[] };
    component_count_base: number;
    component_count_draft: number;
    bridging_removed_count: number;
    topology_changed: boolean;
  };
  weight_only: boolean;
  has_changes: boolean;
}

export async function fetchDraftDiff(projectId: number, snapshotId?: number): Promise<DraftDiff> {
  const query = snapshotId ? `?snapshot_id=${snapshotId}` : '';
  return api<DraftDiff>(`/api/projects/${projectId}/draft-diff${query}`);
}
