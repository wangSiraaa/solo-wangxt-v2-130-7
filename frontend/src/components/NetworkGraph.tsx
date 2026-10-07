import { useEffect, useRef } from 'react';
import cytoscape, { Core, CoreLayoutOptions, ElementDefinition } from 'cytoscape';
import type { TopologyDiff } from '../lib/api';

interface Props {
  elements: ElementDefinition[];
  diff?: TopologyDiff | null;
}

/**
 * Merge snapshot-draft topology changes into the graph elements.
 *
 * Surviving nodes/edges keep their stable graph ids (point code / line code);
 * removed rows that are no longer in the draft are injected as ghost elements
 * so the surveyor can still see what disappeared (e.g. a deleted bridge chord).
 */
export function mergeTopologyDiff(
  elements: ElementDefinition[],
  diff: TopologyDiff | null | undefined
): ElementDefinition[] {
  if (!diff) return elements.map((element) => ({ ...element, classes: '' }));

  const nodeClasses = new Map<string, string[]>();
  const edgeClasses = new Map<string, string[]>();

  for (const node of diff.nodes.added) nodeClasses.set(node.code ?? `p${node.point_id}`, ['diff-added']);
  for (const node of diff.nodes.removed) nodeClasses.set(node.code ?? `p${node.point_id}`, ['diff-removed', 'diff-ghost']);
  for (const node of diff.nodes.modified) nodeClasses.set(node.code ?? `p${node.point_id}`, ['diff-modified']);

  for (const edge of diff.edges.added) edgeClasses.set(edge.line_code, ['diff-added']);
  for (const edge of diff.edges.removed) edgeClasses.set(edge.line_code, ['diff-removed', 'diff-ghost']);
  for (const edge of diff.edges.modified)
    edgeClasses.set(edge.line_code, [edge.structural ? 'diff-modified' : 'diff-value']);

  const presentNodeIds = new Set<string>();
  const presentEdgeIds = new Set<string>();
  const merged: ElementDefinition[] = elements.map((element) => {
    const data = element.data as Record<string, unknown>;
    const id = String(data.id);
    const isNode = data.source === undefined;
    if (isNode) {
      presentNodeIds.add(id);
      return { ...element, classes: (element.classes as string[] | undefined) ?? nodeClasses.get(id) ?? [] };
    }
    presentEdgeIds.add(id);
    return { ...element, classes: (element.classes as string[] | undefined) ?? edgeClasses.get(id) ?? [] };
  });

  // Ghost nodes for removed points no longer present in the draft topology.
  for (const node of diff.nodes.removed) {
    const code = node.code ?? `p${node.point_id}`;
    if (!presentNodeIds.has(code)) {
      merged.push({ data: { id: code, label: `${code}·删` }, classes: ['diff-removed', 'diff-ghost'] });
    }
  }
  // Ghost edges for removed/deactivated segments no longer present.
  for (const edge of diff.edges.removed) {
    if (!presentEdgeIds.has(edge.line_code)) {
      merged.push({
        data: { id: edge.line_code, source: edge.source ?? `p?`, target: edge.target ?? `p?`, label: '已删除' },
        classes: ['diff-removed', 'diff-ghost']
      });
    }
  }
  return merged;
}

export function NetworkGraph({ elements, diff }: Props) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const cyRef = useRef<Core | null>(null);

  useEffect(() => {
    if (!containerRef.current) return;
    const cyElements = mergeTopologyDiff(elements, diff);
    const cy = cytoscape({
      container: containerRef.current,
      elements: cyElements,
      style: [
        {
          selector: 'node',
          style: {
            label: 'data(label)',
            'background-color': '#2563eb',
            color: '#fff',
            'font-size': 10,
            'text-valign': 'bottom',
            'text-margin-y': 6,
            width: 18,
            height: 18
          }
        },
        {
          selector: 'edge',
          style: {
            label: 'data(label)',
            width: 1.2,
            'line-color': '#64748b',
            'target-arrow-color': '#64748b',
            'target-arrow-shape': 'triangle',
            'curve-style': 'bezier',
            'font-size': 8
          }
        },
        // Snapshot-draft review highlights.
        { selector: 'node.diff-added', style: { 'background-color': '#16a34a', 'border-width': 3, 'border-color': '#16a34a' } },
        { selector: 'edge.diff-added', style: { 'line-color': '#16a34a', 'target-arrow-color': '#16a34a', width: 3 } },
        { selector: 'node.diff-modified', style: { 'background-color': '#f59e0b', 'border-width': 3, 'border-color': '#f59e0b' } },
        { selector: 'edge.diff-modified', style: { 'line-color': '#f59e0b', 'target-arrow-color': '#f59e0b', width: 3 } },
        { selector: 'edge.diff-value', style: { 'line-style': 'dashed', 'line-color': '#f59e0b', 'target-arrow-color': '#f59e0b', width: 2.5 } },
        { selector: 'node.diff-removed', style: { 'background-color': '#dc2626', 'border-width': 3, 'border-color': '#dc2626' } },
        { selector: 'edge.diff-removed', style: { 'line-color': '#dc2626', 'target-arrow-color': '#dc2626', width: 3 } },
        {
          selector: '.diff-ghost',
          style: { opacity: 0.65, 'line-style': 'dotted', 'background-opacity': 0.55, color: '#dc2626' }
        }
      ],
      layout: { name: 'cose', animate: false, nodeRepulsion: 8000 } as CoreLayoutOptions
    });
    cyRef.current = cy;
    return () => cy.destroy();
  }, [elements, diff]);

  return <div ref={containerRef} style={{ height: 520, border: '1px solid #cbd5e1', borderRadius: 8 }} />;
}
