import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useState } from 'react';
import type { ParseResponse } from '@workspace/api-client-react';
import type { ParsedSource } from '@/components/lineage/lineage-utils';
import { ParseInspector } from '@/components/parse-inspector';
import type { DbtDialect } from '@/components/dbt-project-files';

vi.mock('@/components/lineage/lineage-view', () => ({
  LineageView: () => <div data-testid="mock-lineage" />,
}));

const mappingData = {
  mappings: [{
    name: 'm_member_enrollment',
    repository: 'DEMO_REPO',
    folder: 'HEALTHCARE_DEMO',
    sources: [],
    targets: [],
    transformations: [],
    instances: [],
    connectors: [],
  }],
  execution_order: { m_member_enrollment: [] },
  diagnostics: [],
  lineage: { nodes: [], edges: [] },
} as unknown as ParseResponse;

const sampleSource: ParsedSource = {
  kind: 'sample',
  name: 'm_member_enrollment.xml',
};

function projectFor(dialect: DbtDialect) {
  const targetSql = `-- BLOCKED MODEL: dim_member\nselect '${dialect}' as generated_dialect\n`;
  const files = {
    'dbt_project.yml': 'name: infa2dbt\n',
    'models/sources.yml': 'version: 2\nsources: []\n',
    'models/schema.yml': 'version: 2\nmodels: []\n',
    'models/marts/dim_member.sql': targetSql,
    'models/staging/stg_member_enrollment.sql': 'select * from source_table\n',
    'diagnostics.json': JSON.stringify({
      format_version: 1,
      warehouse: dialect,
      blocked_models: [{
        name: 'dim_member',
        path: 'models/marts/dim_member.sql',
        kind: 'target',
        mapping: 'm_member_enrollment',
        reasons: ['UNSUPPORTED_TRANSFORMATION'],
      }],
      diagnostics: [{
        severity: 'error',
        code: 'UNSUPPORTED_TRANSFORMATION',
        message: 'Lookup transformation is not supported.',
        mapping: 'm_member_enrollment',
      }],
    }),
  };
  return {
    dialect,
    files,
    blocked_models: [{
      name: 'dim_member',
      path: 'models/marts/dim_member.sql',
      kind: 'target',
      mapping: 'm_member_enrollment',
      reasons: ['UNSUPPORTED_TRANSFORMATION'],
    }],
    diagnostics: [{
      severity: 'error',
      code: 'UNSUPPORTED_TRANSFORMATION',
      message: 'Lookup transformation is not supported.',
      mapping: 'm_member_enrollment',
    }],
  };
}

function TestInspector() {
  const [dialect, setDialect] = useState<DbtDialect>('snowflake');
  return (
    <ParseInspector
      data={mappingData}
      label="sample / m_member_enrollment.xml"
      source={sampleSource}
      dialect={dialect}
      onDialectChange={setDialect}
    />
  );
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('dbt SQL inspector tab', () => {
  it('lists generated models, highlights SQL, shows blocked diagnostics, and copies a file', async () => {
    const payload = projectFor('snowflake');
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => payload,
    });
    vi.stubGlobal('fetch', fetchMock);
    const previousClipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard');
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    });

    try {
      const { container } = render(<TestInspector />);
      fireEvent.click(screen.getByTestId('tab-dbt-sql'));

      expect(await screen.findByTestId('dbt-code-viewer')).toBeTruthy();
      expect(screen.getAllByText('models/marts/dim_member.sql').length).toBeGreaterThan(0);
      expect(screen.getByText('models/staging/stg_member_enrollment.sql')).toBeTruthy();
      expect(screen.getByText('models/sources.yml')).toBeTruthy();
      expect(screen.getByText('models/schema.yml')).toBeTruthy();
      expect(screen.getByText('dbt_project.yml')).toBeTruthy();
      expect(screen.getByTestId('dbt-blocked-reason').textContent).toContain(
        'Lookup transformation is not supported.',
      );
      expect(screen.getByTestId('dbt-blocked-reason').textContent).toContain(
        'UNSUPPORTED_TRANSFORMATION',
      );
      expect(container.querySelector('.dbt-token-keyword')?.textContent).toBe('select');
      expect(screen.getByTestId('dbt-code-viewer').getAttribute('data-syntax')).toBe('sql');
      expect(container.querySelector('.dbt-code-line-number')?.textContent).toBe('1');

      fireEvent.click(screen.getByRole('button', {
        name: 'Copy models/marts/dim_member.sql',
      }));
      await waitFor(() => expect(writeText).toHaveBeenCalledWith(payload.files['models/marts/dim_member.sql']));
      const [url, options] = fetchMock.mock.calls[0] as [string, RequestInit];
      expect(url).toBe('/api/dbt/project/files');
      expect((options.body as FormData).get('sample_name')).toBe('m_member_enrollment.xml');
      expect((options.body as FormData).get('dialect')).toBe('snowflake');
    } finally {
      if (previousClipboard) {
        Object.defineProperty(navigator, 'clipboard', previousClipboard);
      } else {
        Reflect.deleteProperty(navigator, 'clipboard');
      }
    }
  });

  it('reloads the same generated file set for the selected dialect', async () => {
    const snowflake = projectFor('snowflake');
    const duckdb = projectFor('duckdb');
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({ ok: true, json: async () => snowflake })
      .mockResolvedValueOnce({ ok: true, json: async () => duckdb });
    vi.stubGlobal('fetch', fetchMock);

    render(<TestInspector />);
    fireEvent.click(screen.getByTestId('tab-dbt-sql'));
    await screen.findByTestId('dbt-code-viewer');
    fireEvent.click(screen.getByTestId('dbt-dialect-duckdb'));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getByTestId('dbt-code-viewer').textContent).toContain('duckdb'));
    expect(screen.getByTestId('dbt-dialect-duckdb').getAttribute('aria-pressed')).toBe('true');
    const [, secondOptions] = fetchMock.mock.calls[1] as [string, RequestInit];
    expect((secondOptions.body as FormData).get('dialect')).toBe('duckdb');
  });
});
