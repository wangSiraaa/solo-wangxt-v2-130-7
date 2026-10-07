import { useEffect, useMemo, useState } from 'react';
import type { ElementDefinition } from 'cytoscape';
import { api, fetchDraftDiff, type DraftDiff, type Job, type ResidualRow } from './lib/api';
import { NetworkGraph } from './components/NetworkGraph';
import { StageTracker } from './components/StageTracker';
import { ResidualTable } from './components/ResidualTable';
import { DraftDiffPanel } from './components/DraftDiffPanel';
import './styles.css';

export default function App() {
  const [projectId, setProjectId] = useState(1);
  const [elements, setElements] = useState<ElementDefinition[]>([]);
  const [job, setJob] = useState<Job | null>(null);
  const [residuals, setResiduals] = useState<ResidualRow[]>([]);
  const [diff, setDiff] = useState<DraftDiff | null>(null);
  const [reviewing, setReviewing] = useState(false);
  const [message, setMessage] = useState('');

  useEffect(() => {
    setDiff(null);
    api<{ nodes: unknown[]; edges: unknown[] }>(`/api/projects/${projectId}/topology`)
      .then((data) => setElements([...(data.nodes as ElementDefinition[]), ...(data.edges as ElementDefinition[])]))
      .catch((error) => setMessage(error.message));
  }, [projectId]);

  useEffect(() => {
    if (!job || ['completed', 'failed'].includes(job.status)) return;
    const timer = window.setInterval(async () => {
      const next = await api<Job>(`/api/jobs/${job!.id}`);
      setJob(next);
    }, 1500);
    return () => window.clearInterval(timer);
  }, [job]);

  // Inject ghost nodes/edges for removed topology and compute highlight tags.
  // The /topology graph identifies nodes by their stable point *code* (unique
  // per project); the review matches entities by numeric stable id and supplies
  // the codes needed to attach ghosts to the live graph. Draft edges keep their
  // stable line_code id; removed edges become `edel:{obsId}` ghosts.
  const { graphElements, highlight } = useMemo(() => {
    if (!diff) {
      return { graphElements: elements, highlight: undefined };
    }
    const topo = diff.topology;
    const addedNodeCodes = new Set(topo.nodes.added.map((n) => n.code));
    const removedNodeCodes = new Set(topo.nodes.removed.map((n) => n.code));
    const addedEdgeByObs = new Map(topo.edges.added.map((e) => [e.observation_id, e]));
    const removedEdgeByObs = new Map(topo.edges.removed.map((e) => [e.observation_id, e]));

    const liveObsIds = new Set<number>();
    const liveNodeCodes = new Set<string>();
    for (const el of elements) {
      if (el.data.source) {
        if (el.data.obs_id !== undefined) liveObsIds.add(Number(el.data.obs_id));
      } else {
        liveNodeCodes.add(String(el.data.id));
      }
    }

    const modifiedNodeCodes = new Set<string>();
    const modifiedLiveLineCodes = new Set<string>();
    for (const entry of diff.changes.observations) {
      if (entry.change_type !== 'modified') continue;
      const rewired = entry.fields.some(
        (field) => field.field === 'from_point_id' || field.field === 'to_point_id'
      );
      if (rewired) {
        if (entry.from_code) modifiedNodeCodes.add(entry.from_code);
        if (entry.to_code) modifiedNodeCodes.add(entry.to_code);
      } else if (
        entry.line_code &&
        entry.fields.some((field) =>
          ['observed_delta_m', 'distance_m', 'direction', 'pair_group', 'weight_override'].includes(
            field.field
          )
        )
      ) {
        modifiedLiveLineCodes.add(entry.line_code);
      }
    }

    // Tag live elements (added nodes/edges are usually already live after a
    // page refresh; deactivated rows are absent from the live graph).
    const tagged: ElementDefinition[] = elements.map((el) => {
      if (!el.data.source) {
        const code = String(el.data.id);
        const tag = addedNodeCodes.has(code)
          ? 'added'
          : removedNodeCodes.has(code)
            ? 'removed'
            : undefined;
        return tag ? { data: { ...el.data, diffTag: tag } } : el;
      }
      const obsId = el.data.obs_id !== undefined ? Number(el.data.obs_id) : undefined;
      let tag: string | undefined;
      if (obsId !== undefined && addedEdgeByObs.has(obsId)) tag = 'added';
      else if (obsId !== undefined && removedEdgeByObs.has(obsId)) tag = 'removed';
      else if (modifiedLiveLineCodes.has(String(el.data.id))) tag = 'modified';
      return tag ? { data: { ...el.data, diffTag: tag } } : el;
    });

    const ghosts: ElementDefinition[] = [];
    for (const node of topo.nodes.removed) {
      if (!liveNodeCodes.has(node.code)) {
        ghosts.push({
          data: { id: node.code, label: `${node.code} (删)`, point_id: node.id, diffTag: 'removed' }
        });
      }
    }
    for (const edge of topo.edges.added) {
      if (!liveObsIds.has(edge.observation_id)) {
        ghosts.push({
          data: {
            id: `eadd:${edge.observation_id}`,
            source: edge.from_code,
            target: edge.to_code,
            label: `${edge.line_code} (新)`,
            diffTag: 'added'
          }
        });
      }
    }
    for (const edge of topo.edges.removed) {
      if (!liveObsIds.has(edge.observation_id)) {
        ghosts.push({
          data: {
            id: `edel:${edge.observation_id}`,
            source: edge.from_code,
            target: edge.to_code,
            label: `${edge.line_code} (删)`,
            diffTag: 'removed'
          }
        });
      }
    }

    return {
      graphElements: [...tagged, ...ghosts],
      highlight: {
        modifiedNodeCodes: [...modifiedNodeCodes]
      }
    };
  }, [elements, diff]);

  async function submitSnapshot() {
    setMessage('创建不可变快照并提交唯一任务代次...');
    const result = await api<{ job_id: number; deduplicated: boolean; snapshot_version: number }>(
      `/api/projects/${projectId}/jobs`,
      { method: 'POST' }
    );
    setMessage(result.deduplicated ? '重复提交已合并到既有代次' : `已启动快照 v${result.snapshot_version}`);
    const detail = await api<Job>(`/api/jobs/${result.job_id}`);
    setJob(detail);
  }

  async function reviewDiff() {
    setReviewing(true);
    setMessage('正在只读复核草稿与上一份快照的输入差异...');
    try {
      const result = await fetchDraftDiff(projectId);
      setDiff(result);
      setMessage(
        result.draft.matches_base_snapshot
          ? '草稿与上一份快照一致：无需新快照'
          : result.weight_only
            ? '差异复核完成：仅权重规则变化（不生成 Job）'
            : '差异复核完成（只读，未提交 Job、未推进锁版本）'
      );
    } catch (error) {
      setDiff(null);
      setMessage((error as Error).message);
    } finally {
      setReviewing(false);
    }
  }

  async function resume() {
    if (!job) return;
    await api(`/api/jobs/${job.id}/resume`, { method: 'POST' });
    setMessage('已从最后一个已确认阶段恢复');
  }

  async function publish() {
    if (!job) return;
    try {
      const result = await api<{ publication_id: number; version: number }>(`/api/jobs/${job.id}/publish`, {
        method: 'POST',
        body: JSON.stringify({ confirm: true })
      });
      setMessage(`已发布成果版本 v${result.version}`);
    } catch (error) {
      setMessage((error as Error).message);
    }
  }

  async function loadResiduals() {
    if (!job) return;
    setResiduals(await api<ResidualRow[]>(`/api/jobs/${job.id}/residuals?limit=100`));
  }

  return (
    <main>
      <header>
        <h1>省级水准网成果平台</h1>
        <p>不可变观测/规则快照 · 稀疏加权最小二乘 · QR秩诊断 · 旧任务只审计不覆盖新草稿</p>
      </header>

      <section className="toolbar">
        <label>
          项目 ID
          <input value={projectId} onChange={(event) => setProjectId(Number(event.target.value))} type="number" />
        </label>
        <button onClick={reviewDiff} disabled={reviewing}>
          {reviewing ? '复核中...' : '复核草稿差异（不提交）'}
        </button>
        <button onClick={submitSnapshot}>提交当前草稿快照</button>
        <button onClick={resume} disabled={!job}>
          从确认阶段恢复
        </button>
        <button onClick={loadResiduals} disabled={!job}>
          查看残差
        </button>
        <button onClick={publish} disabled={job?.status !== 'completed'} className="primary">
          发布成果
        </button>
      </section>

      {message && <div className="message">{message}</div>}

      {diff && <DraftDiffPanel diff={diff} />}

      <section className="grid">
        <div className="card">
          <h2>测点拓扑 / 问题子网{diff ? '（绿色新增 · 橙色修改 · 红色虚线删除）' : ''}</h2>
          <NetworkGraph elements={graphElements} highlight={highlight} />
        </div>
        <div className="card">
          <h2>任务阶段</h2>
          {job ? (
            <>
              <StageTracker stages={job.stages} />
              <dl className="facts">
                <dt>状态</dt>
                <dd>{job.status}</dd>
                <dt>快照版本</dt>
                <dd>v{job.snapshot_version}</dd>
                <dt>分量数</dt>
                <dd>{String(job.diagnostics?.component_count ?? '—')}</dd>
                <dt>阻塞分量</dt>
                <dd>{String(job.diagnostics?.blocked_components?.length ?? 0)}</dd>
                <dt>算法</dt>
                <dd>{String(job.algorithm.signature)}</dd>
                <dt>正则化</dt>
                <dd className="strong">禁止：{String(job.diagnostics?.regularization ?? 'none')}</dd>
              </dl>
            </>
          ) : (
            <p>提交快照后显示代次和阶段进度。差异复核不会创建或改变任何 Job。</p>
          )}
        </div>
      </section>

      {residuals.length > 0 && (
        <section className="card">
          <h2>改正数与残差追踪</h2>
          <ResidualTable rows={residuals} />
        </section>
      )}
    </main>
  );
}
