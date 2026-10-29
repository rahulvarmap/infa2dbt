import { useEffect, useMemo, useState } from 'react';
import { Check, Copy, LoaderCircle } from 'lucide-react';
import type { ParsedSource } from '@/components/lineage/lineage-utils';
import { DbtCodeViewer } from '@/components/dbt-code-viewer';

export type DbtDialect = 'snowflake' | 'duckdb';

type BlockedModel = {
  path: string;
  name: string;
  mapping: string;
  reasons: string[];
};

type ProjectDiagnostic = {
  code: string;
  message: string;
  mapping: string;
};

type GeneratedProject = {
  dialect: DbtDialect;
  files: Record<string, string>;
  blocked_models: BlockedModel[];
  diagnostics: ProjectDiagnostic[];
};

function preferredFile(files: Record<string, string>): string {
  const paths = Object.keys(files).sort();
  return (
    paths.find((path) => path.startsWith('models/marts/') && path.endsWith('.sql'))
    ?? paths.find((path) => path.startsWith('models/staging/') && path.endsWith('.sql'))
    ?? paths[0]
    ?? ''
  );
}

function isGeneratedProject(value: unknown): value is GeneratedProject {
  if (!value || typeof value !== 'object') return false;
  const candidate = value as Partial<GeneratedProject>;
  return (
    (candidate.dialect === 'snowflake' || candidate.dialect === 'duckdb')
    && !!candidate.files
    && typeof candidate.files === 'object'
    && Object.values(candidate.files).every((file) => typeof file === 'string')
    && Array.isArray(candidate.blocked_models)
    && Array.isArray(candidate.diagnostics)
  );
}

export function DbtProjectFiles({
  source,
  dialect,
  onDialectChange,
}: {
  source: ParsedSource | null;
  dialect: DbtDialect;
  onDialectChange: (dialect: DbtDialect) => void;
}) {
  const [project, setProject] = useState<GeneratedProject | null>(null);
  const [selectedPath, setSelectedPath] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [copyError, setCopyError] = useState('');
  const [copiedPath, setCopiedPath] = useState('');

  useEffect(() => {
    if (!source) {
      setLoading(false);
      setError('Choose a parsed XML file or sample to preview generated dbt files.');
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
    setCopyError('');
    setProject(null);
    setSelectedPath('');

    void (async () => {
      try {
        const response = await fetch('/api/dbt/project/files', {
          method: 'POST',
          body: form,
          signal: controller.signal,
        });
        const payload: unknown = await response.json().catch(() => null);
        if (!response.ok) {
          const message = payload && typeof payload === 'object'
            ? (payload as { error?: unknown }).error
            : null;
          throw new Error(typeof message === 'string' ? message : 'Generated dbt files could not be loaded.');
        }
        if (!isGeneratedProject(payload)) {
          throw new Error('The server returned an invalid generated-file response.');
        }
        if (!active) return;
        setProject(payload);
        setSelectedPath(preferredFile(payload.files));
      } catch (cause) {
        if (!active || (cause instanceof DOMException && cause.name === 'AbortError')) return;
        setError(cause instanceof Error ? cause.message : 'Generated dbt files could not be loaded.');
      } finally {
        if (active) setLoading(false);
      }
    })();

    return () => {
      active = false;
      controller.abort();
    };
  }, [source, dialect]);

  const paths = useMemo(
    () => project ? Object.keys(project.files).sort() : [],
    [project],
  );
  const blockedDetails = useMemo(() => {
    if (!project || !selectedPath) return null;
    const blocked = project.blocked_models.find((item) => item.path === selectedPath);
    if (!blocked) return null;
    const diagnostics = project.diagnostics.filter((item) => (
      item.mapping === blocked.mapping && blocked.reasons.includes(item.code)
    ));
    return { blocked, diagnostics };
  }, [project, selectedPath]);

  async function copyFile(path: string) {
    if (!project || !(path in project.files)) return;
    setCopyError('');
    try {
      await navigator.clipboard.writeText(project.files[path]);
      setCopiedPath(path);
    } catch {
      setCopyError(`Could not copy ${path}.`);
    }
  }

  return (
    <section className="dbt-project-files" data-testid="panel-dbt-sql">
      <div className="dbt-sql-toolbar">
        <div>
          <h3 className="dbt-sql-title">Generated dbt files</h3>
          <p className="dbt-sql-caption">
            Preview uses the same generated files included in the download ZIP.
          </p>
        </div>
        <div className="dbt-dialect-toggle" role="group" aria-label="SQL dialect">
          {(['snowflake', 'duckdb'] as DbtDialect[]).map((option) => (
            <button
              key={option}
              type="button"
              className="dbt-dialect-button"
              aria-pressed={dialect === option}
              data-active={dialect === option}
              onClick={() => onDialectChange(option)}
              data-testid={`dbt-dialect-${option}`}
            >
              {option === 'snowflake' ? 'Snowflake' : 'DuckDB'}
            </button>
          ))}
        </div>
      </div>

      {loading && (
        <div className="dbt-files-state" role="status" data-testid="dbt-files-loading">
          <LoaderCircle size={17} className="animate-spin" aria-hidden="true" />
          <span>Generating {dialect === 'snowflake' ? 'Snowflake' : 'DuckDB'} files…</span>
        </div>
      )}
      {error && (
        <div className="dbt-files-error" role="alert" data-testid="dbt-files-error">{error}</div>
      )}
      {copyError && (
        <div className="dbt-files-error" role="alert" data-testid="dbt-copy-error">{copyError}</div>
      )}

      {project && (
        <div className="dbt-project-browser">
          <aside className="dbt-file-picker" aria-label="Generated files">
            <h4>Files</h4>
            {paths.length ? (
              <ul>
                {paths.map((path) => (
                  <li key={path} data-active={path === selectedPath}>
                    <button
                      type="button"
                      className="dbt-file-select"
                      aria-pressed={path === selectedPath}
                      onClick={() => { setSelectedPath(path); setCopiedPath(''); }}
                      title={path}
                    >
                      <span>{path}</span>
                    </button>
                    <button
                      type="button"
                      className="dbt-file-copy"
                      aria-label={`${copiedPath === path ? 'Copied' : 'Copy'} ${path}`}
                      onClick={() => { void copyFile(path); }}
                      data-testid={`copy-file-${path.replace(/[^a-z0-9]+/gi, '-')}`}
                    >
                      {copiedPath === path
                        ? <Check size={14} aria-hidden="true" />
                        : <Copy size={14} aria-hidden="true" />}
                      <span>{copiedPath === path ? 'Copied' : 'Copy'}</span>
                    </button>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="dbt-sql-caption">No files were generated.</p>
            )}
          </aside>

          <div className="dbt-file-preview">
            {selectedPath && project.files[selectedPath] !== undefined ? (
              <>
                <div className="dbt-file-preview-head">
                  <code title={selectedPath}>{selectedPath}</code>
                  <span>{project.files[selectedPath].split(/\r?\n/).length} lines</span>
                </div>
                {blockedDetails && (
                  <div className="dbt-blocked-notice" role="note" data-testid="dbt-blocked-reason">
                    <strong>Blocked model placeholder</strong>
                    <code>{blockedDetails.blocked.reasons.join(', ')}</code>
                    {blockedDetails.diagnostics.length ? (
                      blockedDetails.diagnostics.map((diagnostic, index) => (
                        <p key={`${diagnostic.code}-${index}`}>{diagnostic.message}</p>
                      ))
                    ) : (
                      <p>This model is a placeholder because its transformation is unsupported.</p>
                    )}
                  </div>
                )}
                <DbtCodeViewer
                  path={selectedPath}
                  content={project.files[selectedPath]}
                />
              </>
            ) : (
              <p className="dbt-sql-caption">Select a generated file to preview its contents.</p>
            )}
          </div>
        </div>
      )}
    </section>
  );
}
