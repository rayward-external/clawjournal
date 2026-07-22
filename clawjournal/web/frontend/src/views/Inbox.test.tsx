import { fireEvent, render, screen, waitFor } from '@testing-library/react';
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
});
