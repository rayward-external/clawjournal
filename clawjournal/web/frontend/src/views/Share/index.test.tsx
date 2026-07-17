import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from '../../api.ts';
import { ToastProvider } from '../../components/Toast.tsx';
import type { ReadySession, ShareReadyStats } from './types.ts';
import { Share } from './index.tsx';

function LocationProbe() {
  return <output data-testid="location-search">{useLocation().search}</output>;
}

function readySession(id: string): ReadySession {
  return {
    session_id: id,
    project: 'project-a',
    model: 'gpt-test',
    source: 'codex',
    display_title: `Trace ${id}`,
    ai_quality_score: 4,
    ai_failure_value_score: 3,
    ai_recovery_labels: [],
    ai_failure_attribution: null,
    ai_failure_modes: [],
    ai_learning_summary: null,
    user_messages: 1,
    assistant_messages: 1,
    tool_uses: 0,
    input_tokens: 100,
    output_tokens: 50,
    outcome_badge: 'resolved',
    start_time: '2026-07-15T12:00:00Z',
    review_status: 'approved',
  };
}

function readyStats(count = 12): ShareReadyStats {
  const sessions = Array.from({ length: count }, (_, index) => readySession(`s${index + 1}`));
  return {
    count: sessions.length,
    total_approved: sessions.length,
    projects: ['project-a'],
    models: ['gpt-test'],
    recommended_session_ids: ['s1'],
    sessions,
  };
}

function mockInitialLoad(stats: ShareReadyStats) {
  vi.spyOn(api, 'shareReady').mockResolvedValue(
    stats as Awaited<ReturnType<typeof api.shareReady>>,
  );
  vi.spyOn(api.shares, 'list').mockResolvedValue([]);
  vi.spyOn(api, 'scoringBackend').mockResolvedValue({ backend: null, display_name: null });
  vi.spyOn(api, 'shareDestination').mockResolvedValue({
    configured: false,
    daemon_upload_supported: false,
    submissions_open: false,
    preferred_upload_flow: 'manual',
    cli_ingest_supported: false,
    share_page_url: null,
  });
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('Share selection defaults', () => {
  it('opens the picker with every available trace selected and lets the user deselect', async () => {
    mockInitialLoad(readyStats());

    render(
      <MemoryRouter initialEntries={['/share']}>
        <ToastProvider><Share /></ToastProvider>
        <LocationProbe />
      </MemoryRouter>,
    );

    expect(await screen.findByRole('heading', { name: 'What would you like to share?' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Hide traces' })).toHaveAttribute('aria-expanded', 'true');

    const first = screen.getByRole('checkbox', { name: 'Include trace: Trace s1' });
    const last = screen.getByRole('checkbox', { name: 'Include trace: Trace s12' });
    expect(first).toBeChecked();
    expect(last).toBeChecked();
    expect(screen.getByTestId('location-search')).toHaveTextContent(/^$/);

    fireEvent.click(first);
    await waitFor(() => expect(first).not.toBeChecked());
    await waitFor(() => expect(screen.getByTestId('location-search')).toHaveTextContent('exclude_ids=s1'));
    expect(last).toBeChecked();

    fireEvent.click(first);
    await waitFor(() => expect(first).toBeChecked());
    await waitFor(() => expect(screen.getByTestId('location-search')).toHaveTextContent(/^$/));
  });

  it('bounds large-history rendering while retaining and confirming the full selection', async () => {
    mockInitialLoad(readyStats(125));

    render(
      <MemoryRouter initialEntries={['/share']}>
        <ToastProvider><Share /></ToastProvider>
      </MemoryRouter>,
    );

    expect(await screen.findByText('125 traces selected')).toBeInTheDocument();
    expect(screen.getAllByRole('checkbox', { name: /Include trace:/ })).toHaveLength(50);
    expect(screen.getByText(/Showing 50 of 125 matching traces/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /Show 50 more/ }));
    expect(screen.getAllByRole('checkbox', { name: /Include trace:/ })).toHaveLength(100);

    fireEvent.click(screen.getByRole('button', { name: 'Redact & review' }));
    expect(screen.getByRole('dialog', { name: 'Review large bundle?' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Redact 125 traces' })).toBeInTheDocument();
  });

  it('sanitizes duplicate and ineligible ids from deep links after ready state loads', async () => {
    mockInitialLoad(readyStats(3));

    render(
      <MemoryRouter initialEntries={['/share?ids=s2,s2,blocked']}>
        <ToastProvider><Share /></ToastProvider>
        <LocationProbe />
      </MemoryRouter>,
    );

    expect(await screen.findByText('1 trace selected')).toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: 'Include trace: Trace s2' })).toBeChecked();
    await waitFor(() => expect(screen.getByTestId('location-search')).toHaveTextContent('ids=s2'));
  });

  it('gates a large Redact deep link before any redaction or AI review starts', async () => {
    mockInitialLoad(readyStats(125));
    const redactionSpy = vi.spyOn(api.sessions, 'redactionReport');

    render(
      <MemoryRouter initialEntries={['/share?step=redact&ai_pii=1']}>
        <ToastProvider><Share /></ToastProvider>
      </MemoryRouter>,
    );

    expect(await screen.findByText('125 traces selected')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'What would you like to share?' })).toBeInTheDocument();
    expect(redactionSpy).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Redact & review' }));
    expect(screen.getByRole('dialog', { name: 'Review large bundle?' })).toBeInTheDocument();
    expect(redactionSpy).not.toHaveBeenCalled();
  });

  it.each(['redact', 'review', 'package'] as const)(
    'rejects an unpackaged %s deep link without a locked exact selection',
    async (step) => {
      mockInitialLoad(readyStats(3));
      const redactionSpy = vi.spyOn(api.sessions, 'redactionReport');
      const packageSpy = vi.spyOn(api.shares, 'create');

      render(
        <MemoryRouter initialEntries={[`/share?step=${step}&ai_pii=1`]}>
          <ToastProvider><Share /></ToastProvider>
          <LocationProbe />
        </MemoryRouter>,
      );

      expect(await screen.findByRole('heading', { name: 'What would you like to share?' })).toBeInTheDocument();
      expect(redactionSpy).not.toHaveBeenCalled();
      expect(packageSpy).not.toHaveBeenCalled();
      await waitFor(() => {
        const params = new URLSearchParams(screen.getByTestId('location-search').textContent || '');
        expect(params.has('step')).toBe(false);
        expect(params.has('selection')).toBe(false);
      });
    },
  );

  it('reloads a default-all queue from its locked IDs without adding a newly eligible trace', async () => {
    mockInitialLoad(readyStats(2));
    const redactionSpy = vi.spyOn(api.sessions, 'redactionReport').mockImplementation(async (id) => ({
      session_id: id,
      redaction_count: 0,
      redaction_log: [],
      ai_pii_findings: [],
      ai_coverage: 'full',
      redacted_session: { messages: [] },
    }) as unknown as Awaited<ReturnType<typeof api.sessions.redactionReport>>);

    const firstView = render(
      <MemoryRouter initialEntries={['/share?ai_pii=1']}>
        <ToastProvider><Share /></ToastProvider>
        <LocationProbe />
      </MemoryRouter>,
    );

    expect(await screen.findByText('2 traces selected')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Redact & review' }));

    await waitFor(() => expect(redactionSpy).toHaveBeenCalledTimes(2));
    const lockedSearch = screen.getByTestId('location-search').textContent || '';
    const lockedParams = new URLSearchParams(lockedSearch);
    expect(lockedParams.get('step')).toBe('redact');
    expect(lockedParams.get('ids')).toBe('s1,s2');
    expect(lockedParams.get('selection')).toBe('locked');

    firstView.unmount();
    vi.mocked(api.shareReady).mockResolvedValue(
      readyStats(3) as Awaited<ReturnType<typeof api.shareReady>>,
    );
    redactionSpy.mockClear();

    render(
      <MemoryRouter initialEntries={[`/share${lockedSearch}`]}>
        <ToastProvider><Share /></ToastProvider>
      </MemoryRouter>,
    );

    expect(await screen.findByRole('heading', { name: 'Redacting your traces' })).toBeInTheDocument();
    await waitFor(() => expect(redactionSpy).toHaveBeenCalledTimes(2));
    expect(redactionSpy.mock.calls.map(([id]) => id)).toEqual(['s1', 's2']);
    expect(redactionSpy).not.toHaveBeenCalledWith('s3', expect.anything());
    expect(redactionSpy.mock.calls.every(([, options]) => options?.aiPii === true)).toBe(true);
  });

  it('resumes an existing packaged large share without restarting queue work', async () => {
    mockInitialLoad(readyStats(125));
    vi.mocked(api.shareDestination).mockResolvedValue({
      configured: true,
      daemon_upload_supported: true,
      submissions_open: true,
      preferred_upload_flow: 'daemon',
      cli_ingest_supported: false,
      share_page_url: null,
    });
    const redactionSpy = vi.spyOn(api.sessions, 'redactionReport');

    render(
      <MemoryRouter initialEntries={['/share?step=submit&share=existing']}>
        <ToastProvider><Share /></ToastProvider>
      </MemoryRouter>,
    );

    expect(await screen.findByRole('heading', { name: 'Submit to ClawJournal Research' })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'What would you like to share?' })).not.toBeInTheDocument();
    expect(redactionSpy).not.toHaveBeenCalled();
  });
});
