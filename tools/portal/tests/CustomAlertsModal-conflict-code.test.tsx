/**
 * #3202: the Reef 6 base_hash conflict box is opened by code CONFLICT (with
 * current_source_hash), not by remedy refresh_retry. Here the contract is
 * extended with a second refresh_retry code — the shape a future code would
 * take — and that code, even with current_source_hash, must not open the box.
 * The mock is file-wide, hence a file of its own.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

vi.mock('../src/interactive/tools/_common/data/tenant-api-errors.json', async (importOriginal) => {
  const actual = (await importOriginal()) as any;
  const contract = actual.default ?? actual;
  return {
    default: {
      ...contract,
      codes: [
        ...contract.codes,
        { code: 'FUTURE_REFRESH_RETRY', remedy: 'refresh_retry', retryable: 'always', statuses: [409], retry_after_s: null, kinds: [] },
      ],
    },
  };
});

import { CustomAlertsModal } from '../src/interactive/tools/tenant-manager/components/CustomAlertsModal.jsx';
import { classifyApiError } from '../src/interactive/tools/_common/api/classify-error.js';

describe('CustomAlertsModal — conflict box is keyed on code CONFLICT', () => {
  it('a second refresh_retry code with current_source_hash shows the remedy message, not the conflict box', async () => {
    // the mock took effect: the extra code classifies as refresh_retry
    expect(classifyApiError(409, { code: 'FUTURE_REFRESH_RETRY' }).remedy).toBe('refresh_retry');
    const ft = vi.fn(() => Promise.resolve({ custom_alerts: [], source_hash: 'h1' }));
    // Not through apiFixture: the code is not in swagger's enum, and
    // current_source_hash is an envelope Extra field.
    const save = vi.fn(() => Promise.resolve({
      ok: false, status: 409,
      data: { error: 'x', code: 'FUTURE_REFRESH_RETRY', current_source_hash: 'hX' },
    }));
    render(<CustomAlertsModal tenantId="tenant-x" onClose={() => {}} fetchTenant={ft} fetchMetrics={vi.fn(() => Promise.resolve([]))} saveCustomAlerts={save} />);
    await waitFor(() => expect(screen.getByTestId('save')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('save'));
    await waitFor(() => expect(screen.getByTestId('save-error')).toBeInTheDocument());
    expect(screen.getByTestId('save-error').getAttribute('data-remedy')).toBe('refresh_retry');
    expect(screen.queryByTestId('conflict')).toBeNull();
  });
});
