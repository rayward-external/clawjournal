import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from '../api.ts';
import { ToastProvider } from '../components/Toast.tsx';
import type { Session } from '../types.ts';
import { Inbox } from './Inbox.tsx';

function session(id: string): Session {
  return {
    session_id: id,
    project: 'project-a',
    source: 'codex',
    model: 'gpt-test',
    model_effort: null,
    start_time: '2026-07-15T12:00:00Z',
    end_time: null,
    duration_seconds: 60,
    git_branch: null,
    user_messages: 1,
    assistant_messages: 1,
    tool_uses: 0,
    input_tokens: 100,
    output_tokens: 50,
    display_title: `Session ${id}`,
    outcome_label: 'resolved',
    value_labels: [],
    risk_level: [],
    sensitivity_score: 0,
    task_type: 'testing',
    files_touched: [],
    commands_run: [],
    review_status: 'new',
    selection_reason: null,
    reviewer_notes: null,
    reviewed_at: null,
    ai_quality_score: 4,
    ai_failure_value_score: 3,
    ai_recovery_labels: [],
    ai_failure_attribution: null,
    ai_failure_modes: [],
    ai_learning_summary: null,
    ai_score_reason: null,
    ai_summary: null,
    ai_effort_estimate: null,
    blob_path: null,
    raw_source_path: null,
    client_origin: null,
    runtime_channel: null,
    outer_session_id: null,
    indexed_at: '2026-07-15T12:01:00Z',
    updated_at: null,
    share_id: null,
    estimated_cost_usd: null,
    parent_session_id: null,
    subagent_session_ids: null,
    user_interrupts: null,
    hold_state: 'auto_redacted',
    embargo_until: null,
    findings_revision: null,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function stats(total: number) {
  return {
    total,
    by_status: { new: total },
    by_source: { codex: total },
    by_project: { 'project-a': total },
    by_task_type: { testing: total },
  };
}

function renderInbox(sessions: Session[]) {
  vi.spyOn(api.sessions, 'list').mockResolvedValue(sessions);
  const statsSpy = vi.spyOn(api, 'stats').mockResolvedValue(stats(sessions.length));
  localStorage.setItem('cj.gettingStartedGuideV2Dismissed', '1');
  render(
    <MemoryRouter>
      <ToastProvider><Inbox /></ToastProvider>
    </MemoryRouter>,
  );
  return { statsSpy };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('Inbox selection defaults', () => {
  it('selects every loaded session and preserves manual deselection and Clear', async () => {
    const sessions = [session('one'), session('two')];
    vi.spyOn(api.sessions, 'list').mockResolvedValue(sessions);
    vi.spyOn(api, 'stats').mockResolvedValue({
      total: 2,
      by_status: { new: 2 },
      by_source: { codex: 2 },
      by_project: { 'project-a': 2 },
      by_task_type: { testing: 2 },
    });
    localStorage.setItem('cj.gettingStartedGuideV2Dismissed', '1');

    render(
      <MemoryRouter>
        <ToastProvider><Inbox /></ToastProvider>
      </MemoryRouter>,
    );

    const first = await screen.findByRole('checkbox', { name: 'Select session: Session one' });
    const second = screen.getByRole('checkbox', { name: 'Select session: Session two' });
    expect(first).toBeChecked();
    expect(second).toBeChecked();
    expect(screen.getByText('2 selected')).toBeInTheDocument();

    fireEvent.click(first);
    expect(first).not.toBeChecked();
    expect(second).toBeChecked();
    expect(screen.getByText('1 selected')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Select all' }));
    await waitFor(() => expect(first).toBeChecked());
    expect(second).toBeChecked();

    fireEvent.click(screen.getByRole('button', { name: 'Clear' }));
    expect(first).not.toBeChecked();
    expect(second).not.toBeChecked();
    expect(screen.queryByText(/selected$/)).not.toBeInTheDocument();
  });

  it('labels logical conversations and warns about an incomplete projection', async () => {
    renderInbox([{
      ...session('logical'),
      logical_session_id: 'logical',
      checkpoint_count: 3,
      logical_incomplete: true,
    }]);

    expect(await screen.findByText('3 upload checkpoints')).toBeInTheDocument();
    expect(screen.getByText('Incomplete conversation')).toBeInTheDocument();
  });
});

describe('Inbox bulk status updates', () => {
  it('shows immediate progress, closes the dialog, and prevents duplicate approval requests', async () => {
    const pending = deferred<{ updated_ids: string[]; missing_ids: string[] }>();
    const bulkStatus = vi.spyOn(api.sessions, 'bulkStatus').mockReturnValue(pending.promise);
    const { statsSpy } = renderInbox([session('one'), session('two')]);

    await screen.findByRole('checkbox', { name: 'Select session: Session one' });
    await waitFor(() => expect(statsSpy).toHaveBeenCalledTimes(1));
    const statsRefresh = deferred<ReturnType<typeof stats>>();
    statsSpy.mockReturnValueOnce(statsRefresh.promise);
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
    const confirmButton = within(screen.getByRole('dialog')).getByRole('button', { name: 'Approve' });
    fireEvent.click(confirmButton);
    // Exercise the synchronous guard with the old DOM reference: React may not
    // have committed the modal close between two browser click events.
    fireEvent.click(confirmButton);

    expect(bulkStatus).toHaveBeenCalledTimes(1);
    expect(bulkStatus).toHaveBeenCalledWith(['one', 'two'], 'approved');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('Approving 2 sessions…');
    expect(screen.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: 'Select session: Session one' })).toBeDisabled();
    fireEvent.click(screen.getAllByTitle('Filter by Testing')[0]);
    expect(api.sessions.list).toHaveBeenCalledTimes(1);

    pending.resolve({ updated_ids: ['one', 'two'], missing_ids: [] });

    await waitFor(() => {
      expect(screen.queryByRole('checkbox', { name: 'Select session: Session one' })).not.toBeInTheDocument();
    });
    expect(screen.getByText('2 sessions approved')).toBeInTheDocument();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
    expect(statsSpy).toHaveBeenCalledTimes(2);
    statsRefresh.resolve(stats(0));
  });

  it('keeps a missing session selected while removing the sessions the server skipped', async () => {
    const pending = deferred<{ updated_ids: string[]; missing_ids: string[] }>();
    vi.spyOn(api.sessions, 'bulkStatus').mockReturnValue(pending.promise);
    renderInbox([session('one'), session('two')]);

    await screen.findByRole('checkbox', { name: 'Select session: Session one' });
    fireEvent.click(screen.getByRole('button', { name: 'Skip' }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Skip' }));

    expect(screen.getByRole('status')).toHaveTextContent('Skipping 2 sessions…');
    pending.resolve({ updated_ids: ['one'], missing_ids: ['two'] });

    await waitFor(() => {
      expect(screen.queryByRole('checkbox', { name: 'Select session: Session one' })).not.toBeInTheDocument();
    });
    expect(screen.getByRole('checkbox', { name: 'Select session: Session two' })).toBeChecked();
    expect(screen.getByText('1 selected')).toBeInTheDocument();
    expect(screen.getByText('1 session skipped')).toBeInTheDocument();
    expect(screen.getByText('1 session could not be updated; visible items remain selected')).toBeInTheDocument();
  });

  it('keeps the submitted sessions selected when the request fails', async () => {
    const pending = deferred<{ updated_ids: string[]; missing_ids: string[] }>();
    vi.spyOn(api.sessions, 'bulkStatus').mockReturnValue(pending.promise);
    const { statsSpy } = renderInbox([session('one'), session('two')]);

    await screen.findByRole('checkbox', { name: 'Select session: Session one' });
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Approve' }));
    pending.reject(new Error('network unavailable'));

    await screen.findByText('Failed to update sessions: network unavailable');
    expect(screen.getByRole('checkbox', { name: 'Select session: Session one' })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: 'Select session: Session two' })).toBeChecked();
    expect(screen.getByText('2 selected')).toBeInTheDocument();
    expect(screen.queryByText(/sessions approved$/)).not.toBeInTheDocument();
    expect(statsSpy).toHaveBeenCalledTimes(2);
  });

  it('ignores a stale list response while a bulk update is pending', async () => {
    const staleList = deferred<Session[]>();
    vi.spyOn(api.sessions, 'list')
      .mockResolvedValueOnce([session('one'), session('two')])
      .mockReturnValueOnce(staleList.promise);
    vi.spyOn(api, 'stats').mockResolvedValue(stats(2));
    const pending = deferred<{ updated_ids: string[]; missing_ids: string[] }>();
    vi.spyOn(api.sessions, 'bulkStatus').mockReturnValue(pending.promise);
    localStorage.setItem('cj.gettingStartedGuideV2Dismissed', '1');
    render(
      <MemoryRouter>
        <ToastProvider><Inbox /></ToastProvider>
      </MemoryRouter>,
    );

    await screen.findByRole('checkbox', { name: 'Select session: Session one' });
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
    fireEvent.change(screen.getByRole('combobox', { name: 'Sort sessions' }), {
      target: { value: 'start_time:desc' },
    });
    await waitFor(() => expect(api.sessions.list).toHaveBeenCalledTimes(2));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Approve' }));

    expect(screen.getByRole('combobox', { name: 'Sort sessions' })).toBeDisabled();
    expect(screen.getByRole('combobox', { name: 'Sessions per page' })).toBeDisabled();
    pending.resolve({ updated_ids: ['one', 'two'], missing_ids: [] });
    await waitFor(() => {
      expect(screen.queryByRole('checkbox', { name: 'Select session: Session one' })).not.toBeInTheDocument();
    });

    await act(async () => {
      staleList.resolve([session('one'), session('two')]);
      await staleList.promise;
    });
    expect(screen.queryByRole('checkbox', { name: 'Select session: Session one' })).not.toBeInTheDocument();
  });

  it('refills from the first page without reselecting a deliberately excluded row', async () => {
    const initialSessions = Array.from({ length: 11 }, (_, index) => session(`s${index + 1}`));
    const list = vi.spyOn(api.sessions, 'list')
      .mockResolvedValueOnce(initialSessions)
      .mockResolvedValueOnce([session('s10'), session('s11')]);
    vi.spyOn(api, 'stats').mockResolvedValue(stats(initialSessions.length));
    vi.spyOn(api.sessions, 'bulkStatus').mockImplementation(async ids => ({
      updated_ids: ids,
      missing_ids: [],
    }));
    localStorage.setItem('cj.gettingStartedGuideV2Dismissed', '1');
    render(
      <MemoryRouter>
        <ToastProvider><Inbox /></ToastProvider>
      </MemoryRouter>,
    );

    await screen.findByRole('checkbox', { name: 'Select session: Session s1' });
    fireEvent.click(screen.getByRole('checkbox', { name: 'Select session: Session s10' }));
    expect(screen.getByText('9 selected')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Approve' }));

    const nextSession = await screen.findByRole('checkbox', {
      name: 'Select session: Session s11',
    });
    expect(list).toHaveBeenCalledTimes(2);
    expect(screen.getByRole('checkbox', { name: 'Select session: Session s10' })).not.toBeChecked();
    expect(nextSession).toBeChecked();
    expect(screen.getByText('1 selected')).toBeInTheDocument();
    expect(screen.queryByText('Every session has been reviewed.')).not.toBeInTheDocument();
  });

  it('keeps new rows unselected after Clear opts out of automatic selection', async () => {
    const initialSessions = Array.from({ length: 11 }, (_, index) => session(`s${index + 1}`));
    vi.spyOn(api.sessions, 'list')
      .mockResolvedValueOnce(initialSessions)
      .mockResolvedValueOnce(initialSessions.slice(1));
    vi.spyOn(api, 'stats').mockResolvedValue(stats(initialSessions.length));
    vi.spyOn(api.sessions, 'bulkStatus').mockImplementation(async ids => ({
      updated_ids: ids,
      missing_ids: [],
    }));
    localStorage.setItem('cj.gettingStartedGuideV2Dismissed', '1');
    render(
      <MemoryRouter>
        <ToastProvider><Inbox /></ToastProvider>
      </MemoryRouter>,
    );

    const firstSession = await screen.findByRole('checkbox', {
      name: 'Select session: Session s1',
    });
    fireEvent.click(screen.getByRole('button', { name: 'Clear' }));
    fireEvent.click(firstSession);
    expect(screen.getByText('1 selected')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Approve' }));

    const nextSession = await screen.findByRole('checkbox', {
      name: 'Select session: Session s11',
    });
    expect(screen.getByRole('checkbox', { name: 'Select session: Session s2' })).not.toBeChecked();
    expect(nextSession).not.toBeChecked();
    expect(screen.queryByText(/selected$/)).not.toBeInTheDocument();
  });

  it('splits over-limit selections and preserves a later failed chunk for retry', async () => {
    const allSessions = Array.from({ length: 101 }, (_, index) => session(`s${index + 1}`));
    vi.spyOn(api.sessions, 'list').mockImplementation(async (params = {}) => {
      const start = params.offset ?? 0;
      return allSessions.slice(start, start + (params.limit ?? 50));
    });
    vi.spyOn(api, 'stats').mockResolvedValue(stats(allSessions.length));
    const bulkStatus = vi.spyOn(api.sessions, 'bulkStatus').mockImplementation(async ids => {
      if (ids.length === 1) throw new Error('second chunk failed');
      return { updated_ids: ids, missing_ids: [] };
    });
    localStorage.setItem('cj.gettingStartedGuideV2Dismissed', '1');
    render(
      <MemoryRouter>
        <ToastProvider><Inbox /></ToastProvider>
      </MemoryRouter>,
    );

    await screen.findByRole('checkbox', { name: 'Select session: Session s1' });
    fireEvent.change(screen.getByRole('combobox', { name: 'Sessions per page' }), {
      target: { value: '100' },
    });
    await screen.findByText('100 selected');
    fireEvent.click(screen.getByRole('button', { name: 'Load more' }));
    await screen.findByText('101 selected');

    fireEvent.click(screen.getByRole('button', { name: 'Approve' }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Approve' }));

    await waitFor(() => expect(bulkStatus).toHaveBeenCalledTimes(2));
    expect(bulkStatus.mock.calls[0][0]).toHaveLength(100);
    expect(bulkStatus.mock.calls[1][0]).toHaveLength(1);
    expect(bulkStatus.mock.calls[0][1]).toBe('approved');
    expect(bulkStatus.mock.calls[1][1]).toBe('approved');
    await waitFor(() => {
      expect(screen.queryByRole('checkbox', { name: 'Select session: Session s1' })).not.toBeInTheDocument();
    });
    expect(screen.getByRole('checkbox', { name: 'Select session: Session s101' })).toBeChecked();
    expect(screen.getByText('1 selected')).toBeInTheDocument();
    expect(screen.getByText('100 sessions approved')).toBeInTheDocument();
    expect(screen.getByText('1 session could not be updated; visible items remain selected')).toBeInTheDocument();
  }, 15_000);
});
