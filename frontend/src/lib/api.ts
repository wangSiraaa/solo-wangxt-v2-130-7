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

export type DiffOp = 'added' | 'removed' | 'modified';

export interface FieldChange {
  field: string;
  before: unknown;
  after: unknown;
}

export interface DiffEntry {
  id: number;
  label: string;
  lock_version: number | null;
  base_lock_version: number | null;
  fields?: Record<string, unknown>;
  changes?: FieldChange[];
}

export interface EntityDiff {
  added: DiffEntry[];
  removed: DiffEntry[];
  modified: DiffEntry[];
}

export interface TopologyEdgeChange {
  observation_id: number;
  line_code: string;
  source: string | null;
  target: string | null;
  change_fields?: string[];
  structural?: boolean;
}

export interface TopologyNodeChange {
  point_id: number;
  code: string | null;
  change_fields?: string[];
}

export interface TopologyDiff {
  nodes: { added: TopologyNodeChange[]; removed: TopologyNodeChange[]; modified: TopologyNodeChange[] };
  edges: { added: TopologyEdgeChange[]; removed: TopologyEdgeChange[]; modified: TopologyEdgeChange[] };
}

export interface DraftSummary {
  point_count: number;
  observation_count: number;
  datum_count: number;
  weight_rule_count: number;
  distance_total_m: number;
  observed_delta_sha256: string;
  payload_sha256: string;
}

export interface DraftDiff {
  base_snapshot: {
    id: number;
    version: number;
    created_at: string;
    observations_sha256: string;
    rules_sha256: string;
    input_summary: Record<string, unknown>;
  } | null;
  project: { id: number; code: string; lock_version: number };
  points: EntityDiff;
  observations: EntityDiff;
  datums: EntityDiff;
  weight_rules: EntityDiff;
  topology: TopologyDiff;
  totals: {
    points: number;
    observations: number;
    datums: number;
    weight_rules: number;
    total: number;
    weight_rules_only: boolean;
    topology_changed: boolean;
  };
  draft_summary: DraftSummary;
  query_side_effect: string;
}

