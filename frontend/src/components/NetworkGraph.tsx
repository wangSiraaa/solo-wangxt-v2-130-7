import { useEffect, useRef } from 'react';
import cytoscape, { Core, CoreLayoutOptions, ElementDefinition } from 'cytoscape';

interface Props {
  elements: ElementDefinition[];
  /** Endpoint codes of re-wired observations, marked as modified. */
  highlight?: {
    modifiedNodeCodes?: string[];
  };
}

export function NetworkGraph({ elements, highlight }: Props) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const cyRef = useRef<Core | null>(null);

  useEffect(() => {
    if (!containerRef.current) return;
    const cy = cytoscape({
      container: containerRef.current,
      elements,
      style: [
        {
          selector: 'node',
          style: {
            label: 'data(label)',
            'background-color': '#2563eb',
            color: '#0f172a',
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
        {
          selector: 'node[diffTag = "added"]',
          style: {
            'background-color': '#16a34a',
            'border-width': 3,
            'border-color': '#86efac',
            width: 24,
            height: 24
          }
        },
        {
          selector: 'node[diffTag = "removed"]',
          style: {
            'background-color': '#dc2626',
            'border-style': 'dashed',
            'border-width': 3,
            'border-color': '#fca5a5',
            opacity: 0.8
          }
        },
        {
          selector: 'edge[diffTag = "added"]',
          style: {
            'line-color': '#16a34a',
            'target-arrow-color': '#16a34a',
            width: 3
          }
        },
        {
          selector: 'edge[diffTag = "removed"]',
          style: {
            'line-color': '#dc2626',
            'target-arrow-color': '#dc2626',
            'line-style': 'dashed',
            width: 3,
            opacity: 0.85
          }
        },
        {
          selector: 'edge.diff-modified',
          style: {
            'line-color': '#f59e0b',
            'target-arrow-color': '#f59e0b',
            width: 3
          }
        },
        {
          selector: 'node.diff-modified',
          style: {
            'background-color': '#f59e0b',
            'border-width': 3,
            'border-color': '#fde68a',
            width: 24,
            height: 24
          }
        }
      ],
      layout: { name: 'cose', animate: false, nodeRepulsion: 8000 } as CoreLayoutOptions
    });
    cyRef.current = cy;
    return () => cy.destroy();
  }, [elements]);

  // Re-wired endpoints are marked after layout; the other change states are
  // carried by the immutable element data (diffTag) set during review build.
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.batch(() => {
      cy.elements().removeClass('diff-modified');
      if (!highlight) return;
      (highlight.modifiedNodeCodes ?? []).forEach((code) => cy.getElementById(code).addClass('diff-modified'));
    });
  }, [highlight]);

  return <div ref={containerRef} style={{ height: 520, border: '1px solid #cbd5e1', borderRadius: 8 }} />;
}
