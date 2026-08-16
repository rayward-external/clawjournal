import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from '../api.ts';
import type { ScoringQueueStatus as QueueStatus } from '../types.ts';
import { ToastProvider } from './Toast.tsx';
import { ScoringQueueStatus } from './ScoringQueueStatus.tsx';

const status: QueueStatus = {
  enabled: true,
  backend: 'codex',
  worker_state: 'running',
  counts: { pending: 382, running: 1, retry_wait: 15, succeeded: 20, failed: 2 },
  current: { job_id: 'opaque-job', stage: 'locating_evidence', progress_current: 2, progress_total: 5 },
  next_retry_at: null,
  action_required_code: null,
};

describe('ScoringQueueStatus', () => {
  afterEach(() => vi.restoreAllMocks());

  it('shows bounded aggregate progress without session identifiers', async () => {
    vi.spyOn(api, 'scoringStatus').mockResolvedValue(status);
    render(<ToastProvider><ScoringQueueStatus compact /></ToastProvider>);

    expect(await screen.findByText(/382 pending/)).toHaveTextContent('1 scoring');
    expect(screen.getByText('Locating evidence 2/5')).toBeInTheDocument();
    expect(screen.queryByText('opaque-job')).not.toBeInTheDocument();
  });

  it('can retry failed jobs and replaces the snapshot with the response', async () => {
    vi.spyOn(api, 'scoringStatus').mockResolvedValue(status);
    const control = vi.spyOn(api, 'scoringControl').mockResolvedValue({
      ok: true,
      action: 'retry_failed',
      retried: 2,
      status: { ...status, counts: { ...status.counts, pending: 384, failed: 0 } },
    });
    render(<ToastProvider><ScoringQueueStatus controls /></ToastProvider>);

    fireEvent.click(await screen.findByRole('button', { name: 'Retry failed' }));
    await waitFor(() => expect(control).toHaveBeenCalledWith('retry_failed'));
    expect(await screen.findByText(/384 pending/)).toBeInTheDocument();
  });

  it('does not block its parent when status cannot be loaded', async () => {
    vi.spyOn(api, 'scoringStatus').mockRejectedValue(new Error('raw private failure'));
    render(<ToastProvider><ScoringQueueStatus compact /></ToastProvider>);
    await waitFor(() => expect(api.scoringStatus).toHaveBeenCalled());
    expect(screen.queryByText(/raw private failure/)).not.toBeInTheDocument();
  });
});
