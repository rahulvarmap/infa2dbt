import { useEffect, useMemo, useRef, useState } from 'react';
import {
  Background, Controls, Handle, Position, ReactFlow, ReactFlowProvider, MarkerType,
  getNodesBounds, getViewportForBounds, useReactFlow, type Edge, type Node, type NodeProps,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { useTraceLineage } from '@workspace/api-client-react';
import type { Mapping, ParseResponse } from '@workspace/api-client-react';
import { GitBranch, Route as RouteIcon, X } from 'lucide-react';
import { FieldTrace, type TraceState } from './field-trace';
import { KINDS, NODE_W, NODE_H, errorText, kindLabel, kindOf, layoutGraph, resolvePorts, type Kind, type ParsedSource } from './lineage-utils';

type NodeData = { name: string; type: string; kind: Kind; state: 'idle' | 'selected' | 'trace' | 'dim' };

function InstanceNode({ data, id }: NodeProps<Node<NodeData>>) {
  return (
    <div className="lg-node" data-kind={data.kind} data-state={data.state} data-testid={`node-lineage-${id}`} title={`${data.name} · ${data.type}`}>
      <Handle type="target" position={Position.Left} isConnectable={false} />
      <span className="lg-node-type">{kindLabel(data.kind)}</span>
      <span className="lg-node-name">{data.name}</span>
      <Handle type="source" position={Position.Right} isConnectable={false} />
    </div>
  );
}

const nodeTypes = { instance: InstanceNode };

export function LineageView({ data, mapping, source }: { data: ParseResponse; mapping: Mapping; source: ParsedSource | null }) {
  return <ReactFlowProvider><LineageInner data={data} mapping={mapping} source={source} /></ReactFlowProvider>;
}

function LineageInner({ data, mapping, source }: { data: ParseResponse; mapping: Mapping; source: ParsedSource | null }) {
  const mappingKey = useMemo(() => {
    const dup = data.mappings.filter((x) => x.name === mapping.name).length > 1;
    return dup ? `${mapping.repository}/${mapping.folder}/${mapping.name}` : mapping.name;
  }, [data, mapping]);
  const lnodes = useMemo(() => data.lineage.nodes.filter((n) => n.mapping === mappingKey), [data, mappingKey]);
  const ledges = useMemo(() => data.lineage.edges.filter((e) => e.mapping === mappingKey), [data, mappingKey]);
  const positions = useMemo(() => layoutGraph(lnodes, ledges), [lnodes, ledges]);
  const bounds = useMemo(() => getNodesBounds(
    Array.from(positions, ([id, position]) => ({
      id, position, width: NODE_W, height: NODE_H, data: {},
    })),
  ), [positions]);

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [trace, setTrace] = useState<TraceState>({ status: 'idle' });
  const reqId = useRef(0);
  const traceMut = useTraceLineage();
  const sourceIdentity = source?.kind === 'file' ? source.file : source?.name;
  useEffect(() => {
    reqId.current++;
    setSelectedId(null);
    setTrace({ status: 'idle' });
    return () => { reqId.current++; };
  }, [data, mappingKey, sourceIdentity]);

  const selected = lnodes.find((n) => n.id === selectedId) ?? null;
  const canvasRef = useRef<HTMLDivElement>(null);
  const { setViewport, viewportInitialized } = useReactFlow();
  useEffect(() => {
    const viewport = canvasRef.current?.querySelector<HTMLDivElement>('.react-flow');
    if (!viewportInitialized || !viewport) return;
    let frame = 0;
    // Fit the known dagre bounds directly using the visible viewport dimensions.
    // Queued fitView depends on controlled-node measurement updates; these nodes
    // already have fixed dimensions, so resizing must not wait for another update.
    const observer = new ResizeObserver(() => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        if (viewport.clientWidth && viewport.clientHeight) {
          void setViewport(getViewportForBounds(
            bounds, viewport.clientWidth, viewport.clientHeight, 0.2, 2, 0.15,
          ), { duration: 180 });
        }
      });
    });
    observer.observe(viewport);
    return () => { observer.disconnect(); cancelAnimationFrame(frame); };
  }, [bounds, viewportInitialized, setViewport]);
  const selectedKind = selected ? kindOf(selected.type) : null;
  const ports = useMemo(() => (selected ? resolvePorts(mapping, selected.name, selected.type) : null), [selected, mapping]);

  const hopNames = useMemo(() => {
    if (trace.status !== 'done') return null;
    return new Set(trace.result.hops.map((h) => h.instance));
  }, [trace]);
  const hopPorts = useMemo(() => {
    if (trace.status !== 'done') return null;
    return new Set(trace.result.hops.map((h) => `${h.instance}\u0000${h.port}`));
  }, [trace]);

  const nameById = useMemo(() => new Map(lnodes.map((n) => [n.id, n.name])), [lnodes]);

  const nodes: Node<NodeData>[] = useMemo(() => lnodes.map((n) => {
    const inTrace = hopNames?.has(n.name) ?? false;
    const state: NodeData['state'] = n.id === selectedId ? 'selected' : hopNames ? (inTrace ? 'trace' : 'dim') : 'idle';
    return { id: n.id, type: 'instance', width: NODE_W, height: NODE_H, position: positions.get(n.id) ?? { x: 0, y: 0 }, data: { name: n.name, type: n.type, kind: kindOf(n.type), state }, draggable: false };
  }), [lnodes, positions, selectedId, hopNames]);

  const edges: Edge[] = useMemo(() => ledges.map((e) => {
    const s = nameById.get(e.source) ?? e.source;
    const t = nameById.get(e.target) ?? e.target;
    const lit = hopPorts ? e.fields.some((f) => hopPorts.has(`${s}\u0000${f.from_field}`) && hopPorts.has(`${t}\u0000${f.to_field}`)) : false;
    return {
      id: e.id, source: e.source, target: e.target,
      className: hopPorts ? (lit ? 'lg-edge-lit' : 'lg-edge-dim') : 'lg-edge',
      animated: lit,
      markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14 },
      data: { fields: e.fields.length },
    };
  }), [ledges, nameById, hopPorts]);

  async function runTrace(field: string) {
    if (!selected) return;
    const id = ++reqId.current;
    if (!source) {
      setTrace({ status: 'error', field, message: 'Load a sample or upload the XML again before tracing this column.' });
      return;
    }
    setTrace({ status: 'loading', field });
    try {
      const payload = source.kind === 'file'
        ? { target_instance: selected.name, target_field: field, mapping: mappingKey, file: source.file }
        : { target_instance: selected.name, target_field: field, mapping: mappingKey, sample_name: source.name };
      const result = await traceMut.mutateAsync({ data: payload });
      if (id === reqId.current) setTrace({ status: 'done', field, result });
    } catch (err) {
      if (id === reqId.current) setTrace({ status: 'error', field, message: errorText(err) });
    }
  }

  function selectNode(id: string | null) {
    reqId.current++;
    setSelectedId(id);
    setTrace({ status: 'idle' });
  }

  function clearTrace() {
    reqId.current++;
    setTrace({ status: 'idle' });
    traceMut.reset();
  }

  if (lnodes.length === 0) {
    return (
      <div className="lg-empty" data-testid="status-lineage-empty">
        <GitBranch size={20} />
        <p>No lineage graph for this mapping.</p>
        <small>The parser found no instances or connectors to draw.</small>
      </div>
    );
  }

  const tracedField = trace.status !== 'idle' ? trace.field : null;

  return (
    <div className="lg-wrap" data-panel={selected ? 'open' : 'closed'}>
      <div ref={canvasRef} className="lg-canvas" data-testid="graph-lineage">
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={nodeTypes}
          fitView
          fitViewOptions={{ padding: 0.15 }}
          minZoom={0.2}
          maxZoom={2}
          nodesConnectable={false}
          proOptions={{ hideAttribution: true }}
          onNodeClick={(_, n) => selectNode(n.id)}
          onPaneClick={() => selectNode(null)}
        >
          <Background gap={18} size={1} />
          <Controls showInteractive={false} position="bottom-left" />
        </ReactFlow>
        <ul className="lg-legend" aria-label="Node type legend" data-testid="legend-lineage">
          {KINDS.map((k) => <li key={k.kind}><i data-kind={k.kind} />{k.label}</li>)}
        </ul>
        <p className="lg-meta" data-testid="text-lineage-counts">{lnodes.length} instances · {ledges.length} links</p>
      </div>

      {selected && ports && (
        <aside className="lg-panel" aria-label={`Details for ${selected.name}`} data-testid="panel-node-detail">
          <div className="lg-panel-head">
            <div style={{ minWidth: 0 }}>
              <span className="lg-chip" data-kind={selectedKind ?? 'unknown'}>{kindLabel(selectedKind ?? 'unknown')}</span>
              <h4 className="lg-panel-title" data-testid="text-node-name">{selected.name}</h4>
              <p className="file-meta">{selected.type}</p>
            </div>
            <button type="button" className="lg-close" onClick={() => selectNode(null)} aria-label="Close details" data-testid="button-close-node">
              <X size={16} />
            </button>
          </div>

          {selectedKind === 'target' && (
            <p className="lg-hint"><RouteIcon size={13} /> Select a column to trace it back to its sources.</p>
          )}
          {ports.fromConnectors && <p className="lg-hint">Definition not found; ports inferred from connectors.</p>}

          {ports.rows.length === 0 ? <p className="drop-caption">No ports recorded for this instance.</p> : (
            <ul className="lg-ports" data-testid="list-node-ports">
              {ports.rows.map((p, i) => {
                const body = (
                  <>
                    <span className="lg-port-name">{p.name}</span>
                    <span className="lg-port-meta">{[selectedKind === 'target' ? 'column' : p.porttype, p.datatype].filter(Boolean).join(' · ')}</span>
                    {p.expression && <code className="lg-expr">{p.expression}</code>}
                  </>
                );
                return selectedKind === 'target' ? (
                  <li key={`${p.name}-${i}`}>
                    <button
                      type="button"
                      className="lg-port lg-port-btn"
                      data-active={tracedField === p.name}
                      aria-expanded={tracedField === p.name}
                      aria-controls={tracedField === p.name ? 'lineage-field-trace' : undefined}
                      onClick={(event) => { event.stopPropagation(); void runTrace(p.name); }}
                      data-testid={`button-trace-field-${p.name}`}
                    >
                      {body}
                    </button>
                    {trace.status !== 'idle' && trace.field === p.name && (
                      <FieldTrace trace={trace} onClear={clearTrace} onRetry={() => { void runTrace(p.name); }} />
                    )}
                  </li>
                ) : <li key={`${p.name}-${i}`} className="lg-port" data-testid={`row-node-port-${p.name}`}>{body}</li>;
              })}
            </ul>
          )}

        </aside>
      )}
    </div>
  );
}
