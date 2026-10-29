import { useEffect, useRef, useState, type DragEvent, type ChangeEvent } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import {
  getGetReadinessQueryKey,
  useGetReadiness,
  useListSamples,
  useParseSample,
  useParseXml,
  useUploadXml,
} from '@workspace/api-client-react';
import type { ParseResponse } from '@workspace/api-client-react';
import { ParseInspector } from '@/components/parse-inspector';
import type { DbtDialect } from '@/components/dbt-project-files';
import type { ParsedSource } from '@/components/lineage/lineage-utils';
import {
  AlertCircle,
  ArrowUpFromLine,
  Check,
  Download,
  FileCode2,
  FileText,
  LoaderCircle,
  LockKeyhole,
  Moon,
  Sun,
  X,
} from 'lucide-react';
import { ErrorBoundary } from '@/components/error-boundary';
import { Toaster } from '@/components/ui/toaster';
import { TooltipProvider } from '@/components/ui/tooltip';
import NotFound from '@/pages/not-found';
import { Route, Switch, useLocation, Router as WouterRouter } from 'wouter';
import type { ReactNode } from 'react';

const queryClient = new QueryClient();
const MAX_FILE_SIZE = 5 * 1024 * 1024;

type ValidationReport = {
  status: 'passed' | 'failed' | 'blocked' | string;
  dialect: string;
  steps: Array<{ name: string; command: string; status: string; message: string }>;
  models: Array<{
    name: string;
    kind: string;
    path: string;
    status: string;
    phase: string;
    message: string;
    row_count: number | null;
  }>;
};

function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} bytes`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

function messageFromError(error: unknown) {
  const data = (error as { data?: { error?: unknown } } | null)?.data;
  if (data && typeof data.error === 'string') return data.error;
  if (error instanceof Error && error.message) return error.message;
  return 'The upload could not be completed. Please try again.';
}

function Home() {
  const [file, setFile] = useState<File | null>(null);
  const [result, setResult] = useState<{ filename: string; size_bytes: number } | null>(null);
  const [validationError, setValidationError] = useState('');
  const [dragging, setDragging] = useState(false);
  const [dark, setDark] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const readiness = useGetReadiness({
    query: { queryKey: getGetReadinessQueryKey(), refetchInterval: 30_000, retry: 1 },
  });
  const upload = useUploadXml();
  const parseXml = useParseXml();
  const parseSample = useParseSample();
  const samples = useListSamples();
  const [parsed, setParsed] = useState<ParseResponse | null>(null);
  const [parsedLabel, setParsedLabel] = useState('');
  const [parsedSource, setParsedSource] = useState<ParsedSource | null>(null);
  const [dbtDialect, setDbtDialect] = useState<DbtDialect>('snowflake');
  const [activeSample, setActiveSample] = useState<string | null>(null);
  const [generatingProject, setGeneratingProject] = useState(false);
  const [generationError, setGenerationError] = useState('');
  const [validatingProject, setValidatingProject] = useState(false);
  const [validationElapsedSeconds, setValidationElapsedSeconds] = useState(0);
  const [validationReport, setValidationReport] = useState<ValidationReport | null>(null);
  const [validationReportError, setValidationReportError] = useState('');
  const busy = upload.isPending || parseXml.isPending || parseSample.isPending;

  useEffect(() => {
    const saved = window.localStorage.getItem('infa2dbt-theme');
    setDark(saved === 'dark');
  }, []);

  useEffect(() => {
    document.documentElement.classList.toggle('dark', dark);
    window.localStorage.setItem('infa2dbt-theme', dark ? 'dark' : 'light');
  }, [dark]);

  useEffect(() => {
    if (!validatingProject) {
      setValidationElapsedSeconds(0);
      return;
    }
    const startedAt = Date.now();
    const interval = window.setInterval(() => {
      setValidationElapsedSeconds(Math.floor((Date.now() - startedAt) / 1000));
    }, 1000);
    return () => window.clearInterval(interval);
  }, [validatingProject]);

  const apiReady = readiness.data?.status === 'ready' && !readiness.isError;
  const apiError = readiness.isError;
  const apiLoading = readiness.isLoading;
  const apiState = apiReady ? 'ready' : apiError ? 'error' : 'checking';

  function selectFile(candidate: File | undefined) {
    setValidationError('');
    setGenerationError('');
    setValidationReport(null);
    setValidationReportError('');
    if (busy) return;
    setResult(null);
    setParsed(null); setParsedSource(null);
    upload.reset();
    parseXml.reset();
    parseSample.reset();
    if (!candidate) return;
    if (!candidate.name.toLowerCase().endsWith('.xml')) {
      setFile(null);
      setValidationError('Choose an XML file to continue. This intake only accepts .xml files.');
      return;
    }
    if (candidate.size > MAX_FILE_SIZE) {
      setFile(null);
      setValidationError('This file is larger than 5 MB. Choose a smaller XML file and try again.');
      return;
    }
    setFile(candidate);
  }

  function onPick(event: ChangeEvent<HTMLInputElement>) {
    selectFile(event.currentTarget.files?.[0]);
    event.currentTarget.value = '';
  }

  function onDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setDragging(false);
    selectFile(event.dataTransfer.files?.[0]);
  }

  async function onSubmit() {
    if (!file || busy) return;
    setValidationError('');
    setResult(null);
    setParsed(null); setParsedSource(null);
    setGenerationError('');
    setValidationReport(null);
    setValidationReportError('');
    parseXml.reset();
    parseSample.reset();
    try {
      const response = await upload.mutateAsync({ data: { file } });
      setResult(response);
      const ir = await parseXml.mutateAsync({ data: { file } });
      setParsedSource({ kind: 'file', file });
      setParsed(ir);
      setParsedLabel(response.filename);
    } catch {
      setParsed(null); setParsedSource(null);
    }
  }

  async function onSample(filename: string) {
    if (busy) return;
    setValidationError('');
    setResult(null);
    setParsed(null); setParsedSource(null);
    setGenerationError('');
    setValidationReport(null);
    setValidationReportError('');
    setFile(null);
    upload.reset();
    parseXml.reset();
    setActiveSample(filename);
    try {
      const ir = await parseSample.mutateAsync({ filename });
      setParsedSource({ kind: 'sample', name: filename });
      setParsed(ir);
      setParsedLabel(`sample / ${filename}`);
    } catch {
      setParsed(null); setParsedSource(null);
    } finally {
      setActiveSample(null);
    }
  }

  async function onDownloadDbtProject() {
    if (!parsedSource || generatingProject || validatingProject) return;
    setGenerationError('');
    setGeneratingProject(true);
    const form = new FormData();
    if (parsedSource.kind === 'file') {
      form.append('file', parsedSource.file);
    } else {
      form.append('sample_name', parsedSource.name);
    }
    form.append('dialect', dbtDialect);
    try {
      const response = await fetch('/api/dbt/project', {
        method: 'POST',
        body: form,
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => null) as { error?: string } | null;
        throw new Error(payload?.error || 'The dbt project could not be generated.');
      }
      if (!response.headers.get('content-type')?.includes('application/zip')) {
        throw new Error('The server returned an invalid dbt project archive.');
      }
      const archive = await response.blob();
      const url = URL.createObjectURL(archive);
      const link = document.createElement('a');
      link.href = url;
      link.download = `infa2dbt_${dbtDialect}.zip`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 0);
    } catch (error) {
      setGenerationError(
        error instanceof Error ? error.message : 'The dbt project could not be generated.',
      );
    } finally {
      setGeneratingProject(false);
    }
  }

  async function onValidateDbtProject() {
    if (!parsedSource || generatingProject || validatingProject) return;
    setValidationReport(null);
    setValidationReportError('');
    setValidatingProject(true);
    const form = new FormData();
    if (parsedSource.kind === 'file') {
      form.append('file', parsedSource.file);
    } else {
      form.append('sample_name', parsedSource.name);
    }
    try {
      const response = await fetch('/api/dbt/validate', {
        method: 'POST',
        body: form,
      });
      const payload = await response.json().catch(() => null) as ValidationReport | { error?: string } | null;
      if (!response.ok) {
        const error = payload && 'error' in payload ? payload.error : undefined;
        throw new Error(error || 'DuckDB validation could not be completed.');
      }
      if (!payload || !('models' in payload) || !('steps' in payload)) {
        throw new Error('The server returned an invalid validation report.');
      }
      setValidationReport(payload);
    } catch (error) {
      setValidationReportError(
        error instanceof Error ? error.message : 'DuckDB validation could not be completed.',
      );
    } finally {
      setValidatingProject(false);
    }
  }

  function clearFile() {
    if (busy) return;
    setFile(null);
    setResult(null);
    setParsed(null); setParsedSource(null);
    setValidationError('');
    setGenerationError('');
    setValidationReport(null);
    setValidationReportError('');
    upload.reset();
    parseXml.reset();
    parseSample.reset();
  }

  return (
    <div className="workspace-shell">
      <header className="topbar">
        <div className="flex items-center gap-3">
          <div className="brand-mark" aria-hidden="true">
            <FileCode2 size={18} strokeWidth={2.1} />
          </div>
          <div className="brand-name">infa<span>2</span>dbt</div>
        </div>
        <div className="flex items-center gap-4">
          <div className="health-line" data-testid="status-api-readiness" aria-live="polite">
            <span className="health-dot" data-state={apiState} />
            {apiLoading ? 'CHECKING API' : apiReady ? 'API READY' : apiError ? 'API UNAVAILABLE' : 'CONNECTING'}
          </div>
          <button
            type="button"
            className="theme-toggle"
            onClick={() => setDark((value) => !value)}
            aria-label={`Switch to ${dark ? 'light' : 'dark'} mode`}
            data-testid="button-toggle-theme"
          >
            {dark ? <Sun size={15} /> : <Moon size={15} />}
            <span className="hidden sm:inline">{dark ? 'Light' : 'Dark'} mode</span>
          </button>
        </div>
      </header>

      <main className="main-content">
        <div className="eyebrow">Migration intake · XML</div>
        <h1 className="hero-title">Informatica <em>→ dbt Migrator</em></h1>
        <p className="hero-copy">
          Send an Informatica XML file through a simple, secure intake. We validate it, then parse its sources, transformations, ports and connectors into an inspectable structure you can read before migrating.
        </p>

        <section className="intake-card" aria-label="XML file upload">
          <div
            className="dropzone"
            data-dragging={dragging}
            onDragEnter={(event) => { event.preventDefault(); setDragging(true); }}
            onDragOver={(event) => event.preventDefault()}
            onDragLeave={(event) => {
              if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDragging(false);
            }}
            onDrop={onDrop}
            aria-label="File drop area"
            data-testid="dropzone-xml-file"
          >
            {file ? (
              <>
                <div className="upload-icon"><FileText size={21} /></div>
                <div className="file-row" data-testid="file-selected">
                  <div className="file-type-icon"><FileCode2 size={20} /></div>
                  <div className="file-info">
                    <p className="file-name" title={file.name}>{file.name}</p>
                    <p className="file-meta">{formatBytes(file.size)} · XML document</p>
                  </div>
                  <button type="button" className="remove-file" onClick={clearFile} disabled={busy} aria-label="Remove selected file" data-testid="button-remove-file">
                    <X size={17} />
                  </button>
                </div>
                <p className="size-note">READY FOR VALIDATION</p>
              </>
            ) : (
              <>
                <div className="upload-icon"><ArrowUpFromLine size={21} /></div>
                <h2 className="drop-title">Drop your XML file here</h2>
                <p className="drop-caption">or choose a file from your device</p>
                <input
                  ref={inputRef}
                  className="file-picker-input"
                  id="xml-file-picker"
                  type="file"
                  accept=".xml,application/xml,text/xml"
                  onChange={onPick}
                  aria-label="Choose an XML file"
                  data-testid="input-xml-file"
                />
                <label htmlFor="xml-file-picker" className="file-picker-label" data-testid="button-choose-file">
                  Browse files
                </label>
                <p className="size-note">XML ONLY <span aria-hidden="true">·</span> UP TO 5 MB</p>
              </>
            )}
          </div>

          {validationError && (
            <div className="error-message" role="alert" data-testid="error-file-validation">
              <AlertCircle size={17} className="mt-0.5 shrink-0" />
              <span>{validationError}</span>
            </div>
          )}
          {apiError && (
            <div className="error-message" role="alert" data-testid="error-api-readiness">
              <AlertCircle size={17} className="mt-0.5 shrink-0" />
              <span>The API is unavailable right now. Your selected file has not been sent. Check the connection and try again.</span>
              <button
                type="button"
                className="ml-auto shrink-0 font-semibold underline underline-offset-2"
                onClick={() => { void readiness.refetch(); }}
                data-testid="button-retry-api"
              >
                Retry
              </button>
            </div>
          )}
          {upload.isError && (
            <div className="error-message" role="alert" data-testid="error-upload">
              <AlertCircle size={17} className="mt-0.5 shrink-0" />
              <span>{messageFromError(upload.error)}</span>
            </div>
          )}
          {parseXml.isError && (
            <div className="error-message" role="alert" data-testid="error-parse">
              <AlertCircle size={17} className="mt-0.5 shrink-0" />
              <span>Upload succeeded, but parsing failed: {messageFromError(parseXml.error)}</span>
            </div>
          )}
          {parseSample.isError && (
            <div className="error-message" role="alert" data-testid="error-parse-sample">
              <AlertCircle size={17} className="mt-0.5 shrink-0" />
              <span>The sample could not be parsed: {messageFromError(parseSample.error)}</span>
            </div>
          )}

          <div className="action-bar">
            <div className="privacy-note">
              <LockKeyhole size={15} className="shrink-0" />
              <span>Files are processed temporarily and not kept after parsing.</span>
            </div>
            <button
              type="button"
              className="primary-action"
              onClick={() => { void onSubmit(); }}
              disabled={!file || busy}
              data-testid="button-upload-xml"
            >
              {upload.isPending || parseXml.isPending ? (
                <>
                  <LoaderCircle size={16} className="animate-spin" />
                  {upload.isPending ? 'Validating file…' : 'Parsing mapping…'}
                </>
              ) : (
                <>
                  <ArrowUpFromLine size={16} />
                  Validate and parse
                </>
              )}
            </button>
          </div>

          {result && (
            <div className={parseXml.isError ? 'upload-details-panel' : 'success-panel'} role="status" aria-live="polite" data-testid="status-upload-success">
              <div className="success-heading">
                {parseXml.isError ? <FileText size={17} /> : <Check size={17} />}
                {parseXml.isError ? 'Upload details · parsing failed' : parsed ? 'File received and parsed' : 'File received · parsing…'}
              </div>
              <div className="result-row">
                <div className="file-type-icon"><FileCode2 size={20} /></div>
                <div className="file-info">
                  <p className="file-name" title={result.filename} data-testid="text-upload-filename">{result.filename}</p>
                  <p className="file-meta" data-testid="text-upload-size">{formatBytes(result.size_bytes)}{result.size_bytes >= 1024 ? ` · ${result.size_bytes.toLocaleString()} bytes` : ''}</p>
                </div>
              </div>
            </div>
          )}
        </section>

        <div className="section-rule">Bundled samples</div>
        <div className="sample-list" data-testid="list-samples">
          {samples.isLoading && [0, 1].map((n) => <div key={n} className="sample-skeleton" />)}
          {samples.isError && (
            <div className="error-message" role="alert" data-testid="error-samples">
              <AlertCircle size={17} className="mt-0.5 shrink-0" />
              <span>Samples could not be loaded.</span>
              <button type="button" className="ml-auto shrink-0 font-semibold underline" onClick={() => { void samples.refetch(); }} data-testid="button-retry-samples">Retry</button>
            </div>
          )}
          {samples.data && samples.data.length === 0 && <p className="drop-caption">No bundled samples available.</p>}
          {samples.data?.map((sample) => (
            <div key={sample.filename} className="sample-row" data-testid={`row-sample-${sample.filename}`}>
              <div className="file-info">
                <p className="file-name" title={sample.filename}>{sample.filename}</p>
                <p className="file-meta">{formatBytes(sample.size_bytes)}</p>
              </div>
              <button type="button" className="sample-button" disabled={busy} onClick={() => { void onSample(sample.filename); }} data-testid={`button-try-sample-${sample.filename}`}>
                {activeSample === sample.filename ? <LoaderCircle size={14} className="animate-spin" /> : null}
                Try sample
              </button>
            </div>
          ))}
        </div>

        {(parsed || parseXml.isPending || parseSample.isPending) && (
          <>
            <div className="section-rule">Parsed mapping</div>
            {parsed ? (
              <>
                <div className="project-actions">
                  <button
                    type="button"
                    className="sample-button flex items-center gap-2"
                    onClick={() => { void onDownloadDbtProject(); }}
                    disabled={generatingProject || validatingProject || !parsedSource}
                    data-testid="button-download-dbt-project"
                  >
                    {generatingProject
                      ? <LoaderCircle size={14} className="animate-spin" />
                      : <Download size={14} />}
                    {generatingProject ? 'Building dbt project…' : 'Download dbt project'}
                  </button>
                  <button
                    type="button"
                    className="sample-button flex items-center gap-2"
                    onClick={() => { void onValidateDbtProject(); }}
                    disabled={generatingProject || validatingProject || !parsedSource}
                    data-testid="button-validate-dbt-project"
                  >
                    {validatingProject
                      ? <LoaderCircle size={14} className="animate-spin" />
                      : <Check size={14} />}
                    {validatingProject ? 'Validating with DuckDB…' : 'Validate with DuckDB'}
                  </button>
                </div>
                {generationError && (
                  <div className="error-message mb-3" role="alert" data-testid="error-dbt-project">
                    <AlertCircle size={17} className="mt-0.5 shrink-0" />
                    <span>{generationError}</span>
                  </div>
                )}
                {validationReportError && (
                  <div className="error-message mb-3" role="alert" data-testid="error-dbt-validation">
                    <AlertCircle size={17} className="mt-0.5 shrink-0" />
                    <span>{validationReportError}</span>
                  </div>
                )}
                {validatingProject && (
                  <div
                    className="validation-progress"
                    role="status"
                    aria-live="polite"
                    aria-busy="true"
                    data-testid="status-dbt-validation"
                  >
                    <div className="validation-progress-copy">
                      <LoaderCircle size={17} className="validation-progress-spinner animate-spin" aria-hidden="true" />
                      <div className="validation-progress-text">
                        <strong>DuckDB validation in progress</strong>
                        <span>Seeding synthetic data, compiling, and running models.</span>
                      </div>
                      <span className="validation-progress-elapsed" data-testid="validation-elapsed">
                        {validationElapsedSeconds}s
                      </span>
                    </div>
                    <div className="validation-progress-track" aria-hidden="true">
                      <span className="validation-progress-bar" />
                    </div>
                  </div>
                )}
                {validationReport && (
                  <section
                    className="validation-results"
                    data-testid="dbt-validation-results"
                    aria-label="DuckDB validation results"
                  >
                    <div className="validation-heading">
                      <strong>DuckDB validation</strong>
                      <span className="validation-status" data-status={validationReport.status}>
                        {validationReport.status}
                      </span>
                    </div>
                    <p className="validation-caption">
                      Generated models were seeded with synthetic data, then compiled and run.
                      This checks local DuckDB behavior; it does not certify Snowflake runtime results.
                    </p>
                    <div className="validation-steps" aria-label="Validation steps">
                      {validationReport.steps.map((step) => (
                        <div
                          key={step.name}
                          className="validation-step"
                          data-testid={`validation-step-${step.name}`}
                          data-status={step.status}
                        >
                          <span className="validation-step-name">{step.name}</span>
                          <span className="validation-step-status">{step.status}</span>
                        </div>
                      ))}
                    </div>
                    <h3 className="validation-subhead">Per-model results</h3>
                    {validationReport.models.length > 0 ? (
                      <ul className="validation-model-list">
                        {validationReport.models.map((model) => (
                          <li
                            key={model.path}
                            className="validation-model"
                            data-testid={`validation-model-${model.name}`}
                            data-status={model.status}
                          >
                            <div className="validation-model-head">
                              <span className="validation-model-name">{model.name}</span>
                              <span className="validation-model-status">{model.status}</span>
                            </div>
                            <p className="validation-model-meta">
                              {model.kind}{model.row_count === null ? '' : ` · ${model.row_count} rows`}
                              {model.phase ? ` · ${model.phase}` : ''}
                            </p>
                            {(model.status === 'failed' || model.status === 'blocked') && model.message && (
                              <p className="validation-model-message">{model.message}</p>
                            )}
                          </li>
                        ))}
                      </ul>
                    ) : (
                      <p className="validation-caption">No models were generated for validation.</p>
                    )}
                  </section>
                )}
                <ParseInspector
                  key={parsedLabel + String(parsed.mappings.length)}
                  data={parsed}
                  label={parsedLabel}
                  source={parsedSource}
                  dialect={dbtDialect}
                  onDialectChange={setDbtDialect}
                />
              </>
            ) : (
              <div className="inspector" data-testid="status-parsing"><div className="sample-skeleton" /><div className="sample-skeleton" /><div className="sample-skeleton" /></div>
            )}
          </>
        )}

        <div className="section-rule">What gets parsed</div>
        <div className="flow-strip" aria-label="Parsed structure">
          <div className="flow-step">
            <span className="step-number">01</span>
            <p>Sources and targets<small>Tables, fields, datatypes, precision</small></p>
          </div>
          <div className="flow-step">
            <span className="step-number">02</span>
            <p>Transformations<small>Types, ports and expressions</small></p>
          </div>
          <div className="flow-step">
            <span className="step-number">03</span>
            <p>Connectors<small>Field-level wiring and execution order</small></p>
          </div>
        </div>
        <footer className="footer-note">
          <span>INFA2DBT · MAPPING INSPECTOR</span>
          <span>One file at a time. Files are processed temporarily and not kept after parsing.</span>
        </footer>
      </main>
    </div>
  );
}

function Router() {
  return (
    <RoutedErrorBoundary>
      <Switch>
        <Route path="/" component={Home} />
        <Route component={NotFound} />
      </Switch>
    </RoutedErrorBoundary>
  );
}

function RoutedErrorBoundary({ children }: { children: ReactNode }) {
  const [location] = useLocation();
  return <ErrorBoundary resetKey={location}>{children}</ErrorBoundary>;
}

function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <WouterRouter base={import.meta.env.BASE_URL.replace(/\/$/, '')}>
          <Router />
        </WouterRouter>
        <Toaster />
      </TooltipProvider>
    </QueryClientProvider>
  );
}

export default App;
