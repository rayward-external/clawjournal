import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from './api.ts';

describe('desktop open notification API', () => {
  afterEach(() => {
    delete window.__CLAWJOURNAL_API_TOKEN__;
    vi.unstubAllGlobals();
  });

  it('records a real SPA mount through the authenticated POST endpoint', async () => {
    window.__CLAWJOURNAL_API_TOKEN__ = 'desktop-test-token';
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ ok: true, scheduled: true }),
    });
    vi.stubGlobal('fetch', fetchMock);

    await expect(api.desktopOpened()).resolves.toEqual({ ok: true, scheduled: true });
    expect(fetchMock).toHaveBeenCalledWith('/api/desktop/opened', {
      method: 'POST',
      headers: { Authorization: 'Bearer desktop-test-token' },
    });
  });
});

describe('automatic-upload API normalization', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('preserves exact scope and selected hooks while safely normalizing malformed rows', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        mode: 'enabled',
        scope: {
          sources: ['claude', 'codex'],
          projects: ['alpha', 'beta'],
          entries: [
            ['claude', 'alpha'],
            ['codex', 'beta'],
            ['codex', 42],
            ['claude', 'alpha', 'extra'],
          ],
        },
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
    expect(status.scope).toEqual({
      sources: ['claude', 'codex'],
      projects: ['alpha', 'beta'],
      entries: [
        ['claude', 'alpha'],
        ['codex', 'beta'],
      ],
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

describe('share scanner recovery API', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('requests installation of the pinned managed scanners', async () => {
    const payload = {
      ok: true,
      missing: [],
      scanners: {
        betterleaks: {
          ok: true,
          status: 'installed',
          install_attempted: true,
          available: true,
          managed: true,
        },
      },
    };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => payload,
    });
    vi.stubGlobal('fetch', fetchMock);

    await expect(api.share.installScanners()).resolves.toEqual(payload);
    expect(fetchMock).toHaveBeenCalledWith('/api/share/scanners/install', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    });
  });
});
