declare module 'cytoscape' {
  export interface ElementDefinition {
    data: Record<string, unknown>;
  }

  export interface CoreLayoutOptions {
    name: string;
    [key: string]: unknown;
  }

  export interface Collection {
    addClass(className: string): Collection;
    removeClass(className: string): Collection;
  }

  export interface Core {
    destroy(): void;
    batch(callback: () => void): void;
    elements(): Collection;
    getElementById(id: string): Collection;
  }

  interface CytoscapeOptions {
    container: HTMLElement;
    elements: ElementDefinition[];
    style: unknown[];
    layout: CoreLayoutOptions;
  }

  export default function cytoscape(options: CytoscapeOptions): Core;
}
