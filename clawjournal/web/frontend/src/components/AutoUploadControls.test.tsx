import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api, ApiError } from '../api.ts';
import type { AutoUploadStatus, WorkbenchConfig } from '../types.ts';
import { AutoUploadOffer, AutoUploadPanel } from './AutoUploadControls.tsx';
import { ToastProvider } from './Toast.tsx';

function status(overrides: Partial<AutoUploadStatus> = {}): AutoUploadStatus {
  return {
    mode: 'off',
    health: 'ready',
    run_now_allowed: false,
    overlay: null,
    pending_submission_state: null,
    ui_visible: true,
    offer_available: false,
    enrollment_grant_available: false,
    scope: { sources: [], projects: [], entries: [] },
    cap: 5,
    cadence_days: 1,
    ai: { enabled: false, backend: null },
    authorization: { version: null, text: null },
    retention: { version: null, text: null },
    enrolled_at: null,
    next_due_at: null,
    next_retry_at: null,
    hooks: [],
    eligibility: { selected_count: 0, eligible_count: 0, exclusion_counts: {} },
    last_result: null,
    ...overrides,
  };
}

function workbenchConfig(overrides: Partial<WorkbenchConfig> = {}): WorkbenchConfig {
  return {
    source: null,
    projects_confirmed: false,
    ai_pii_review_enabled: false,
    scorer_backend: null,
    scorer_backend_confirmed_at: null,
    benchmark_tab_enabled: true,
    scoring_warmup_declined: false,
    source_choices: ['all', 'claude', 'codex'],
    scorer_backend_choices: [],
    scorer_backend_detected: null,
    ...overrides,
  };
}

function renderControl(ui: React.ReactNode) {
  return render(
    <MemoryRouter>
      <ToastProvider>{ui}</ToastProvider>
    </MemoryRouter>,
  );
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function authorizationRequired() {
  return new ApiError(409, 'Authorization required', {
    code: 'authorization_required',
    authorization_profile_hash: 'profile-hash-v2',
    authorization: {
      version: 'recurring-v2',
      text: 'I authorize capped recurring uploads of eligible future traces.',
    },
    retention: {
      version: 'retention-v3',
      text: 'Hosted retention terms for recurring uploads.',
    },
    ownership_certification: {
      version: 'ownership-v1',
      text: 'I certify every automatically uploaded bundle is my own lawful content.',
    },
    scope: {
      sources: ['claude'],
      projects: ['project-a'],
      entries: [['claude', 'project-a']],
    },
    ai: { enabled: true, backend: 'codex' },
    cap: 5,
    cadence_days: 1,
    maximum_bundle_size: 5_000_000,
    destination_origin: 'https://share.example.test',
  });
}

function scopeRequired() {
  return new ApiError(400, 'Scope confirmation required', {
    code: 'source_confirmation_missing',
    message: 'Confirm a non-empty source and project scope before enabling.',
    scope_blockers: [
      'source_confirmation_missing',
      'project_confirmation_missing',
      'source_scope_empty',
      'project_scope_empty',
    ],
  });
}

function unsupportedScopeRequired() {
  return new ApiError(400, 'Unsupported source', {
    code: 'unsupported_source',
    message: 'Confirm a non-empty source and project scope before enabling.',
    scope_blockers: ['unsupported_source'],
    unsupported_sources: ['gemini'],
  });
}

async function flushPromises() {
  await Promise.resolve();
  await Promise.resolve();
}

afterEach(() => {
  vi.useRealTimers();
});

describe('AutoUploadOffer', () => {
  it('stays hidden when the internal rollout flag is off', async () => {
    vi.spyOn(api.autoUpload, 'status').mockResolvedValueOnce(status({
      ui_visible: false,
      offer_available: true,
    }));

    renderControl(<AutoUploadOffer manualReceiptId="receipt-hidden" />);
    await flushPromises();

    expect(screen.queryByText('Share future traces automatically?')).not.toBeInTheDocument();
  });

  it('requires a manual receipt and server capability, then persists dismissal', async () => {
    const statusSpy = vi.spyOn(api.autoUpload, 'status');
    vi.spyOn(api.autoUpload, 'enable').mockRejectedValue(authorizationRequired());

    const withoutReceipt = renderControl(<AutoUploadOffer manualReceiptId={null} />);
    expect(statusSpy).not.toHaveBeenCalled();
    withoutReceipt.unmount();

    statusSpy.mockResolvedValueOnce(status({ offer_available: false }));
    const withoutCapability = renderControl(<AutoUploadOffer manualReceiptId="receipt-1" />);
    await waitFor(() => expect(statusSpy).toHaveBeenCalledTimes(1));
    expect(screen.queryByText('Share future traces automatically?')).not.toBeInTheDocument();
    withoutCapability.unmount();

    statusSpy.mockResolvedValueOnce(status({ offer_available: true }));
    const dismissible = renderControl(<AutoUploadOffer manualReceiptId="receipt-2" />);
    expect(await screen.findByText('Share future traces automatically?')).toBeInTheDocument();
    expect(await screen.findByText(/exact source\/project pairs/i)).toBeInTheDocument();
    expect(screen.getByText(/you will verify your email before enabling/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Review and enable' })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Not now' }));

    expect(screen.queryByText('Share future traces automatically?')).not.toBeInTheDocument();
    expect(window.localStorage.getItem('clawjournal.autoUploadOfferDismissed.v1')).toBe('receipt-2');
    dismissible.unmount();

    // The same receipt stays dismissed without another status fetch...
    const callsAfterDismiss = statusSpy.mock.calls.length;
    const sameReceipt = renderControl(<AutoUploadOffer manualReceiptId="receipt-2" />);
    await flushPromises();
    expect(statusSpy).toHaveBeenCalledTimes(callsAfterDismiss);
    expect(screen.queryByText('Share future traces automatically?')).not.toBeInTheDocument();
    sameReceipt.unmount();

    // ...but a later manual share (new receipt) is offered again.
    statusSpy.mockResolvedValueOnce(status({ offer_available: true }));
    renderControl(<AutoUploadOffer manualReceiptId="receipt-3" />);
    expect(await screen.findByText('Share future traces automatically?')).toBeInTheDocument();
  });

  it('keeps missing scope setup inside the enable flow and continues to authorization', async () => {
    vi.spyOn(api.autoUpload, 'status').mockResolvedValueOnce(status({
      offer_available: true,
    }));
    const enableSpy = vi.spyOn(api.autoUpload, 'enable')
      .mockRejectedValueOnce(scopeRequired())
      .mockRejectedValueOnce(authorizationRequired());
    vi.spyOn(api.config, 'get').mockResolvedValueOnce(workbenchConfig());
    const updateSpy = vi.spyOn(api.config, 'update').mockResolvedValueOnce(workbenchConfig({
      source: 'both',
      projects_confirmed: true,
      source_choices: ['all', 'both', 'claude', 'codex'],
    }));
    vi.spyOn(api, 'projects').mockResolvedValueOnce([
      {
        source: 'claude',
        project: 'research-notes',
        session_count: 2,
        total_tokens: 1_200,
      },
      {
        source: 'codex',
        project: 'analysis-pipeline',
        session_count: 1,
        total_tokens: 800,
      },
    ]);

    renderControl(<AutoUploadOffer manualReceiptId="receipt-needs-scope" />);

    expect(await screen.findByText('Choose what automatic uploads may include')).toBeInTheDocument();
    expect(screen.queryByText('Confirm a non-empty source and project scope before enabling.')).not.toBeInTheDocument();
    // Inline presentation: still dismissible, and the run-trigger select stays
    // hidden while its labels would duplicate the source-scope options below.
    expect(screen.getByRole('heading', { name: 'Share future traces automatically?' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Not now' })).toBeInTheDocument();
    expect(screen.queryByLabelText('Run on agent sessions')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Export source scope')).toHaveValue('');

    fireEvent.change(screen.getByLabelText('Export source scope'), {
      target: { value: 'both' },
    });
    expect(screen.getByLabelText('Claude Code · research-notes · 2 sessions')).toBeInTheDocument();
    expect(screen.getByLabelText('Codex · analysis-pipeline · 1 session')).toBeInTheDocument();

    const projectConfirmation = screen.getByLabelText('Confirm all eligible projects for automatic upload');
    expect(projectConfirmation).not.toBeChecked();
    expect(screen.getByText('Confirm all eligible projects')).toBeInTheDocument();
    expect(screen.getByText('I reviewed all 2 projects listed above. Only these projects may enter this automatic-upload scope.')).toBeInTheDocument();
    fireEvent.click(projectConfirmation);
    fireEvent.click(screen.getByRole('button', { name: 'Save scope and continue' }));

    await waitFor(() => expect(updateSpy).toHaveBeenCalledWith({
      source: 'both',
      confirm_projects: true,
    }));
    await waitFor(() => expect(enableSpy).toHaveBeenCalledTimes(2));
    expect(enableSpy).toHaveBeenNthCalledWith(2, {
      agent: 'all',
      challenge_only: true,
    });
    expect(await screen.findByText('I authorize capped recurring uploads of eligible future traces.')).toBeInTheDocument();
    expect(screen.getByText('claude → project-a')).toBeInTheDocument();
  });

  it('retries with the hook matching a single-source scope after saving', async () => {
    vi.spyOn(api.autoUpload, 'status').mockResolvedValueOnce(status({
      offer_available: true,
    }));
    const enableSpy = vi.spyOn(api.autoUpload, 'enable')
      .mockRejectedValueOnce(scopeRequired())
      .mockRejectedValueOnce(authorizationRequired());
    vi.spyOn(api.config, 'get').mockResolvedValueOnce(workbenchConfig());
    vi.spyOn(api.config, 'update').mockResolvedValueOnce(workbenchConfig({
      source: 'claude',
      projects_confirmed: true,
    }));
    vi.spyOn(api, 'projects').mockResolvedValueOnce([
      {
        source: 'claude',
        project: 'research-notes',
        session_count: 2,
        total_tokens: 1_200,
      },
    ]);

    renderControl(<AutoUploadOffer manualReceiptId="receipt-claude-only-scope" />);
    fireEvent.change(await screen.findByLabelText('Export source scope'), {
      target: { value: 'claude' },
    });
    fireEvent.click(screen.getByLabelText('Confirm all eligible projects for automatic upload'));
    fireEvent.click(screen.getByRole('button', { name: 'Save scope and continue' }));

    // A claude-only scope must not schedule a codex SessionStart hook.
    await waitFor(() => expect(enableSpy).toHaveBeenCalledTimes(2));
    expect(enableSpy).toHaveBeenNthCalledWith(2, {
      agent: 'claude',
      challenge_only: true,
    });
  });

  it('keeps scope setup open when saving the scope fails', async () => {
    vi.spyOn(api.autoUpload, 'status').mockResolvedValueOnce(status({
      offer_available: true,
    }));
    const enableSpy = vi.spyOn(api.autoUpload, 'enable')
      .mockRejectedValueOnce(scopeRequired());
    vi.spyOn(api.config, 'get').mockResolvedValueOnce(workbenchConfig());
    vi.spyOn(api.config, 'update').mockRejectedValueOnce(
      new ApiError(500, 'Config persistence could not be confirmed.'),
    );
    vi.spyOn(api, 'projects').mockResolvedValueOnce([
      {
        source: 'claude',
        project: 'research-notes',
        session_count: 2,
        total_tokens: 1_200,
      },
    ]);

    renderControl(<AutoUploadOffer manualReceiptId="receipt-scope-save-fails" />);
    fireEvent.change(await screen.findByLabelText('Export source scope'), {
      target: { value: 'claude' },
    });
    fireEvent.click(screen.getByLabelText('Confirm all eligible projects for automatic upload'));
    fireEvent.click(screen.getByRole('button', { name: 'Save scope and continue' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Config persistence could not be confirmed.',
    );
    expect(screen.getByText('Choose what automatic uploads may include')).toBeInTheDocument();
    expect(screen.getByLabelText('Confirm all eligible projects for automatic upload')).toBeChecked();
    expect(screen.getByRole('button', { name: 'Save scope and continue' })).toBeEnabled();
    expect(enableSpy).toHaveBeenCalledTimes(1);
  });

  it('guides unsupported export scopes to the supported recurring sources', async () => {
    vi.spyOn(api.autoUpload, 'status').mockResolvedValueOnce(status({
      offer_available: true,
    }));
    const enableSpy = vi.spyOn(api.autoUpload, 'enable')
      .mockRejectedValueOnce(unsupportedScopeRequired())
      .mockRejectedValueOnce(authorizationRequired());
    vi.spyOn(api.config, 'get').mockResolvedValueOnce(workbenchConfig({
      source: 'all',
      projects_confirmed: true,
      source_choices: ['aider', 'all', 'claude', 'codex', 'gemini'],
    }));
    const updateSpy = vi.spyOn(api.config, 'update').mockResolvedValueOnce(workbenchConfig({
      source: 'both',
      projects_confirmed: true,
      source_choices: ['all', 'both', 'claude', 'codex'],
    }));
    vi.spyOn(api, 'projects').mockResolvedValueOnce([
      {
        source: 'claude',
        project: 'research-notes',
        session_count: 2,
        total_tokens: 1_200,
      },
      {
        source: 'gemini',
        project: 'unsupported-project',
        session_count: 1,
        total_tokens: 500,
      },
    ]);

    renderControl(<AutoUploadOffer manualReceiptId="receipt-unsupported-scope" />);

    const source = await screen.findByLabelText('Export source scope');
    expect(source).toHaveValue('');
    expect(within(source).getByRole('option', { name: 'Claude Code and Codex' })).toBeInTheDocument();
    expect(within(source).getByRole('option', { name: 'Claude Code' })).toBeInTheDocument();
    expect(within(source).getByRole('option', { name: 'Codex' })).toBeInTheDocument();
    expect(within(source).queryByRole('option', { name: 'All agents' })).not.toBeInTheDocument();
    expect(within(source).queryByRole('option', { name: 'Gemini' })).not.toBeInTheDocument();

    fireEvent.change(source, { target: { value: 'both' } });
    expect(screen.getByLabelText('Claude Code · research-notes · 2 sessions')).toBeInTheDocument();
    expect(screen.queryByText('unsupported-project')).not.toBeInTheDocument();
    fireEvent.click(screen.getByLabelText('Confirm all eligible projects for automatic upload'));
    fireEvent.click(screen.getByRole('button', { name: 'Save scope and continue' }));

    await waitFor(() => expect(updateSpy).toHaveBeenCalledWith({
      source: 'both',
      confirm_projects: true,
    }));
    await waitFor(() => expect(enableSpy).toHaveBeenCalledTimes(2));
    expect(await screen.findByText('I authorize capped recurring uploads of eligible future traces.')).toBeInTheDocument();
  });

  it('omits excluded projects from the project confirmation step', async () => {
    vi.spyOn(api.autoUpload, 'status').mockResolvedValueOnce(status({
      offer_available: true,
    }));
    vi.spyOn(api.autoUpload, 'enable').mockRejectedValueOnce(scopeRequired());
    vi.spyOn(api.config, 'get').mockResolvedValueOnce(workbenchConfig());
    vi.spyOn(api, 'projects').mockResolvedValueOnce([
      {
        source: 'claude',
        project: 'eligible-project',
        session_count: 2,
        total_tokens: 1_200,
        excluded: false,
      },
      {
        source: 'claude',
        project: 'excluded-project',
        session_count: 1,
        total_tokens: 500,
        excluded: true,
      },
    ]);

    renderControl(<AutoUploadOffer manualReceiptId="receipt-with-exclusion" />);
    fireEvent.change(await screen.findByLabelText('Export source scope'), {
      target: { value: 'claude' },
    });

    expect(screen.getByLabelText('Claude Code · eligible-project · 2 sessions')).toBeInTheDocument();
    expect(screen.queryByText('excluded-project')).not.toBeInTheDocument();
  });

  it('requires fresh project confirmation when the source selection changes', async () => {
    vi.spyOn(api.autoUpload, 'status').mockResolvedValueOnce(status({
      offer_available: true,
    }));
    vi.spyOn(api.autoUpload, 'enable').mockRejectedValueOnce(scopeRequired());
    vi.spyOn(api.config, 'get').mockResolvedValueOnce(workbenchConfig({
      source: 'both',
      projects_confirmed: true,
      source_choices: ['all', 'both', 'claude', 'codex'],
    }));
    vi.spyOn(api, 'projects').mockResolvedValueOnce([
      {
        source: 'claude',
        project: 'claude-project',
        session_count: 1,
        total_tokens: 500,
      },
      {
        source: 'codex',
        project: 'codex-project',
        session_count: 1,
        total_tokens: 500,
      },
    ]);

    renderControl(<AutoUploadOffer manualReceiptId="receipt-change-source" />);

    const source = await screen.findByLabelText('Export source scope');
    const projectConfirmation = screen.getByLabelText('Confirm all eligible projects for automatic upload');
    expect(source).toHaveValue('both');
    // A stored projects_confirmed never pre-checks the box: the confirmation
    // attests to the list rendered here, not to an older one.
    expect(projectConfirmation).not.toBeChecked();
    expect(screen.getByRole('button', { name: 'Save scope and continue' })).toBeDisabled();
    expect(screen.getByLabelText('Claude Code · claude-project · 1 session')).toBeInTheDocument();
    expect(screen.getByLabelText('Codex · codex-project · 1 session')).toBeInTheDocument();

    fireEvent.click(projectConfirmation);
    expect(projectConfirmation).toBeChecked();
    fireEvent.change(source, { target: { value: 'codex' } });

    expect(projectConfirmation).not.toBeChecked();
    expect(screen.getByRole('button', { name: 'Save scope and continue' })).toBeDisabled();
    expect(screen.queryByLabelText('Claude Code · claude-project · 1 session')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Codex · codex-project · 1 session')).toBeInTheDocument();
  });

  it('explains an oversized scope instead of echoing the CLI-worded error', async () => {
    vi.spyOn(api.autoUpload, 'status').mockResolvedValueOnce(status({
      offer_available: true,
    }));
    vi.spyOn(api.autoUpload, 'enable').mockRejectedValueOnce(new ApiError(400, 'Scope too large', {
      code: 'scope_too_large',
      message: 'The exact source/project scope exceeds the hosted limit of 200 entries; '
        + 'exclude projects (config --exclude) or narrow the source scope first.',
      scope_blockers: ['scope_too_large'],
    }));

    renderControl(<AutoUploadOffer manualReceiptId="receipt-oversized-scope" />);

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'This scope has too many exact source/project pairs',
    );
    expect(screen.queryByText('Choose what automatic uploads may include')).not.toBeInTheDocument();
  });

  it('enables inline with the receipt grant and infers the only agent', async () => {
    const initial = status({
      offer_available: true,
      enrollment_grant_available: true,
      scope: {
        sources: ['codex'],
        projects: ['project-a'],
        entries: [['codex', 'project-a']],
      },
    });
    const enabled = status({
      mode: 'enabled',
      scope: {
        sources: ['codex'],
        projects: ['project-a'],
        entries: [['codex', 'project-a']],
      },
    });
    const grantChallenge = authorizationRequired();
    (grantChallenge.body.scope as Record<string, unknown>).sources = ['codex'];
    (grantChallenge.body.scope as Record<string, unknown>).entries = [
      ['codex', 'project-a'],
    ];
    vi.spyOn(api.autoUpload, 'status').mockResolvedValue(initial);
    const enableSpy = vi.spyOn(api.autoUpload, 'enable')
      .mockRejectedValueOnce(grantChallenge)
      .mockResolvedValueOnce(enabled);
    const uploadStatusSpy = vi.spyOn(api.share, 'uploadStatus').mockResolvedValue({
      verified_email: null,
      token_valid: false,
      expires_at: null,
      pending_email: null,
    });

    renderControl(<AutoUploadOffer manualReceiptId="receipt-grant" />);

    expect(await screen.findByText('Share future traces automatically?')).toBeInTheDocument();
    expect(screen.getByText(/without verifying your email again/i)).toBeInTheDocument();
    expect(await screen.findByText('Exact recurring scope · 1 source/project pair')).toBeInTheDocument();
    const boxes = screen.getAllByRole('checkbox');
    expect(boxes).toHaveLength(2);
    fireEvent.click(boxes[0]);
    fireEvent.click(boxes[1]);
    fireEvent.click(screen.getByRole('button', { name: 'Enable automatic upload' }));

    await waitFor(() => expect(enableSpy).toHaveBeenCalledTimes(2));
    expect(enableSpy.mock.calls[1][0].agent).toBe('codex');
    expect(uploadStatusSpy).not.toHaveBeenCalled();
  });
});

describe('AutoUploadPanel visibility', () => {
  it('renders nothing when the internal rollout flag is off', async () => {
    vi.spyOn(api.autoUpload, 'status').mockResolvedValueOnce(status({ ui_visible: false }));

    renderControl(<AutoUploadPanel />);
    await waitFor(() => expect(api.autoUpload.status).toHaveBeenCalledTimes(1));

    expect(screen.queryByRole('heading', { name: 'Automatic uploads' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Review and enable' })).not.toBeInTheDocument();
  });

  it('keeps the error and Retry path when the status fetch fails, so an enrolled user can reach the controls', async () => {
    vi.spyOn(api.autoUpload, 'status')
      .mockRejectedValueOnce(new ApiError(500, 'daemon unreachable'))
      .mockResolvedValueOnce(status({ mode: 'enabled', run_now_allowed: true }));

    renderControl(<AutoUploadPanel />);

    expect(await screen.findByRole('button', { name: 'Retry' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Automatic uploads' })).toBeInTheDocument();
    expect(screen.getByText('daemon unreachable')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));

    expect(await screen.findByRole('button', { name: 'Pause' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Turn off' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument();
  });
});

describe('AutoUploadPanel authorization', () => {
  it('reports an inline scope save so Settings can update without a reload', async () => {
    const savedConfig = workbenchConfig({
      source: 'both',
      projects_confirmed: true,
      source_choices: ['all', 'both', 'claude', 'codex'],
    });
    vi.spyOn(api.autoUpload, 'status').mockResolvedValueOnce(status({
      offer_available: true,
    }));
    vi.spyOn(api.autoUpload, 'enable')
      .mockRejectedValueOnce(scopeRequired())
      .mockRejectedValueOnce(authorizationRequired());
    vi.spyOn(api.config, 'get').mockResolvedValueOnce(workbenchConfig());
    vi.spyOn(api.config, 'update').mockResolvedValueOnce(savedConfig);
    vi.spyOn(api, 'projects').mockResolvedValueOnce([
      {
        source: 'claude',
        project: 'research-notes',
        session_count: 2,
        total_tokens: 1_200,
      },
    ]);
    const onConfigUpdated = vi.fn();

    renderControl(<AutoUploadPanel onConfigUpdated={onConfigUpdated} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Review and enable' }));
    fireEvent.change(await screen.findByLabelText('Export source scope'), {
      target: { value: 'both' },
    });
    fireEvent.click(screen.getByLabelText('Confirm all eligible projects for automatic upload'));
    fireEvent.click(screen.getByRole('button', { name: 'Save scope and continue' }));

    await waitFor(() => expect(onConfigUpdated).toHaveBeenCalledWith(savedConfig));
    expect(onConfigUpdated).toHaveBeenCalledTimes(1);
    expect(await screen.findByText('I authorize capped recurring uploads of eligible future traces.')).toBeInTheDocument();
  });

  it('shows the distinct recurring wording, retains the selected hook, and rejects a stale GET after enable', async () => {
    const initial = status({
      mode: 'enabled',
      run_now_allowed: true,
      scope: {
        sources: ['claude'],
        projects: ['project-a'],
        entries: [['claude', 'project-a']],
      },
      authorization: { version: 'recurring-v1', text: 'old terms' },
      hooks: [
        { agent: 'claude', selected: true, configured: true, installed: true, last_observed_at: null },
        { agent: 'codex', selected: false, configured: true, installed: true, last_observed_at: null },
      ],
    });
    const enabled = status({
      ...initial,
      authorization: { version: 'recurring-v2', text: 'new terms' },
    });
    const staleGet = deferred<AutoUploadStatus>();
    vi.spyOn(api.autoUpload, 'status')
      .mockResolvedValueOnce(initial)
      .mockReturnValueOnce(staleGet.promise);
    const enableSpy = vi.spyOn(api.autoUpload, 'enable')
      .mockRejectedValueOnce(authorizationRequired())
      .mockResolvedValueOnce(enabled);

    renderControl(<AutoUploadPanel />);
    expect(await screen.findByText('recurring-v1')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Refresh status' }));
    fireEvent.click(screen.getByRole('button', { name: 'Review scope and terms' }));

    await screen.findByRole('heading', { name: 'Authorize future automatic uploads' });
    expect(screen.queryByLabelText('Run on agent sessions')).not.toBeInTheDocument();
    expect(enableSpy).toHaveBeenNthCalledWith(1, { agent: 'claude', challenge_only: true });
    expect(screen.getByText(/separate from the consent you gave/i)).toBeInTheDocument();
    expect(screen.getByText('I authorize capped recurring uploads of eligible future traces.')).toBeInTheDocument();
    expect(screen.getByText('Hosted retention terms for recurring uploads.')).toBeInTheDocument();
    expect(screen.getByText('Every 1 day, on the next supported agent session')).toBeInTheDocument();
    expect(screen.getByText('claude → project-a')).toBeInTheDocument();
    expect(screen.getByText(/exact source\/project pairs shown above/i)).toBeInTheDocument();
    expect(screen.getByText(/can upload without my reviewing each bundle/i)).toBeInTheDocument();

    expect(
      screen.getByText('I certify every automatically uploaded bundle is my own lawful content.'),
    ).toBeInTheDocument();

    // Both affirmative acts are required: terms acceptance alone must not
    // enable the button, and the enable POST carries the certification version.
    const checkboxes = screen.getAllByRole('checkbox');
    expect(checkboxes).toHaveLength(2);
    fireEvent.click(checkboxes[0]);
    expect(screen.getByRole('button', { name: 'Enable automatic upload' })).toBeDisabled();
    fireEvent.click(screen.getByLabelText('Certify bundle ownership'));
    fireEvent.click(screen.getByRole('button', { name: 'Enable automatic upload' }));

    await waitFor(() => expect(enableSpy).toHaveBeenCalledTimes(2));
    expect(enableSpy).toHaveBeenNthCalledWith(2, {
      agent: 'claude',
      accepted_authorization_version: 'recurring-v2',
      accepted_retention_version: 'retention-v3',
      accepted_ownership_certification_version: 'ownership-v1',
      accepted_authorization_profile_hash: 'profile-hash-v2',
    });
    expect(await screen.findByText('recurring-v2')).toBeInTheDocument();

    await act(async () => {
      staleGet.resolve(initial);
      await flushPromises();
    });

    expect(screen.getByText('recurring-v2')).toBeInTheDocument();
    expect(screen.queryByText('recurring-v1')).not.toBeInTheDocument();
  });

  it('never blames a receipt grant when rotating an active enrollment', async () => {
    // Rotating credentials on a live enrollment returns email_verification_required
    // and opens the same verification block the receipt offer uses. No grant was
    // ever issued on this path, so it must not claim one expired.
    const initial = status({
      mode: 'enabled',
      scope: {
        sources: ['claude'],
        projects: ['project-a'],
        entries: [['claude', 'project-a']],
      },
      hooks: [
        { agent: 'claude', selected: true, configured: true, installed: true, last_observed_at: null },
        { agent: 'codex', selected: false, configured: true, installed: true, last_observed_at: null },
      ],
    });
    vi.spyOn(api.autoUpload, 'status').mockResolvedValue(initial);
    vi.spyOn(api.autoUpload, 'enable')
      .mockRejectedValueOnce(authorizationRequired())
      .mockRejectedValueOnce(new ApiError(409, 'Verify your email again to rotate recurring credentials.', {
        code: 'email_verification_required',
      }));
    vi.spyOn(api.share, 'uploadStatus').mockResolvedValue({
      verified_email: null,
      token_valid: false,
      expires_at: null,
      pending_email: null,
    });

    renderControl(<AutoUploadPanel />);
    fireEvent.click(await screen.findByRole('button', { name: 'Review scope and terms' }));
    await screen.findByRole('heading', { name: 'Authorize future automatic uploads' });

    const checkboxes = screen.getAllByRole('checkbox');
    fireEvent.click(checkboxes[0]);
    fireEvent.click(checkboxes[1]);
    fireEvent.click(screen.getByRole('button', { name: 'Enable automatic upload' }));

    expect(await screen.findByText(/single-use email verification/i)).toBeInTheDocument();
    expect(screen.queryByText(/receipt-issued enrollment grant/i)).not.toBeInTheDocument();
  });
});

describe('AutoUploadPanel status and controls', () => {
  it('shows every durable exact pair instead of implying a Cartesian scope', async () => {
    vi.spyOn(api.autoUpload, 'status').mockResolvedValueOnce(status({
      mode: 'enabled',
      scope: {
        sources: ['claude', 'codex'],
        projects: ['alpha', 'beta'],
        entries: [
          ['claude', 'alpha'],
          ['codex', 'beta'],
        ],
      },
    }));

    renderControl(<AutoUploadPanel />);

    const title = await screen.findByText(
      'Exact enrolled scope · 2 source/project pairs',
    );
    const scopeBlock = title.parentElement;
    expect(scopeBlock).toHaveTextContent('Claude Code → alpha');
    expect(scopeBlock).toHaveTextContent('Codex → beta');
    expect(scopeBlock).not.toHaveTextContent('Claude Code → beta');
    expect(scopeBlock).not.toHaveTextContent('Codex → alpha');
    expect(screen.getByText('Sources represented')).toBeInTheDocument();
    expect(screen.getByText('Projects represented')).toBeInTheDocument();
  });

  it('renders every mode, health, and transient overlay chip', async () => {
    const statusSpy = vi.spyOn(api.autoUpload, 'status');

    statusSpy.mockResolvedValueOnce(status({
      mode: 'enabled',
      health: 'action_required',
      overlay: 'running',
      pending_submission_state: 'submitting',
    }));
    const first = renderControl(<AutoUploadPanel />);
    expect(await screen.findByText('On')).toBeInTheDocument();
    expect(screen.getByText('Action required')).toBeInTheDocument();
    expect(screen.getByText('Running')).toBeInTheDocument();
    expect(screen.getByText('Request may be in flight')).toBeInTheDocument();
    first.unmount();

    statusSpy.mockResolvedValueOnce(status({
      mode: 'paused',
      health: 'retrying',
      pending_submission_state: 'sealed',
    }));
    const second = renderControl(<AutoUploadPanel />);
    expect(await screen.findByText('Paused')).toBeInTheDocument();
    expect(screen.getByText('Retrying')).toBeInTheDocument();
    expect(screen.getByText('Sealed recovery pending')).toBeInTheDocument();
    second.unmount();

    statusSpy.mockResolvedValueOnce(status({ overlay: 'revocation_pending' }));
    renderControl(<AutoUploadPanel />);
    await waitFor(() => expect(screen.getAllByText('Off')).toHaveLength(2));
    expect(screen.getByText('Revocation pending')).toBeInTheDocument();
  });

  it('polls transient state on the fast interval', async () => {
    vi.useFakeTimers();
    const running = status({ mode: 'enabled', overlay: 'running' });
    const statusSpy = vi.spyOn(api.autoUpload, 'status').mockResolvedValue(running);

    renderControl(<AutoUploadPanel />);
    await act(flushPromises);
    expect(statusSpy).toHaveBeenCalledTimes(1);

    await act(async () => {
      vi.advanceTimersByTime(2_499);
      await flushPromises();
    });
    expect(statusSpy).toHaveBeenCalledTimes(1);

    await act(async () => {
      vi.advanceTimersByTime(1);
      await flushPromises();
    });
    expect(statusSpy).toHaveBeenCalledTimes(2);
  });

  it('does not let an older poll overwrite a pause response', async () => {
    vi.useFakeTimers();
    const enabled = status({ mode: 'enabled', run_now_allowed: true });
    const paused = status({ mode: 'paused' });
    const stalePoll = deferred<AutoUploadStatus>();
    const statusSpy = vi.spyOn(api.autoUpload, 'status')
      .mockResolvedValueOnce(enabled)
      .mockReturnValueOnce(stalePoll.promise);
    vi.spyOn(api.autoUpload, 'pause').mockResolvedValue(paused);

    renderControl(<AutoUploadPanel />);
    await act(flushPromises);
    expect(screen.getByText('On')).toBeInTheDocument();

    await act(async () => {
      vi.advanceTimersByTime(30_000);
      await flushPromises();
    });
    expect(statusSpy).toHaveBeenCalledTimes(2);

    fireEvent.click(screen.getByRole('button', { name: 'Pause' }));
    await act(flushPromises);
    expect(screen.getByText('Paused')).toBeInTheDocument();

    await act(async () => {
      stalePoll.resolve(enabled);
      await flushPromises();
    });
    expect(screen.getByText('Paused')).toBeInTheDocument();
    expect(screen.queryByText('On')).not.toBeInTheDocument();
  });

  it('does not let an older status request overwrite a disable response', async () => {
    const enabled = status({ mode: 'enabled', run_now_allowed: true });
    const disabled = status({ mode: 'off', offer_available: true });
    const staleGet = deferred<AutoUploadStatus>();
    vi.spyOn(api.autoUpload, 'status')
      .mockResolvedValueOnce(enabled)
      .mockReturnValueOnce(staleGet.promise);
    const disableSpy = vi.spyOn(api.autoUpload, 'disable').mockResolvedValue(disabled);

    renderControl(<AutoUploadPanel />);
    expect(await screen.findByText('On')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Refresh status' }));
    fireEvent.click(screen.getByRole('button', { name: 'Turn off' }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Turn off' }));

    await waitFor(() => expect(disableSpy).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getAllByText('Off')).toHaveLength(2));

    await act(async () => {
      staleGet.resolve(enabled);
      await flushPromises();
    });
    expect(screen.getAllByText('Off')).toHaveLength(2);
    expect(screen.queryByText('On')).not.toBeInTheDocument();
  });

  it('links reviewable exclusions back to Share', async () => {
    vi.spyOn(api.autoUpload, 'status').mockResolvedValue(status({
      mode: 'enabled',
      eligibility: {
        selected_count: 1,
        eligible_count: 3,
        exclusion_counts: {
          held_or_embargoed: 2,
          source_excluded: 4,
          scope_pair_excluded: 1,
        },
      },
    }));

    renderControl(<AutoUploadPanel />);

    const reviewLink = await screen.findByRole('link', { name: 'Review 2 in Share' });
    expect(reviewLink).toHaveAttribute('href', '/share');
    expect(screen.getByText('Outside enrolled sources: 4')).toBeInTheDocument();
    expect(screen.getByText('Outside exact enrolled scope: 1')).toBeInTheDocument();
  });

  it('requires an explicit retry for pending revocation and calls disable again', async () => {
    const pending = status({ overlay: 'revocation_pending' });
    const revoked = status();
    vi.spyOn(api.autoUpload, 'status').mockResolvedValue(pending);
    const disableSpy = vi.spyOn(api.autoUpload, 'disable').mockResolvedValue(revoked);

    renderControl(<AutoUploadPanel />);

    expect(await screen.findByText(/it will not retry automatically/i)).toBeInTheDocument();
    expect(screen.queryByText(/can be offered after a successful hosted manual share/i)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Retry revocation' }));

    await waitFor(() => expect(disableSpy).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getAllByText('Off')).toHaveLength(2));
    expect(screen.queryByText('Revocation pending')).not.toBeInTheDocument();
  });
});

describe('AuthorizationDialog daemon version skew', () => {
  it('explains a v1-shaped challenge from an older daemon instead of a retry loop', async () => {
    const enrolled = status({
      mode: 'enabled',
      run_now_allowed: true,
      scope: {
        sources: ['claude'],
        projects: ['project-a'],
        entries: [['claude', 'project-a']],
      },
      authorization: { version: 'recurring-v1', text: 'terms' },
      hooks: [
        { agent: 'claude', selected: true, configured: true, installed: true, last_observed_at: null },
      ],
    });
    vi.spyOn(api.autoUpload, 'status').mockResolvedValue(enrolled);
    const v1Error = authorizationRequired();
    delete (v1Error.body as Record<string, unknown>).ownership_certification;
    vi.spyOn(api.autoUpload, 'enable').mockRejectedValue(v1Error);

    renderControl(<AutoUploadPanel />);
    fireEvent.click(await screen.findByRole('button', { name: 'Review scope and terms' }));

    expect(
      await screen.findByText(/older than this page/i),
    ).toBeInTheDocument();
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument();
  });

  it('rejects a challenge when any exact scope pair cannot be displayed', async () => {
    const enrolled = status({
      mode: 'enabled',
      run_now_allowed: true,
      scope: {
        sources: ['claude'],
        projects: ['project-a'],
        entries: [['claude', 'project-a']],
      },
      authorization: { version: 'recurring-v2', text: 'terms' },
      hooks: [
        { agent: 'claude', selected: true, configured: true, installed: true, last_observed_at: null },
      ],
    });
    vi.spyOn(api.autoUpload, 'status').mockResolvedValue(enrolled);
    const malformed = authorizationRequired();
    (malformed.body.scope as Record<string, unknown>).entries = [
      ['claude', 'project-a'],
      ['hidden-project-without-source'],
    ];
    vi.spyOn(api.autoUpload, 'enable').mockRejectedValue(malformed);

    renderControl(<AutoUploadPanel />);
    fireEvent.click(await screen.findByRole('button', { name: 'Review scope and terms' }));

    expect(await screen.findByText(/incompatible authorization challenge/i)).toBeInTheDocument();
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument();
  });
});

describe('AuthorizationDialog focus and dismissal', () => {
  it('moves focus into the dialog and stays dismissable while the challenge loads', async () => {
    const enrolled = status({
      mode: 'enabled',
      run_now_allowed: true,
      scope: {
        sources: ['claude'],
        projects: ['project-a'],
        entries: [['claude', 'project-a']],
      },
      authorization: { version: 'recurring-v1', text: 'terms' },
      hooks: [
        { agent: 'claude', selected: true, configured: true, installed: true, last_observed_at: null },
      ],
    });
    vi.spyOn(api.autoUpload, 'status').mockResolvedValue(enrolled);
    // The challenge fetch never settles → the dialog is stuck loading; it must
    // still move focus in and stay dismissable rather than trapping the user.
    vi.spyOn(api.autoUpload, 'enable').mockReturnValue(
      new Promise<AutoUploadStatus>(() => {}),
    );

    renderControl(<AutoUploadPanel />);
    fireEvent.click(await screen.findByRole('button', { name: 'Review scope and terms' }));

    const dialog = await screen.findByRole('dialog');
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));

    const cancel = screen.getByRole('button', { name: 'Cancel' });
    expect(cancel).not.toBeDisabled();
    fireEvent.click(cancel);
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('ignores a stale challenge response from a prior dialog opening', async () => {
    const enrolled = status({
      mode: 'enabled',
      run_now_allowed: true,
      scope: {
        sources: ['claude'],
        projects: ['project-a'],
        entries: [['claude', 'project-a']],
      },
      authorization: { version: 'recurring-v1', text: 'terms' },
      hooks: [
        { agent: 'claude', selected: true, configured: true, installed: true, last_observed_at: null },
      ],
    });
    const staleChallenge = deferred<AutoUploadStatus>();
    const freshChallenge = deferred<AutoUploadStatus>();
    const staleError = authorizationRequired();
    const freshError = authorizationRequired();
    (staleError.body.authorization as Record<string, unknown>).text = 'Stale authorization text';
    (freshError.body.authorization as Record<string, unknown>).text = 'Fresh authorization text';
    vi.spyOn(api.autoUpload, 'status').mockResolvedValue(enrolled);
    vi.spyOn(api.autoUpload, 'enable')
      .mockReturnValueOnce(staleChallenge.promise)
      .mockReturnValueOnce(freshChallenge.promise);

    renderControl(<AutoUploadPanel />);
    fireEvent.click(await screen.findByRole('button', { name: 'Review scope and terms' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel' }));
    fireEvent.click(screen.getByRole('button', { name: 'Review scope and terms' }));

    await act(async () => {
      freshChallenge.reject(freshError);
      await flushPromises();
    });
    expect(await screen.findByText('Fresh authorization text')).toBeInTheDocument();

    await act(async () => {
      staleChallenge.reject(staleError);
      await flushPromises();
    });
    expect(screen.getByText('Fresh authorization text')).toBeInTheDocument();
    expect(screen.queryByText('Stale authorization text')).not.toBeInTheDocument();
  });
});
