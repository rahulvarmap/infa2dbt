import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';

const hooks = vi.hoisted(() => ({
  parseSample: vi.fn(),
}));

vi.mock('@workspace/api-client-react', () => ({
  getGetReadinessQueryKey: () => ['readiness'],
  useGetReadiness: () => ({
    data: { status: 'ready' },
    isError: false,
    isLoading: false,
    refetch: vi.fn(),
  }),
  useListSamples: () => ({
    data: [{ filename: 'sample.xml', size_bytes: 12 }],
    isError: false,
    isLoading: false,
    refetch: vi.fn(),
  }),
  useParseSample: () => ({
    mutateAsync: hooks.parseSample,
    isError: false,
    isPending: false,
    reset: vi.fn(),
  }),
  useParseXml: () => ({
    isError: false,
    isPending: false,
    reset: vi.fn(),
  }),
  useUploadXml: () => ({
    isError: false,
    isPending: false,
    reset: vi.fn(),
  }),
}));

vi.mock('@/components/parse-inspector', () => ({
  ParseInspector: ({ label }: { label: string }) => <div data-testid="parse-inspector">{label}</div>,
}));

import App from './App';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('dbt project download', () => {
  it('submits the parsed sample and downloads the ZIP', async () => {
    hooks.parseSample.mockResolvedValue({ mappings: [], diagnostics: [] });
    const createObjectUrl = vi.fn(() => 'blob:generated-project');
    const revokeObjectUrl = vi.fn();
    const anchorClick = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: createObjectUrl });
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: revokeObjectUrl });
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      headers: { get: () => 'application/zip' },
      blob: async () => new Blob(['zip-content']),
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<App />);
    fireEvent.click(screen.getByTestId('button-try-sample-sample.xml'));
    await screen.findByTestId('button-download-dbt-project');
    fireEvent.click(screen.getByTestId('button-download-dbt-project'));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const [url, options] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/api/dbt/project');
    expect(options.method).toBe('POST');
    expect((options.body as FormData).get('sample_name')).toBe('sample.xml');
    await waitFor(() => expect(createObjectUrl).toHaveBeenCalledTimes(1));
    expect(anchorClick).toHaveBeenCalledTimes(1);
  });
});

describe('DuckDB validation', () => {
  it('submits the selected sample and displays step and per-model results', async () => {
    hooks.parseSample.mockResolvedValue({ mappings: [], diagnostics: [] });
    const report = {
      status: 'failed',
      dialect: 'duckdb',
      steps: [
        { name: 'seed', command: 'seed', status: 'passed', message: 'Synthetic source rows loaded.' },
        { name: 'compile', command: 'compile', status: 'passed', message: 'Compiled.' },
        { name: 'run', command: 'run', status: 'failed', message: 'A model failed.' },
      ],
      models: [
        {
          name: 'stg_member_enrollment',
          kind: 'staging',
          path: 'models/staging/stg_member_enrollment.sql',
          status: 'passed',
          phase: 'run',
          message: 'OK',
          row_count: 6,
        },
        {
          name: 'dim_member',
          kind: 'target',
          path: 'models/targets/dim_member.sql',
          status: 'failed',
          phase: 'run',
          message: 'DuckDB rejected the generated expression.',
          row_count: null,
        },
      ],
    };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => report,
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<App />);
    fireEvent.click(screen.getByTestId('button-try-sample-sample.xml'));
    await screen.findByTestId('button-validate-dbt-project');
    fireEvent.click(screen.getByTestId('button-validate-dbt-project'));

    expect(await screen.findByTestId('dbt-validation-results')).toBeTruthy();
    expect(screen.getByTestId('validation-step-compile').getAttribute('data-status')).toBe('passed');
    expect(screen.getByTestId('validation-model-stg_member_enrollment').textContent).toContain('6 rows');
    expect(screen.getByTestId('validation-model-dim_member').textContent).toContain('failed');
    expect(screen.getByText('DuckDB rejected the generated expression.')).toBeTruthy();
    const [url, options] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/api/dbt/validate');
    expect(options.method).toBe('POST');
    expect((options.body as FormData).get('sample_name')).toBe('sample.xml');
  });
});
