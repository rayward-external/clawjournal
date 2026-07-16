import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from './api.ts';

describe('automatic-upload API normalization', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('preserves selected hook targets and safely normalizes malformed hook rows', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        mode: 'enabled',
        hooks: [
          {
            agent: 'claude',
            selected: true,
            configured: true,
            installed: true,
            last_observed_at: '2026-07-15T00:00:00Z',
          },
          {
            agent: 'codex',
            selected: 'yes',
            configured: 1,
            installed: false,
            last_observed_at: 42,
            diagnostic: 99,
          },
          null,
          { selected: true },
        ],
      }),
    });
    vi.stubGlobal('fetch', fetchMock);

    const status = await api.autoUpload.status();

    expect(fetchMock).toHaveBeenCalledWith('/api/auto-upload/status', {
      headers: {},
    });
    expect(status.hooks).toEqual([
      {
        agent: 'claude',
        selected: true,
        configured: true,
        installed: true,
        last_observed_at: '2026-07-15T00:00:00Z',
      },
      {
        agent: 'codex',
        selected: false,
        configured: false,
        installed: false,
        last_observed_at: null,
      },
    ]);
  });
});
