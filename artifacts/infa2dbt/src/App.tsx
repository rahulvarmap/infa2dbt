import { useEffect, useRef, useState, type DragEvent, type ChangeEvent } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import {
  getGetHealthQueryKey,
  getHealthCheckQueryKey,
  useGetHealth,
  useHealthCheck,
  useUploadXml,
} from '@workspace/api-client-react';
import {
  AlertCircle,
  ArrowUpFromLine,
  Check,
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

function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} bytes`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

function messageFromError(error: unknown) {
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

  const health = useGetHealth({
    query: { queryKey: getGetHealthQueryKey(), refetchInterval: 30_000, retry: 1 },
  });
  const healthCheck = useHealthCheck({
    query: { queryKey: getHealthCheckQueryKey(), refetchInterval: 30_000, retry: 1 },
  });
  const upload = useUploadXml();

  useEffect(() => {
    const saved = window.localStorage.getItem('infa2dbt-theme');
    setDark(saved === 'dark');
  }, []);

  useEffect(() => {
    document.documentElement.classList.toggle('dark', dark);
    window.localStorage.setItem('infa2dbt-theme', dark ? 'dark' : 'light');
  }, [dark]);

  const apiReady = Boolean(health.data && healthCheck.data);
  const apiError = Boolean(health.isError || healthCheck.isError);
  const apiLoading = health.isLoading || healthCheck.isLoading;
  const apiState = apiReady ? 'ready' : apiError ? 'error' : 'checking';

  function selectFile(candidate: File | undefined) {
    setValidationError('');
    setResult(null);
    upload.reset();
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

  function onSubmit() {
    if (!file || upload.isPending) return;
    setValidationError('');
    setResult(null);
    upload.mutate(
      { data: { file } },
      {
        onSuccess: (response) => setResult(response),
      },
    );
  }

  function clearFile() {
    setFile(null);
    setResult(null);
    setValidationError('');
    upload.reset();
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
          Send an Informatica XML file through a simple, secure intake. We validate the upload and return its file details—nothing more.
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
                  <button type="button" className="remove-file" onClick={clearFile} aria-label="Remove selected file" data-testid="button-remove-file">
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
                onClick={() => { void health.refetch(); void healthCheck.refetch(); }}
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

          <div className="action-bar">
            <div className="privacy-note">
              <LockKeyhole size={15} className="shrink-0" />
              <span>File contents are checked in memory, not saved.</span>
            </div>
            <button
              type="button"
              className="primary-action"
              onClick={onSubmit}
              disabled={!file || upload.isPending}
              data-testid="button-upload-xml"
            >
              {upload.isPending ? (
                <>
                  <LoaderCircle size={16} className="animate-spin" />
                  Validating file…
                </>
              ) : (
                <>
                  <ArrowUpFromLine size={16} />
                  Validate XML
                </>
              )}
            </button>
          </div>

          {result && (
            <div className="success-panel" role="status" aria-live="polite" data-testid="status-upload-success">
              <div className="success-heading"><Check size={17} /> File received and validated</div>
              <div className="result-row">
                <div className="file-type-icon"><FileCode2 size={20} /></div>
                <div className="file-info">
                  <p className="file-name" title={result.filename} data-testid="text-upload-filename">{result.filename}</p>
                  <p className="file-meta" data-testid="text-upload-size">{formatBytes(result.size_bytes)} · {result.size_bytes.toLocaleString()} bytes</p>
                </div>
              </div>
            </div>
          )}
        </section>

        <div className="section-rule">The intake, in three steps</div>
        <div className="flow-strip" aria-label="Upload process">
          <div className="flow-step">
            <span className="step-number">01</span>
            <p>Select an XML<small>Choose a file up to 5 MB</small></p>
          </div>
          <div className="flow-step">
            <span className="step-number">02</span>
            <p>Validate upload<small>We check the file request</small></p>
          </div>
          <div className="flow-step">
            <span className="step-number">03</span>
            <p>Review details<small>Filename and byte count returned</small></p>
          </div>
        </div>
        <footer className="footer-note">
          <span>INFA2DBT · FILE INTAKE</span>
          <span>One file at a time. No content stored.</span>
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
