import { Graph, layout } from '@dagrejs/dagre';
import type { LineageEdge, LineageNode, Mapping, ParseResponse } from '@workspace/api-client-react';

export type ParsedSource = { kind: 'file'; file: File } | { kind: 'sample'; name: string };

export type Kind =
  | 'source' | 'qualifier' | 'expression' | 'filter' | 'joiner' | 'lookup' | 'aggregator' | 'target' | 'unknown';

export const KINDS: { kind: Kind; label: string }[] = [
  { kind: 'source', label: 'Source' },
  { kind: 'qualifier', label: 'Source Qualifier' },
  { kind: 'expression', label: 'Expression' },
  { kind: 'filter', label: 'Filter' },
  { kind: 'joiner', label: 'Joiner' },
  { kind: 'lookup', label: 'Lookup' },
  { kind: 'aggregator', label: 'Aggregator' },
  { kind: 'target', label: 'Target' },
  { kind: 'unknown', label: 'Unknown' },
];

export function kindOf(type: string): Kind {
  const t = type.toLowerCase();
  if (t.includes('qualifier')) return 'qualifier';
  if (t.includes('source')) return 'source';
  if (t.includes('target')) return 'target';
  if (t.includes('expression')) return 'expression';
  if (t.includes('filter')) return 'filter';
  if (t.includes('joiner')) return 'joiner';
  if (t.includes('lookup')) return 'lookup';
  if (t.includes('aggregator')) return 'aggregator';
  return 'unknown';
}

export function kindLabel(kind: Kind) {
  return KINDS.find((k) => k.kind === kind)?.label ?? 'Unknown';
}

export function normalizeIdentifier(value: string) {
  return value.toLowerCase();
}

export function orderKey(data: ParseResponse, m: Mapping) {
  const dup = data.mappings.filter((x) => normalizeIdentifier(x.name) === normalizeIdentifier(m.name)).length > 1;
  return dup ? `${m.repository}/${m.folder}/${m.name}` : m.name;
}

export const NODE_W = 196;
export const NODE_H = 58;

export function layoutGraph(nodes: LineageNode[], edges: LineageEdge[]) {
  const g = new Graph();
  g.setGraph({ rankdir: 'LR', nodesep: 26, ranksep: 74, marginx: 20, marginy: 20 });
  g.setDefaultEdgeLabel(() => ({}));
  nodes.forEach((n) => g.setNode(n.id, { width: NODE_W, height: NODE_H }));
  edges.forEach((e) => { if (g.hasNode(e.source) && g.hasNode(e.target)) g.setEdge(e.source, e.target); });
  layout(g);
  const pos = new Map<string, { x: number; y: number }>();
  nodes.forEach((n) => {
    const p = g.node(n.id);
    pos.set(n.id, { x: (p?.x ?? 0) - NODE_W / 2, y: (p?.y ?? 0) - NODE_H / 2 });
  });
  return pos;
}

export type PortRow = { name: string; datatype: string; porttype: string; expression: string };

export function resolvePorts(mapping: Mapping, instanceName: string, nodeType: string): { rows: PortRow[]; fromConnectors: boolean } {
  const instanceKey = normalizeIdentifier(instanceName);
  const inst = mapping.instances.find((i) => normalizeIdentifier(i.name) === instanceKey);
  const tname = inst?.transformation_name || instanceName;
  const definitionKey = normalizeIdentifier(tname);
  const k = kindOf(nodeType);
  const fmt = (d: string, p: string, s: string) => `${d}${p ? `(${p}${s && s !== '0' ? `,${s}` : ''})` : ''}`;
  if (k === 'source' || k === 'target') {
    const list = k === 'source' ? mapping.sources : mapping.targets;
    const def = list.find((x) => normalizeIdentifier(x.name) === definitionKey);
    if (def) return { rows: def.fields.map((f) => ({ name: f.name, datatype: fmt(f.datatype, f.precision, f.scale), porttype: k === 'source' ? 'OUTPUT' : 'INPUT', expression: '' })), fromConnectors: false };
  } else {
    const t = mapping.transformations.find((x) => normalizeIdentifier(x.name) === definitionKey);
    if (t) return { rows: t.ports.map((p) => ({ name: p.name, datatype: fmt(p.datatype, p.precision, p.scale), porttype: p.porttype, expression: p.expression })), fromConnectors: false };
  }
  const seen = new Map<string, PortRow>();
  mapping.connectors.forEach((c) => {
    if (normalizeIdentifier(c.to_instance) === instanceKey && !seen.has(normalizeIdentifier(c.to_field))) {
      seen.set(normalizeIdentifier(c.to_field), { name: c.to_field, datatype: '', porttype: 'INPUT', expression: '' });
    }
    if (normalizeIdentifier(c.from_instance) === instanceKey && !seen.has(normalizeIdentifier(c.from_field))) {
      seen.set(normalizeIdentifier(c.from_field), { name: c.from_field, datatype: '', porttype: 'OUTPUT', expression: '' });
    }
  });
  return { rows: [...seen.values()], fromConnectors: true };
}

export function errorText(error: unknown) {
  const data = (error as { data?: { error?: unknown } } | null)?.data;
  if (data && typeof data.error === 'string') return data.error;
  if (error instanceof Error && error.message) return error.message;
  return 'The trace could not be completed.';
}
