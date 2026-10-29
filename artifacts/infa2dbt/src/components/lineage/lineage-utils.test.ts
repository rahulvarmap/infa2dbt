import { describe, expect, it } from 'vitest';
import type { Mapping, ParseResponse } from '@workspace/api-client-react';
import { normalizeIdentifier, orderKey, resolvePorts } from './lineage-utils';

describe('identifier matching in the lineage UI', () => {
  it('uses locale-independent lowercase normalization', () => {
    expect(normalizeIdentifier('EXP_MEMBER')).toBe('exp_member');
  });

  it('resolves instance definitions and connectors without case-sensitive mismatches', () => {
    const mapping = {
      name: 'case_mapping',
      repository: '',
      folder: '',
      sources: [],
      targets: [],
      transformations: [{
        name: 'EXPR_DEF',
        type: 'Expression',
        ports: [{ name: 'Output', datatype: 'string', precision: '', scale: '', porttype: 'OUTPUT', expression: 'Input' }],
        attributes: {},
      }],
      instances: [{ name: 'exp', type: 'TRANSFORMATION', transformation_name: 'expr_def', transformation_type: '' }],
      connectors: [
        { from_instance: 'EXP', from_field: 'Output', to_instance: 't', to_field: 'Name' },
      ],
      attributes: {},
    } as Mapping;

    expect(resolvePorts(mapping, 'EXP', 'Expression').rows.map((row) => row.name)).toEqual(['Output']);
    expect(resolvePorts(mapping, 'T', 'Unknown').rows.map((row) => row.name)).toEqual(['Name']);
  });

  it('creates duplicate mapping keys consistently when names differ only by case', () => {
    const first = { name: 'DailyLoad', repository: 'repo', folder: 'one' } as Mapping;
    const second = { name: 'dailyload', repository: 'repo', folder: 'two' } as Mapping;
    const response = { mappings: [first, second] } as ParseResponse;
    expect(orderKey(response, first)).toBe('repo/one/DailyLoad');
    expect(orderKey(response, second)).toBe('repo/two/dailyload');
  });
});
