import { useEffect, useMemo, useState } from 'react';
import { LoaderCircle } from 'lucide-react';
import type { Mapping, ParseResponse } from '@workspace/api-client-react';
import type { ParsedSource } from '@/components/lineage/lineage-utils';
import type { DbtDialect } from '@/components/dbt-project-files';
import { orderKey } from '@/components/lineage/lineage-utils';

type ExplainStep = {
  instance: string;
  type: string;
  description: string;
};

type ExplainColumn = {
  target_column: string;
  source_columns: string[];
  expression: string;
  rule: string;
};

type ExplainModel = {
  name: string;
  path: string;
  status: 'generated' | 'blocked';
};

type ExplainDiagnostic = {
  severity: 'error' | 'warning' | 'info';
  code: string;
  message: string;
  instance?: string | null;
  field?: string | null;
};

type MappingExplanation = {
  mapping: string;
  overview: string;
  steps: ExplainStep[];
  columns: ExplainColumn[];
  migration_status: {
    supported: number;
    warning: number;
    blocked: number;
  };
  generated_models: ExplainModel[];
  diagnostics: ExplainDiagnostic[];
};

type ExplainPayload = {
  explanations: Record<string, MappingExplanation>;
};

function isExplanation(value: unknown): value is MappingExplanation {
  if (!value || typeof value !== 'object') return false;
  const candidate = value as Partial<MappingExplanation>;
  return (
    typeof candidate.mapping === 'string'
    && typeof candidate.overview === 'string'
    && Array.isArray(candidate.steps)
    && Array.isArray(candidate.columns)
    && !!candidate.migration_status
    && Array.isArray(candidate.generated_models)
    && Array.isArray(candidate.diagnostics)
  );
}

function isExplainPayload(value: unknown): value is ExplainPayload {
  if (!value || typeof value !== 'object') return false;
  const explanations = (value as { explanations?: unknown }).explanations;
  return (
    !!explanations
    && typeof explanations === 'object'
    && !Array.isArray(explanations)
    && Object.values(explanations).every(isExplanation)
  );
}

export function ExplainView({
  data,
  mapping,
  source,
  dialect,
}: {
  data: ParseResponse;
  mapping: Mapping;
  source: ParsedSource | null;
  dialect: DbtDialect;
}) {
  const mappingId = useMemo(() => orderKey(data, mapping), [data, mapping]);
  const [payload, setPayload] = useState<ExplainPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!source) {
      setPayload(null);
      setError('Choose a parsed XML file or sample to explain its mapping.');
      setLoading(false);
      return;
    }

    const controller = new AbortController();
    let active = true;
    const form = new FormData();
    if (source.kind === 'file') form.append('file', source.file);
    else form.append('sample_name', source.name);
    form.append('dialect', dialect);

    setLoading(true);
    setError('');
    setPayload(null);
    void (async () => {
      try {
        const response = await fetch('/api/explain', {
          method: 'POST',
          body: form,
          signal: controller.signal,
        });
        const result: unknown = await response.json().catch(() => null);
        if (!response.ok) {
          const message = result && typeof result === 'object'
            ? (result as { error?: unknown }).error
            : null;
          throw new Error(
            typeof message === 'string'
              ? message
              : 'The mapping explanation could not be generated.',
          );
        }
        if (!isExplainPayload(result)) {
          throw new Error('The server returned an invalid explanation response.');
        }
        if (active) setPayload(result);
      } catch (cause) {
        if (!active || (cause instanceof DOMException && cause.name === 'AbortError')) return;
        setError(
          cause instanceof Error
            ? cause.message
            : 'The mapping explanation could not be generated.',
        );
      } finally {
        if (active) setLoading(false);
      }
    })();

    return () => {
      active = false;
      controller.abort();
    };
  }, [source, dialect]);

  const explanation = payload?.explanations[mappingId];

  return (
    <section className="explain-view" data-testid="panel-explain">
      {loading && (
        <div className="explain-state" role="status" data-testid="explain-loading">
          <LoaderCircle size={16} className="animate-spin" aria-hidden="true" />
          <span>Preparing a deterministic mapping explanation…</span>
        </div>
      )}
      {error && !loading && (
        <p className="explain-error" role="alert" data-testid="explain-error">{error}</p>
      )}
      {!loading && !error && !explanation && (
        <p className="explain-error" role="alert">
          No explanation is available for this mapping.
        </p>
      )}

      {explanation && (
        <>
          <section className="explain-section" aria-labelledby="explain-overview-title">
            <h3 id="explain-overview-title">Overview</h3>
            <p className="explain-overview" data-testid="explain-overview">
              {explanation.overview}
            </p>
          </section>

          <section className="explain-section" aria-labelledby="explain-flow-title">
            <h3 id="explain-flow-title">Step-by-step flow</h3>
            {explanation.steps.length ? (
              <ol className="explain-steps" data-testid="explain-steps">
                {explanation.steps.map((step, index) => (
                  <li key={`${step.instance}-${index}`}>
                    <span className="explain-step-number">{String(index + 1).padStart(2, '0')}</span>
                    <div className="explain-step-copy">
                      <strong>{step.instance}</strong>
                      <span className="explain-step-type">{step.type}</span>
                      <p>{step.description}</p>
                    </div>
                  </li>
                ))}
              </ol>
            ) : (
              <p className="explain-muted">This mapping has no transformation steps.</p>
            )}
          </section>

          <section className="explain-section" aria-labelledby="explain-columns-title">
            <h3 id="explain-columns-title">Column derivations</h3>
            {explanation.columns.length ? (
              <div className="explain-table-wrap">
                <table className="explain-table" data-testid="explain-columns">
                  <thead>
                    <tr>
                      <th scope="col">Target column</th>
                      <th scope="col">Source columns</th>
                      <th scope="col">Rule</th>
                    </tr>
                  </thead>
                  <tbody>
                    {explanation.columns.map((column, index) => (
                      <tr key={`${column.target_column}-${index}`}>
                        <th scope="row">{column.target_column}</th>
                        <td data-label="Source columns">
                          {column.source_columns.length
                            ? column.source_columns.join(', ')
                            : 'No source column resolved'}
                        </td>
                        <td data-label="Rule">
                          <span>{column.rule}</span>
                          {column.expression && <code>{column.expression}</code>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="explain-muted">No target-column derivations were resolved.</p>
            )}
          </section>

          <section className="explain-section" aria-labelledby="explain-status-title">
            <div className="explain-section-head">
              <h3 id="explain-status-title">Migration status</h3>
              {explanation.diagnostics.length > 0 && (
                <a href="#explain-diagnostics" className="explain-diagnostics-link">
                  View diagnostics
                </a>
              )}
            </div>
            <div className="explain-status-grid" data-testid="explain-status">
              <div className="explain-status-card" data-status="supported">
                <span>{explanation.migration_status.supported}</span>
                <small>Supported models</small>
              </div>
              <a
                className="explain-status-card"
                data-status="warning"
                href="#explain-diagnostics"
                data-testid="explain-warning-link"
              >
                <span>{explanation.migration_status.warning}</span>
                <small>Warnings · view diagnostics</small>
              </a>
              <a
                className="explain-status-card"
                data-status="blocked"
                href="#explain-diagnostics"
              >
                <span>{explanation.migration_status.blocked}</span>
                <small>Blocked models · view diagnostics</small>
              </a>
            </div>

            <h4 className="explain-subhead">Generated dbt models</h4>
            {explanation.generated_models.length ? (
              <ul className="explain-models" data-testid="explain-models">
                {explanation.generated_models.map((model) => (
                  <li key={model.path} data-status={model.status}>
                    <span>{model.name}</span>
                    <code>{model.path}</code>
                    <small>{model.status === 'blocked' ? 'Blocked placeholder' : 'Generated'}</small>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="explain-muted">No dbt models were generated for this mapping.</p>
            )}

            <div id="explain-diagnostics" className="explain-diagnostics">
              <h4 className="explain-subhead">Diagnostics</h4>
              {explanation.diagnostics.length ? (
                <ul>
                  {explanation.diagnostics.map((diagnostic, index) => (
                    <li
                      key={`${diagnostic.code}-${index}`}
                      id={`explain-diagnostic-${index}`}
                      data-severity={diagnostic.severity}
                    >
                      <strong>{diagnostic.severity}</strong>
                      <code>{diagnostic.code}</code>
                      <p>{diagnostic.message}</p>
                      {(diagnostic.instance || diagnostic.field) && (
                        <small>
                          {[diagnostic.instance, diagnostic.field].filter(Boolean).join(' · ')}
                        </small>
                      )}
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="explain-muted">No migration diagnostics for this mapping.</p>
              )}
            </div>
          </section>
        </>
      )}
    </section>
  );
}
