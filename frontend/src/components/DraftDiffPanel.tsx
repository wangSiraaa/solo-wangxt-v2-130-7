import type { DiffEntry, DraftDiff } from '../lib/api';

const FIELD_LABELS: Record<string, string> = {
  code: '测点代码',
  name: '名称',
  observed_delta_m: '观测高差 (m)',
  distance_m: '长度 (m)',
  direction: '方向',
  pair_group: '往返对组',
  weight_override: '权重覆盖',
  from_point_id: '起点',
  to_point_id: '终点',
  elevation_m: '基准高程 (m)',
  sigma_m: '基准中误差 (m)',
  point_id: '基准点',
  rule: '权重规则',
  line_code: '测段编码'
};

function formatValue(value: unknown): string {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

function entryTitle(entry: DiffEntry): string {
  if (entry.entity_type === 'point') return `测点 #${entry.id} ${entry.code ?? ''}`;
  if (entry.entity_type === 'observation') {
    return `测段 ${entry.line_code ?? `#${entry.id}`}（${entry.from_code ?? entry.from_point_id} → ${
      entry.to_code ?? entry.to_point_id
    }）`;
  }
  if (entry.entity_type === 'datum') {
    return `基准 #${entry.id}（${entry.point_code ?? `点#${entry.point_id}`}）`;
  }
  return `权重规则 #${entry.id} ${entry.name ?? ''}`;
}

function changeBadge(type: DiffEntry['change_type']) {
  const text = type === 'added' ? '新增' : type === 'removed' ? '删除' : '修改';
  return <span className={`diff-badge diff-${type}`}>{text}</span>;
}

function EntryRow({ entry }: { entry: DiffEntry }) {
  return (
    <li className={`diff-entry diff-entry-${entry.change_type}`}>
      <div className="diff-entry-head">
        {changeBadge(entry.change_type)}
        <span className="diff-entry-title">{entryTitle(entry)}</span>
        <span className="diff-versions">
          版本 v{entry.base_lock_version ?? '—'} → v{entry.draft_lock_version ?? '—'}
        </span>
      </div>
      {entry.fields.length > 0 && (
        <ul className="diff-fields">
          {entry.fields.map((field) => (
            <li key={field.field}>
              <span className="diff-field-name">{FIELD_LABELS[field.field] ?? field.field}</span>
              <span className="diff-before">{formatValue(field.before)}</span>
              <span className="diff-arrow">→</span>
              <span className="diff-after">{formatValue(field.after)}</span>
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}

function Section({ title, entries }: { title: string; entries: DiffEntry[] }) {
  if (entries.length === 0) return null;
  return (
    <div className="diff-section">
      <h4>
        {title} <span className="diff-count">{entries.length}</span>
      </h4>
      <ul className="diff-list">
        {entries.map((entry) => (
          <EntryRow key={`${entry.entity_type}-${entry.id}`} entry={entry} />
        ))}
      </ul>
    </div>
  );
}

export function DraftDiffPanel({ diff }: { diff: DraftDiff }) {
  const summary = diff.draft.input_summary;
  const topo = diff.topology;
  const c = diff.counts;
  return (
    <section className="card diff-review">
      <h2>草稿—快照输入差异复核（只读，不提交 Job）</h2>
      <p className="diff-subtitle">
        基线快照 <strong>v{diff.base_snapshot.version}</strong>（#
        {diff.base_snapshot.id}，不可变） · 草稿锁版本 <strong>v{diff.draft.project_lock_version}</strong>
      </p>

      {diff.draft.matches_base_snapshot ? (
        <div className="diff-empty">草稿与基线快照输入完全一致：提交将合并到既有代次，不会产生新快照。</div>
      ) : diff.weight_only ? (
        <div className="diff-notice diff-notice-weight">
          仅权重规则/权重覆盖发生变化：测点、基准、观测高差、长度与拓扑均无变化，拓扑图不标记结构差异。
        </div>
      ) : (
        <div className="diff-notice">下列输入差异将在提交时生成新的不可变快照版本。</div>
      )}

      <div className="diff-summary-grid">
        <div className="diff-summary-item">
          <span>测点</span>
          <strong>{String(summary.point_count ?? '—')}</strong>
          <small>
            +{c.points.added} / −{c.points.removed} / ✎{c.points.modified}
          </small>
        </div>
        <div className="diff-summary-item">
          <span>观测测段</span>
          <strong>{String(summary.observation_count ?? '—')}</strong>
          <small>
            +{c.observations.added} / −{c.observations.removed} / ✎{c.observations.modified}
          </small>
        </div>
        <div className="diff-summary-item">
          <span>基准</span>
          <strong>{String(summary.datum_count ?? '—')}</strong>
          <small>
            +{c.datums.added} / −{c.datums.removed} / ✎{c.datums.modified}
          </small>
        </div>
        <div className="diff-summary-item">
          <span>权重规则</span>
          <strong>{String(summary.weight_rule_count ?? '—')}</strong>
          <small>
            +{c.weight_rules.added} / −{c.weight_rules.removed} / ✎{c.weight_rules.modified}
          </small>
        </div>
        <div className="diff-summary-item">
          <span>观测总长度</span>
          <strong>{typeof summary.distance_total_m === 'number' ? `${summary.distance_total_m.toFixed(1)} m` : '—'}</strong>
          <small>连通分量 {topo.component_count_base} → {topo.component_count_draft}</small>
        </div>
        <div className="diff-summary-item">
          <span>删除的桥接测段</span>
          <strong className={topo.bridging_removed_count ? 'residual-bad' : ''}>
            {topo.bridging_removed_count}
          </strong>
          <small>{topo.topology_changed ? '拓扑发生变化' : '拓扑无变化'}</small>
        </div>
      </div>

      <div className="diff-legend">
        <span><i className="legend-dot legend-added" />新增</span>
        <span><i className="legend-dot legend-modified" />修改</span>
        <span><i className="legend-dot legend-removed" />删除（虚线）</span>
      </div>

      <Section title="测点" entries={diff.changes.points} />
      <Section title="基准" entries={diff.changes.datums} />
      <Section title="观测测段（高差/长度/拓扑/权重覆盖）" entries={diff.changes.observations} />
      <Section title="权重规则" entries={diff.changes.weight_rules} />
    </section>
  );
}
