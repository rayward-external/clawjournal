import { render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import App from './App.tsx';
import { api } from './api.ts';
import type { AdvisorData, Features, IndexHealth, Stats } from './types.ts';

function features(indexHealth: IndexHealth): Features {
  return {
    benchmark_tab_enabled: true,
    scoring_warmup_declined: true,
    index_health: indexHealth,
  };
}

const emptyStats: Stats = {
  total: 0,
  by_status: {},
  by_source: {},
  by_project: {},
  by_task_type: {},
};

const emptyAdvisor: AdvisorData = {
  generated_at: '2026-07-30T00:00:00Z',
  period: 'test',
  headline: '',
  recommendations: [],
  summary_stats: {
    total_cost_usd: 0,
    total_sessions: 0,
    cost_per_session: 0,
    most_efficient_model: null,
    highest_quality_model: null,
    potential_savings_usd: 0,
  },
};

afterEach(() => {
  vi.restoreAllMocks();
  window.history.replaceState({}, '', '/');
});

describe('workbench index startup gate', () => {
  it('does not mount any DB-backed UI while recovery is required', async () => {
    vi.spyOn(api, 'features').mockResolvedValue(features({
      status: 'recovery_required',
      message: 'The workbench index is damaged and must be rebuilt.',
      automatic_recovery_available: true,
    }));
    const stats = vi.spyOn(api, 'stats');
    const advisor = vi.spyOn(api, 'advisor');
    const sessions = vi.spyOn(api.sessions, 'list');
    const scoringWarmup = vi.spyOn(api, 'scoringWarmup');

    render(<App />);

    expect(await screen.findByRole('heading', { name: 'Your local index needs repair' })).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Sessions' })).not.toBeInTheDocument();
    expect(stats).not.toHaveBeenCalled();
    expect(advisor).not.toHaveBeenCalled();
    expect(sessions).not.toHaveBeenCalled();
    expect(scoringWarmup).not.toHaveBeenCalled();
  });

  it('mounts the normal workbench only after a successful ready probe', async () => {
    vi.spyOn(api, 'features').mockResolvedValue(features({ status: 'ready' }));
    vi.spyOn(api, 'stats').mockResolvedValue(emptyStats);
    vi.spyOn(api, 'advisor').mockResolvedValue(emptyAdvisor);
    vi.spyOn(api.sessions, 'list').mockResolvedValue([]);
    vi.spyOn(api, 'scoringWarmup').mockResolvedValue({ status: 'declined' });
    localStorage.setItem('cj.gettingStartedGuideV2Dismissed', '1');

    render(<App />);

    expect(screen.getByText('Checking the local index...')).toBeInTheDocument();
    expect(await screen.findByRole('link', { name: 'Sessions' })).toBeInTheDocument();
    await waitFor(() => expect(api.sessions.list).toHaveBeenCalled());
  });

  it('polls rebuilding health until the verified index is ready', async () => {
    const rebuilding = features({
      status: 'rebuilding',
      stage: 'reindexing',
      message: 'Rebuilding from source logs...',
    });
    const ready = features({ status: 'ready' });
    const featureProbe = vi.spyOn(api, 'features')
      .mockResolvedValueOnce(rebuilding)
      // The polling cadence switches as soon as the first result changes the
      // status, which causes one immediate fresh probe before the 1s interval.
      .mockResolvedValueOnce(rebuilding)
      .mockResolvedValue(ready);
    vi.spyOn(api, 'stats').mockResolvedValue(emptyStats);
    vi.spyOn(api, 'advisor').mockResolvedValue(emptyAdvisor);
    vi.spyOn(api.sessions, 'list').mockResolvedValue([]);
    vi.spyOn(api, 'scoringWarmup').mockResolvedValue({ status: 'declined' });
    localStorage.setItem('cj.gettingStartedGuideV2Dismissed', '1');

    render(<App />);

    expect(await screen.findByRole('heading', { name: 'Rebuilding your local index' })).toBeInTheDocument();
    await waitFor(() => expect(featureProbe).toHaveBeenCalledTimes(2));
    await waitFor(
      () => expect(screen.getByRole('link', { name: 'Sessions' })).toBeInTheDocument(),
      { timeout: 3_000 },
    );
    expect(featureProbe.mock.calls.length).toBeGreaterThanOrEqual(3);
  });

  it('surfaces the backup and recovery warnings without another required action', async () => {
    vi.spyOn(api, 'features').mockResolvedValue(features({
      status: 'ready',
      backup_path: 'D:/state/index-backups/recovery-1',
      restored_state_counts: { session_decisions: 3, policies: 1 },
      warnings: ['Automatic uploads were restored paused and must be reviewed.'],
    }));
    vi.spyOn(api, 'stats').mockResolvedValue(emptyStats);
    vi.spyOn(api, 'advisor').mockResolvedValue(emptyAdvisor);
    vi.spyOn(api.sessions, 'list').mockResolvedValue([]);
    vi.spyOn(api, 'scoringWarmup').mockResolvedValue({ status: 'declined' });
    localStorage.setItem('cj.gettingStartedGuideV2Dismissed', '1');

    render(<App />);

    expect(await screen.findByText('Index rebuilt and verified.')).toBeInTheDocument();
    expect(screen.getByText(/Restored 4 saved state records/)).toBeInTheDocument();
    expect(screen.getByText('Automatic uploads were restored paused and must be reviewed.')).toBeInTheDocument();
  });
});
