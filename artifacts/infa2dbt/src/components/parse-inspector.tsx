import { useMemo, useState } from 'react';
import type { Mapping, ParseResponse } from '@workspace/api-client-react';

type Tab = 'summary' | 'ports' | 'raw';

function orderKey(data: ParseResponse, m: Mapping) {
  const dup = data.mappings.filter((x) => x.name === m.name).length > 1;
  return dup ? `${m.repository}/${m.folder}/${m.name}` : m.name;
}

export function ParseInspector({ data, label }: { data: ParseResponse; label: string }) {
  const [mapIdx, setMapIdx] = useState(0);
  const [tab, setTab] = useState<Tab>('summary');
  const [tIdx, setTIdx] = useState(0);
  const [rawOpen, setRawOpen] = useState(false);
  const mapping = data.mappings[Math.min(mapIdx, data.mappings.length - 1)];
  const order = mapping ? data.execution_order[orderKey(data, mapping)] ?? [] : [];
  const transformation = mapping?.transformations[Math.min(tIdx, mapping.transformations.length - 1)];
  const json = useMemo(() => JSON.stringify(data, null, 2), [data]);

  return (
    <section className="inspector" data-testid="panel-inspector">
      <div className="inspector-head">
        <p className="file-meta" data-testid="text-parsed-source">{label}</p>
        {data.mappings.length > 1 && (
          <select aria-label="Mapping" className="inspector-select" value={mapIdx} onChange={(e) => { setMapIdx(Number(e.target.value)); setTIdx(0); }} data-testid="select-mapping">
            {data.mappings.map((m, i) => <option key={i} value={i}>{orderKey(data, m)}</option>)}
          </select>
        )}
      </div>
      <div className="tabs" role="tablist" aria-label="Parsed mapping views">
        {(['summary', 'ports', 'raw'] as Tab[]).map((t) => (
          <button key={t} role="tab" type="button" aria-selected={tab === t} className="tab" data-active={tab === t} onClick={() => setTab(t)} data-testid={`tab-${t}`}>{t}</button>
        ))}
      </div>
      {!mapping && tab !== 'raw' ? <p className="drop-caption">No mappings found in this file.</p> : null}
      {mapping && tab === 'summary' && (
        <div data-testid="panel-summary">
          <h3 className="mapping-name" data-testid="text-mapping-name">{mapping.name}</h3>
          <p className="file-meta">{mapping.repository} / {mapping.folder}</p>
          <div className="stat-grid">
            {[['Sources', mapping.sources.length], ['Targets', mapping.targets.length], ['Transformations', mapping.transformations.length], ['Connectors', mapping.connectors.length]].map(([k, v]) => (
              <div key={k} className="stat" data-testid={`stat-${String(k).toLowerCase()}`}><span>{v}</span><small>{k}</small></div>
            ))}
          </div>
          <h4 className="sub-head">Transformations</h4>
          <ul className="t-list">
            {mapping.transformations.map((t) => <li key={t.name}><span className="t-name">{t.name}</span><span className="t-type">{t.type}</span></li>)}
          </ul>
          <h4 className="sub-head">Execution order</h4>
          {order.length ? (
            <ol className="order-list" data-testid="list-execution-order">{order.map((n, i) => <li key={`${n}-${i}`}><span>{String(i + 1).padStart(2, '0')}</span>{n}</li>)}</ol>
          ) : <p className="drop-caption">No execution order available.</p>}
        </div>
      )}
      {mapping && tab === 'ports' && (
        <div data-testid="panel-ports">
          {mapping.transformations.length === 0 ? <p className="drop-caption">This mapping has no transformations.</p> : (
            <>
              <select aria-label="Transformation" className="inspector-select" value={tIdx} onChange={(e) => setTIdx(Number(e.target.value))} data-testid="select-transformation">
                {mapping.transformations.map((t, i) => <option key={i} value={i}>{t.name} ({t.type})</option>)}
              </select>
              <div className="table-wrap" role="region" aria-label="Transformation ports; scroll horizontally for more columns" tabIndex={0}>
                <table className="port-table">
                  <thead><tr><th>Name</th><th>Datatype</th><th>Port type</th><th>Expression</th></tr></thead>
                  <tbody>
                    {transformation?.ports.map((p, i) => (
                      <tr key={i} data-testid={`row-port-${i}`}>
                        <td>{p.name}</td>
                        <td>{p.datatype}{p.precision ? `(${p.precision}${p.scale && p.scale !== '0' ? `,${p.scale}` : ''})` : ''}</td>
                        <td>{p.porttype}</td>
                        <td className="expr">{p.expression || '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </div>
      )}
      {tab === 'raw' && (
        <div data-testid="panel-raw">
          <button type="button" className="sample-button" onClick={() => setRawOpen((v) => !v)} data-testid="button-toggle-raw">{rawOpen ? 'Collapse' : 'Expand'} IR JSON</button>
          {rawOpen && <pre className="raw-json" data-testid="text-raw-json">{json}</pre>}
        </div>
      )}
    </section>
  );
}
