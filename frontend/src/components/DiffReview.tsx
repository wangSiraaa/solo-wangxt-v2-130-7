import type { DraftDiff, EntityDiff, FieldChange } from '../lib/api';

const FIELD_LABELS: Record<string, string> = {
  code: '点号',
  name: '点记',
  line_code: '测段编号',
  from_point_id: '起点 ID',
  to_point_id: '终点 ID',
  observed_delta_m: '观测高差 (m)',
  distance_m: '长度 (m)',
  direction: '方向',
  pair_group: '往返组',
  weight_override: '权重覆写',
  point_id: '测点 ID',
  elevation_m: '基准高程 (m)',
  sigma_m: '基准中误差 σ (m)',
  name_rule: '规则名称',
  rule: '权重规则'
};

const OP_LABEL: Record<string, string> = {
  added: '新增',
  removed: '删除',
  modified: '修改'
};

function formatValue(value: unknown): string {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'number') return Number.isInteger(value) ? String(value) : value.toFixed(6).replace(/0+$/, '').replace(/\.$/, '');
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

function VersionTag({ lockVersion, baseVersion }: { lockVersion: number | null; baseVersion: number | null }) {
  return (
    <span className="diff-version">
      {baseVersion !== null && <span className="tag tag-base">快照 v{baseVersion}</span>}
      {lockVersion !== null && <span className="tag tag-draft">草稿 v{lockVersion}</span>}
    </span>
  );
}

function DiffGroup({ title, entity, diff }: { title: string; entity: string; diff: EntityDiff }) {
  const empty = diff.added.length === 0 && diff.removed.length === 0 && diff.modified.length === 0;
  if (empty) return null;

  const rows: { op: 'added' | 'removed' | 'modified'; entry: (typeof diff.added)[number] }[] = [
    ...diff.added.map((entry) => ({ op: 'added' as const, entry })),
    ...diff.removed.map((entry) => ({ op: 'removed' as const, entry })),
    ...diff.modified.map((entry) => ({ op: 'modified' as const, entry }))
  ];

  return (
    <div className="diff-group">
      <h4>{title}</h4>
      <table className="diff-table">
        <tbody>
          {rows.map(({ op, entry }) => (
            <tr key={`${op}-${entry.id}`} className={`diff-row diff-${op}`}>
              <td className="diff-op">
                <span className={`badge badge-${op}`}>{OP_LABEL[op]}</span>
              </td>
              <td className="diff-label">
                <span className="diff-stable-id">#{entry.id}</span> {entry.label}
                <VersionTag lockVersion={entry.lock_version} baseVersion={entry.base_lock_version} />
              </td>
              <td className="diff-detail">
                {op === 'modified' && entry.changes ? (
                  <ul className="change-list">
                    {entry.changes.map((change: FieldChange) => (
                      <li key={change.field}>
                        {FIELD_LABELS[change.field] ?? change.field}:{' '}
                        <span className="val-before">{formatValue(change.before)}</span>
                        <span className="change-arrow"> → </span>
                        <span className="val-after">{formatValue(change.after)}</span>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <span className="diff-fields">
                    {entry.fields
                      ? Object.entries(entry.fields)
                          .filter(([, value]) => value !== null && value !== undefined)
                          .map(([field, value]) => `${FIELD_LABELS[field] ?? field}=${formatValue(value)}`)
                          .join('　')
                      : ''}
                  </span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function DiffReview({ diff }: { diff: DraftDiff }) {
  const base = diff.base_snapshot;
  return (
    <div className="diff-review">
      <div className="diff-header">
        <h3>快照 — 草稿差异复核</h3>
        <p className="diff-meta">
          {base ? (
            <>
              基准：上一份快照 <strong>v{base.version}</strong>（#{base.id}，{new Date(base.created_at).toLocaleString()}） ·
              项目草稿锁 <strong>v{diff.project.lock_version}</strong>
            </>
          ) : (
            <>尚无快照：当前全部草稿输入均为新增 · 项目草稿锁 v{diff.project.lock_version}</>
          )}
        </p>
        <p className="diff-side-effect">只读复核：不生成 Job、不改变旧快照、不推进任何锁版本。</p>
      </div>

      <div className="diff-summary">
        <h4>新草稿输入摘要</h4>
        <dl className="facts">
          <dt>测点</dt>
          <dd>{diff.draft_summary.point_count}</dd>
          <dt>活动测段</dt>
          <dd>{diff.draft_summary.observation_count}</dd>
          <dt>基准</dt>
          <dd>{diff.draft_summary.datum_count}</dd>
          <dt>权重规则</dt>
          <dd>{diff.draft_summary.weight_rule_count}</dd>
          <dt>总长度</dt>
          <dd>{diff.draft_summary.distance_total_m.toFixed(3)} m</dd>
          <dt>输入 SHA-256</dt>
          <dd className="hash">{diff.draft_summary.payload_sha256.slice(0, 16)}…</dd>
        </dl>
      </div>

      {diff.totals.total === 0 ? (
        <p className="diff-empty">草稿与上一份快照没有输入差异。</p>
      ) : diff.totals.weight_rules_only ? (
        <p className="diff-notice">本次仅调整权重规则，测点与测段拓扑无变化（拓扑图不高亮）。</p>
      ) : (
        <p className="diff-notice">
          共 {diff.totals.total} 项输入变化：测点 {diff.totals.points}、测段 {diff.totals.observations}、基准{' '}
          {diff.totals.datums}、权重规则 {diff.totals.weight_rules}。
          {diff.totals.topology_changed ? ' 拓扑已在图中高亮：绿=新增，红=删除，橙=改动。' : ''}
        </p>
      )}

      <DiffGroup title="测点" entity="points" diff={diff.points} />
      <DiffGroup title="基准" entity="datums" diff={diff.datums} />
      <DiffGroup title="观测高差 / 长度（测段）" entity="observations" diff={diff.observations} />
      <DiffGroup title="权重规则" entity="weight_rules" diff={diff.weight_rules} />
    </div>
  );
}
