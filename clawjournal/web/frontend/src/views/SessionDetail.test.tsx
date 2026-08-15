import { render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from '../api.ts';
import { ToastProvider } from '../components/Toast.tsx';
import type { SessionDetail as SessionDetailType } from '../types.ts';
import SessionDetail from './SessionDetail.tsx';

function logicalDetail(): SessionDetailType {
  return {
    session_id: 'logical-session',
    logical_session_id: 'logical-session',
    logical_revision: 'logical-r1',
    checkpoint_count: 2,
    project: 'project-a',
    source: 'claude',
    model: 'claude-test',
    model_effort: null,
    start_time: '2026-08-15T10:00:00Z',
    end_time: '2026-08-15T10:01:00Z',
    duration_seconds: 60,
    git_branch: 'main',
    user_messages: 1,
    assistant_messages: 1,
    tool_uses: 0,
    input_tokens: 100,
    output_tokens: 50,
    display_title: 'Logical conversation',
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
    ai_learning_summary: 'Checkpoint-only learning summary',
    ai_score_reason: 'Checkpoint-only score reason',
    ai_summary: null,
    ai_effort_estimate: null,
    ai_scoring_detail: null,
    blob_path: null,
    raw_source_path: null,
    client_origin: null,
    runtime_channel: null,
    outer_session_id: null,
    indexed_at: '2026-08-15T10:01:00Z',
    updated_at: null,
    share_id: null,
    estimated_cost_usd: null,
    parent_session_id: null,
    subagent_session_ids: null,
    user_interrupts: null,
    hold_state: 'auto_redacted',
    embargo_until: null,
    findings_revision: null,
    messages: [
      { role: 'user', content: 'Question' },
      { role: 'assistant', content: 'Answer' },
    ],
  };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('logical conversation scoring', () => {
  it('does not present or mutate a representative checkpoint score', async () => {
    vi.spyOn(api.sessions, 'get').mockResolvedValue(logicalDetail());
    const scoreSpy = vi.spyOn(api.sessions, 'score');
    const updateSpy = vi.spyOn(api.sessions, 'update');

    render(
      <MemoryRouter initialEntries={['/session/logical-session']}>
        <ToastProvider>
          <Routes>
            <Route path="/session/:id" element={<SessionDetail />} />
          </Routes>
        </ToastProvider>
      </MemoryRouter>,
    );

    expect(await screen.findByText(/no checkpoint score is presented/)).toBeInTheDocument();
    expect(screen.queryByText('Failure value 3/5')).not.toBeInTheDocument();
    expect(screen.queryByText('Productivity 4/5')).not.toBeInTheDocument();

    const scoreButton = screen.getByRole('button', { name: 'Conversation scoring unavailable' });
    expect(scoreButton).toBeDisabled();
    expect(screen.getByPlaceholderText('Evidence for 4-5 overrides')).toBeDisabled();
    expect(screen.getByRole('button', { name: '1' })).toBeDisabled();
    expect(scoreSpy).not.toHaveBeenCalled();
    expect(updateSpy).not.toHaveBeenCalled();
  });
});
