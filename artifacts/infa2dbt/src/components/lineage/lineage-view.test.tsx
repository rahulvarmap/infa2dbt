import { spawn, type ChildProcess } from 'node:child_process';
import { readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import type { ComponentType, MouseEvent, ReactNode } from 'react';
import type { Window as HappyWindow } from 'happy-dom';
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, configure, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { setBaseUrl, type ParseResponse } from '@workspace/api-client-react';
import type { Edge, Node } from '@xyflow/react';
import { ParseInspector } from '../parse-inspector';
import { LineageView } from './lineage-view';
import { orderKey, type ParsedSource } from './lineage-utils';

// Real parse/trace work now includes isolated Python worker startup.
configure({ asyncUtilTimeout: 12_000 });

// Mock only the canvas/measurement layer. The component, generated mutation,
// multipart serialization, and Python trace endpoint are all exercised for real.
const viewport = vi.hoisted(() => ({ setViewport: vi.fn(), viewportInitialized: false }));
vi.mock('@xyflow/react', async () => {
  const actual = await vi.importActual<typeof import('@xyflow/react')>('@xyflow/react');
  return {
    ...actual,
    ReactFlowProvider: ({ children }: { children: ReactNode }) => <>{children}</>,
    Handle: () => null,
    Background: () => null,
    Controls: () => null,
    useReactFlow: () => viewport,
    ReactFlow: ({ nodes, edges, nodeTypes, onNodeClick }: {
      nodes: Node[];
      edges: Edge[];
      nodeTypes: Record<string, ComponentType<{ id: string; data: Record<string, unknown> }>>;
      onNodeClick: (event: MouseEvent<HTMLButtonElement>, node: Node) => void;
    }) => (
      <div className="react-flow">
        {nodes.map((node) => {
          const Renderer = nodeTypes[node.type!];
          return <button key={node.id} onClick={(event) => onNodeClick(event, node)}><Renderer id={node.id} data={node.data} /></button>;
        })}
        {edges.map((edge) => <span key={edge.id} data-testid="graph-edge" className={edge.className} data-animated={String(edge.animated)} />)}
      </div>
    ),
  };
});

let server: ChildProcess;
let origin: string;
let data: ParseResponse;
let xml: Uint8Array<ArrayBuffer>;
const realFetch = globalThis.fetch.bind(globalThis);
const sample: ParsedSource = { kind: 'sample', name: 'm_member_enrollment.xml' };

beforeAll(async () => {
  const root = resolve(process.cwd(), '../..');
  // A private ephemeral port keeps tests independent of the preview/workflows.
  server = spawn('python', ['-m', 'uvicorn', 'artifacts.infa2dbt.api.main:app', '--host', '127.0.0.1', '--port', '0'], { cwd: root, stdio: ['ignore', 'ignore', 'pipe'] });
  origin = await new Promise<string>((resolve, reject) => {
    let logs = '';
    server.stderr!.on('data', (chunk) => {
      logs += String(chunk);
      const match = logs.match(/http:\/\/127\.0\.0\.1:\d+/);
      if (match) resolve(match[0]);
    });
    server.once('error', reject);
    server.once('exit', (code) => reject(new Error(`Test API exited (${code}): ${logs}`)));
  });
  setBaseUrl(origin);
  (window as unknown as HappyWindow).happyDOM.setURL(origin);
  xml = new Uint8Array(await readFile(`${root}/samples/m_member_enrollment.xml`));
  const body = new FormData();
  body.append('file', new File([xml], 'sample.xml', { type: 'text/xml' }));
  const response = await realFetch(`${origin}/api/parse`, { method: 'POST', body });
  expect(response.status).toBe(200);
  data = await response.json();
}, 15_000);

afterAll(async () => {
  setBaseUrl('');
  if (server && server.exitCode === null) {
    const exited = new Promise<void>((resolve) => server.once('exit', () => resolve()));
    server.kill('SIGTERM');
    await exited;
  }
});

beforeEach(() => { vi.stubGlobal('fetch', vi.fn(realFetch)); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

function show(source: ParsedSource | null = sample) {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false }, queries: { retry: false } } });
  render(<QueryClientProvider client={client}><LineageView data={data} mapping={data.mappings[0]} source={source} /></QueryClientProvider>);
  fireEvent.click(screen.getByTestId('node-lineage-DIM_MEMBER'));
}

function showInspector(inspectorData: ParseResponse) {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false }, queries: { retry: false } } });
  render(<QueryClientProvider client={client}><ParseInspector data={inspectorData} label="sample.xml" source={sample} /></QueryClientProvider>);
}

function traceBody() {
  const call = vi.mocked(fetch).mock.calls.find(([url]) => String(url).endsWith('/lineage/trace'));
  expect(call?.[0]).toBe(`${origin}/api/lineage/trace`);
  expect(call?.[1]?.method).toBe('POST');
  return call![1]!.body as FormData;
}

describe('target column tracing', { timeout: 15_000 }, () => {
  it('clicks FULL_NAME, calls the real trace endpoint, and shows BOTH source fields directly under the column', async () => {
    show();
    const column = screen.getByTestId('button-trace-field-FULL_NAME');
    expect(column.textContent).toContain('column');
    expect(column.textContent).not.toContain('INPUT');
    fireEvent.click(column);
    const hops = await within(column.closest('li')!).findByTestId('list-trace-hops');
    expect(within(hops).getByText('MEMBER_ENROLLMENT.FIRST_NAME')).toBeTruthy();
    expect(within(hops).getByText('MEMBER_ENROLLMENT.LAST_NAME')).toBeTruthy();
    expect(hops.querySelectorAll('li')).toHaveLength(9);
    expect(hops.textContent).toContain("LTRIM(RTRIM(FIRST_NAME)) || ' ' || LTRIM(RTRIM(LAST_NAME))");
    expect(column.getAttribute('aria-expanded')).toBe('true');
    const body = traceBody();
    expect(body.get('target_instance')).toBe('DIM_MEMBER');
    expect(body.get('target_field')).toBe('FULL_NAME');
    expect(body.get('sample_name')).toBe(sample.name);
    expect(body.has('file')).toBe(false);
    expect(screen.getAllByTestId('graph-edge').every((edge) => edge.classList.contains('lg-edge-lit') && edge.dataset.animated === 'true')).toBe(true);
  });

  it('traces an uploaded XML file rather than silently substituting a sample', async () => {
    show({ kind: 'file', file: new File([xml], 'my-mapping.xml', { type: 'text/xml' }) });
    fireEvent.click(screen.getByTestId('button-trace-field-FULL_NAME'));
    const hops = await screen.findByTestId('list-trace-hops');
    expect(within(hops).getByText('MEMBER_ENROLLMENT.FIRST_NAME')).toBeTruthy();
    expect(within(hops).getByText('MEMBER_ENROLLMENT.LAST_NAME')).toBeTruthy();
    const body = traceBody();
    expect(body.get('file')).toBeInstanceOf(File);
    expect(body.has('sample_name')).toBe(false);
  });

  it('dims non-path elements, animates path edges, and clears the trace without closing the panel', async () => {
    show();
    fireEvent.click(screen.getByTestId('button-trace-field-LOAD_DTS'));
    await screen.findByTestId('list-trace-hops');
    expect(screen.getByTestId('node-lineage-MEMBER_ENROLLMENT').dataset.state).toBe('dim');
    expect(screen.getByTestId('node-lineage-SQ_MEMBER_ENROLLMENT').dataset.state).toBe('dim');
    expect(screen.getAllByTestId('graph-edge').filter((edge) => edge.classList.contains('lg-edge-dim'))).toHaveLength(2);
    fireEvent.click(screen.getByRole('button', { name: 'Clear trace' }));
    expect(screen.queryByTestId('list-trace-hops')).toBeNull();
    expect(screen.getByTestId('panel-node-detail')).toBeTruthy();
    expect(screen.getByTestId('node-lineage-MEMBER_ENROLLMENT').dataset.state).toBe('idle');
    expect(screen.getAllByTestId('graph-edge').every((edge) => edge.dataset.animated === 'false' && !edge.classList.contains('lg-edge-dim'))).toBe(true);
  });

  it('shows a missing-input explanation instead of leaving the click disabled or ignored', async () => {
    show(null);
    fireEvent.click(screen.getByTestId('button-trace-field-FULL_NAME'));
    expect(await screen.findByRole('alert')).toBeTruthy();
    expect(screen.getByTestId('error-trace').textContent).toContain('upload the XML again');
    expect(vi.mocked(fetch).mock.calls).toHaveLength(0);
  });

  it('shows an inline API error and retries the clicked column successfully', async () => {
    show();
    vi.mocked(fetch).mockResolvedValueOnce(new Response(JSON.stringify({ error: 'Temporary trace failure' }), { status: 503, headers: { 'Content-Type': 'application/json' } }));
    fireEvent.click(screen.getByTestId('button-trace-field-FULL_NAME'));
    expect((await screen.findByRole('alert')).textContent).toContain('Temporary trace failure');
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull());
    expect(within(await screen.findByTestId('list-trace-hops')).getByText('MEMBER_ENROLLMENT.LAST_NAME')).toBeTruthy();
  });

  it('shows diagnostics scoped to the selected mapping', () => {
    const first = data.mappings[0];
    const second = { ...first, name: 'second_mapping', folder: 'other_folder' };
    const inspectorData: ParseResponse = {
      ...data,
      mappings: [first, second],
      diagnostics: [
        {
          severity: 'warning',
          code: 'FIRST_MAPPING_WARNING',
          message: 'First mapping warning.',
          mapping: orderKey(data, first),
          instance: 'EXP_MEMBER_DERIVE',
          field: 'FULL_NAME',
        },
        {
          severity: 'error',
          code: 'SECOND_MAPPING_ERROR',
          message: 'Second mapping error.',
          mapping: second.name,
          instance: 'EXP_MEMBER_DERIVE',
          field: 'FULL_NAME',
        },
      ],
    };
    showInspector(inspectorData);
    expect(screen.getByTestId('diagnostic-first_mapping_warning')).toBeTruthy();
    expect(screen.queryByTestId('diagnostic-second_mapping_error')).toBeNull();
    fireEvent.change(screen.getByLabelText('Mapping'), { target: { value: '1' } });
    expect(screen.queryByTestId('diagnostic-first_mapping_warning')).toBeNull();
    expect(screen.getByTestId('diagnostic-second_mapping_error')).toBeTruthy();
    expect(screen.getByText('Diagnostics · second_mapping')).toBeTruthy();
  });
});
