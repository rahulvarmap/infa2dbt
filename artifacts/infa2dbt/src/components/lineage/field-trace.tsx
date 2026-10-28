import type { TraceResponse } from '@workspace/api-client-react';
import { AlertCircle, LoaderCircle } from 'lucide-react';
import { kindOf } from './lineage-utils';

export type TraceState =
  | { status: 'idle' }
  | { status: 'loading'; field: string }
  | { status: 'error'; field: string; message: string }
  | { status: 'done'; field: string; result: TraceResponse };

export function FieldTrace({ trace, onClear, onRetry }: {
  trace: Exclude<TraceState, { status: 'idle' }>;
  onClear: () => void;
  onRetry: () => void;
}) {
  return (
    <section id="lineage-field-trace" className="lg-trace" aria-live="polite" aria-label={`Upstream of ${trace.field}`} data-testid="panel-trace">
      <div className="lg-trace-head">
        <h5 className="sub-head">Upstream of {trace.field}</h5>
        <button type="button" className="lg-clear" onClick={onClear} data-testid="button-clear-trace">Clear trace</button>
      </div>
      {trace.status === 'loading' && <p className="lg-hint" data-testid="status-trace-loading"><LoaderCircle size={13} className="animate-spin" /> Tracing lineage…</p>}
      {trace.status === 'error' && (
        <div className="error-message" role="alert" data-testid="error-trace">
          <AlertCircle size={16} className="shrink-0" />
          <span>{trace.message}</span>
          <button type="button" className="lg-clear" onClick={onRetry} data-testid="button-retry-trace">Retry</button>
        </div>
      )}
      {trace.status === 'done' && (
        trace.result.hops.length === 0 ? <p className="drop-caption">No upstream hops found for this column.</p> : (
          <>
            <p className="lg-hint">{trace.result.hops.length} hops from target to source. Branches are listed depth-first, not as one continuous chain.</p>
            <ol className="lg-hops" data-testid="list-trace-hops">
              {trace.result.hops.map((hop, index) => (
                <li key={`${hop.instance}-${hop.port}-${index}`} data-testid={`row-hop-${index}`}>
                  <span className="lg-hop-n">{String(index + 1).padStart(2, '0')}</span>
                  <div className="min-w-0">
                    <p className="lg-hop-main"><i className="lg-dot" data-kind={kindOf(hop.type)} /><span>{hop.instance}.{hop.port}</span></p>
                    <p className="lg-port-meta">{hop.type}</p>
                    {hop.expression && <code className="lg-expr">{hop.expression}</code>}
                  </div>
                </li>
              ))}
            </ol>
          </>
        )
      )}
    </section>
  );
}
