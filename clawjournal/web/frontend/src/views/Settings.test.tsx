import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from '../api.ts';
import type { WorkbenchConfig } from '../types.ts';
import { ToastProvider } from '../components/Toast.tsx';
import { Settings } from './Settings.tsx';

vi.mock('../components/AutoUploadControls.tsx', () => ({ AutoUploadPanel: () => null }));

const config: WorkbenchConfig = {
  source: null,
  projects_confirmed: false,
  ai_pii_review_enabled: false,
  scorer_backend: null,
  scorer_backend_confirmed_at: null,
  benchmark_tab_enabled: true,
  scoring_warmup_declined: true,
  source_choices: ['claude'],
  scorer_backend_choices: ['claude'],
  scorer_backend_detected: null,
};

function renderSettings() {
  return render(<ToastProvider><Settings /></ToastProvider>);
}

describe('project confirmation', () => {
  afterEach(() => vi.restoreAllMocks());

  it('shows the exact included and excluded projects before one-step confirmation', async () => {
    vi.spyOn(api.config, 'get').mockResolvedValue(config);
    const update = vi.spyOn(api.config, 'update').mockResolvedValue({ ...config, projects_confirmed: true });
    vi.spyOn(api, 'projects').mockResolvedValue([
      { project: 'customer-app', source: 'claude', session_count: 4, total_tokens: 100, excluded: false },
      { project: 'private-notes', source: 'codex', session_count: 2, total_tokens: 50, excluded: true },
    ]);

    renderSettings();

    expect(await screen.findByText('customer-app')).toBeInTheDocument();
    expect(screen.getByText('private-notes')).toBeInTheDocument();
    expect(screen.getByText('Included')).toBeInTheDocument();
    expect(screen.getByText('Excluded')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Confirm reviewed projects' }));
    await waitFor(() => expect(update).toHaveBeenCalledWith({ confirm_projects: true }));
    expect(await screen.findByText(/Projects confirmed/)).toBeInTheDocument();
  });

  it('does not allow blind confirmation when the project list cannot load', async () => {
    vi.spyOn(api.config, 'get').mockResolvedValue(config);
    vi.spyOn(api, 'projects').mockRejectedValue(new Error('offline'));

    renderSettings();

    expect(await screen.findByText(/Could not load the project list/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Confirm reviewed projects' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument();
  });
});
