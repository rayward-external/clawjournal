import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api, ApiError } from '../api.ts';
import type { AutoUploadStatus } from '../types.ts';
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

function renderControl(ui: React.ReactNode) {
  return render(
    <MemoryRouter>
      <ToastProvider>{ui}</ToastProvider>
    </MemoryRouter>,
  );
}

async function openPanelDetails() {
  fireEvent.click(await screen.findByRole('button', { name: 'View details' }));
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

  it('shows durable background setup progress after the receipt', async () => {
    vi.spyOn(api.autoUpload, 'status').mockResolvedValue(status({
      overlay: 'enrollment_pending',
      enrollment_setup: {
        state: 'running',
        stage: 'scanning',
        source: 'codex',
        position: 3,
        total: 8,
        attempt_count: 1,
        error_code: null,
        message: null,
        retryable: false,
      },
    }));

    const view = renderControl(
      <AutoUploadOffer manualReceiptId="receipt-background" />,
    );

    expect(await screen.findByText(
      'Setting up automatic uploads in the background',
    )).toBeInTheDocument();
    expect(screen.getByText(/Refreshing codex logs: 3\/8/)).toBeInTheDocument();
    expect(screen.getByText(/You can close this page/)).toBeInTheDocument();
    expect(screen.queryByText('Share future traces automatically?')).not.toBeInTheDocument();
    view.unmount();
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

  it('goes directly to the exact all-supported-source authorization', async () => {
    vi.spyOn(api.autoUpload, 'status').mockResolvedValueOnce(status({
      offer_available: true,
    }));
    const allSupportedChallenge = authorizationRequired();
    (allSupportedChallenge.body.scope as Record<string, unknown>).sources = ['claude', 'codex'];
    (allSupportedChallenge.body.scope as Record<string, unknown>).projects = [
      'project-a',
      'project-b',
    ];
    (allSupportedChallenge.body.scope as Record<string, unknown>).entries = [
      ['claude', 'project-a'],
      ['codex', 'project-b'],
    ];
    const enableSpy = vi.spyOn(api.autoUpload, 'enable')
      .mockRejectedValueOnce(allSupportedChallenge);

    renderControl(<AutoUploadOffer manualReceiptId="receipt-all-supported" />);

    expect(await screen.findByText('I authorize capped recurring uploads of eligible future traces.')).toBeInTheDocument();
    expect(enableSpy).toHaveBeenCalledOnce();
    expect(enableSpy).toHaveBeenCalledWith({ agent: 'auto', challenge_only: true });
    expect(screen.queryByLabelText('Export source scope')).not.toBeInTheDocument();
    expect(screen.queryByText('Choose what automatic uploads may include')).not.toBeInTheDocument();
    expect(screen.getByText('Claude Code and Codex - matches exact upload scope')).toBeInTheDocument();
    expect(screen.getByText(/claude \u2192 project-a/)).toBeInTheDocument();
    expect(screen.getByText(/codex \u2192 project-b/)).toBeInTheDocument();
    expect(screen.getByText('All currently supported agent sources are included automatically.')).toBeInTheDocument();
  });

  it('explains an oversized scope instead of echoing the CLI-worded error', async () => {
    vi.spyOn(api.autoUpload, 'status').mockResolvedValueOnce(status({
      offer_available: true,
    }));
    vi.spyOn(api.autoUpload, 'enable').mockRejectedValueOnce(new ApiError(400, 'Scope too large', {
      code: 'scope_too_large',
      message: 'The exact source/project scope exceeds the hosted limit of 200 entries; '
        + 'exclude projects (config --exclude), then try again.',
      scope_blockers: ['scope_too_large'],
    }));

    renderControl(<AutoUploadOffer manualReceiptId="receipt-oversized-scope" />);

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'This scope has too many exact source/project pairs',
    );
    expect(screen.queryByText('Choose what automatic uploads may include')).not.toBeInTheDocument();
  });

  it('enables inline with the receipt grant and lets the daemon infer hooks', async () => {
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
    expect(enableSpy.mock.calls[1][0].agent).toBe('auto');
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

    await openPanelDetails();
    expect(await screen.findByRole('button', { name: 'Pause' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Turn off' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument();
  });
});

describe('AutoUploadPanel authorization', () => {
  it('shows the distinct recurring wording, derives hooks from scope, and rejects a stale GET after enable', async () => {
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
    await openPanelDetails();
    expect(await screen.findByText('recurring-v1')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Refresh status' }));
    fireEvent.click(screen.getByRole('button', { name: 'Review scope and terms' }));

    await screen.findByRole('heading', { name: 'Enable automatic uploads?' });
    expect(screen.queryByLabelText('Run on agent sessions')).not.toBeInTheDocument();
    expect(enableSpy).toHaveBeenNthCalledWith(1, { agent: 'auto', challenge_only: true });
    expect(screen.getByText(/automatically upload up to 5 eligible/i)).toBeInTheDocument();
    expect(screen.queryByText('Claude Code - matches exact upload scope')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'View scope and terms' }));
    expect(screen.getByText('Claude Code - matches exact upload scope')).toBeInTheDocument();
    expect(screen.getByText('I authorize capped recurring uploads of eligible future traces.')).toBeInTheDocument();
    expect(screen.getByText('Hosted retention terms for recurring uploads.')).toBeInTheDocument();
    expect(screen.getByText('Every 1 day, on the next supported agent session')).toBeInTheDocument();
    expect(screen.getByText('claude → project-a')).toBeInTheDocument();

    expect(
      screen.getByText('I certify every automatically uploaded bundle is my own lawful content.'),
    ).toBeInTheDocument();

    // The explicit Enable click accepts the displayed authorization; ownership
    // remains a separate affirmative checkbox required by protocol v2.
    const checkboxes = screen.getAllByRole('checkbox');
    expect(checkboxes).toHaveLength(1);
    expect(screen.getByRole('button', { name: 'Enable automatic upload' })).toBeDisabled();
    fireEvent.click(screen.getByLabelText('Certify bundle ownership'));
    fireEvent.click(screen.getByRole('button', { name: 'Enable automatic upload' }));

    await waitFor(() => expect(enableSpy).toHaveBeenCalledTimes(2));
    expect(enableSpy).toHaveBeenNthCalledWith(2, {
      agent: 'auto',
      accepted_authorization_version: 'recurring-v2',
      accepted_retention_version: 'retention-v3',
      accepted_ownership_certification_version: 'ownership-v1',
      accepted_authorization_profile_hash: 'profile-hash-v2',
      progress_id: expect.any(String),
    });
    expect(await screen.findByText('recurring-v2')).toBeInTheDocument();

    await act(async () => {
      staleGet.resolve(initial);
      await flushPromises();
    });

    expect(screen.getByText('recurring-v2')).toBeInTheDocument();
    expect(screen.queryByText('recurring-v1')).not.toBeInTheDocument();
  });

  it('shows scan-lock waiting and live source project progress while enabling', async () => {
    const initial = status({
      mode: 'enabled',
      run_now_allowed: true,
      scope: {
        sources: ['claude'],
        projects: ['project-a'],
        entries: [['claude', 'project-a']],
      },
      authorization: { version: 'recurring-v1', text: 'old terms' },
    });
    const enabled = status({
      ...initial,
      authorization: { version: 'recurring-v2', text: 'new terms' },
    });
    const enableRequest = deferred<AutoUploadStatus>();
    vi.spyOn(api.autoUpload, 'status').mockResolvedValue(initial);
    vi.spyOn(api.autoUpload, 'enable')
      .mockRejectedValueOnce(authorizationRequired())
      .mockReturnValueOnce(enableRequest.promise);
    vi.spyOn(api.autoUpload, 'enableProgress')
      .mockResolvedValueOnce({
        progress_id: 'progress-issue165',
        stage: 'waiting_for_scan_lock',
        message: 'Waiting for another scan to finish before refreshing...',
        source: null,
        current_project: null,
        total_projects: null,
        updated_at: '2026-07-29T00:00:00Z',
      })
      .mockResolvedValue({
        progress_id: 'progress-issue165',
        stage: 'scanning',
        message: 'Refreshing Codex source logs: 42/118 projects',
        source: 'codex',
        current_project: 42,
        total_projects: 118,
        updated_at: '2026-07-29T00:00:01Z',
      });

    renderControl(<AutoUploadPanel />);
    await openPanelDetails();
    fireEvent.click(await screen.findByRole('button', { name: 'Review scope and terms' }));
    await screen.findByRole('heading', { name: 'Enable automatic uploads?' });
    fireEvent.click(screen.getByLabelText('Certify bundle ownership'));
    fireEvent.click(screen.getByRole('button', { name: 'Enable automatic upload' }));

    expect(screen.getByText(
      'Checking the hosted service and current terms...',
    )).toBeInTheDocument();
    expect(await screen.findByText(
      'Waiting for another scan to finish before refreshing...',
      {},
      { timeout: 2_000 },
    )).toBeInTheDocument();
    expect(await screen.findByText(
      'Refreshing Codex source logs: 42/118 projects',
      {},
      { timeout: 2_000 },
    )).toBeInTheDocument();
    const progress = screen.getByRole('progressbar', {
      name: 'Automatic upload enrollment refresh',
    });
    expect(progress).toHaveAttribute('value', '42');
    expect(progress).toHaveAttribute('max', '118');

    await act(async () => {
      enableRequest.resolve(enabled);
      await flushPromises();
    });
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
    await openPanelDetails();
    fireEvent.click(await screen.findByRole('button', { name: 'Review scope and terms' }));
    await screen.findByRole('heading', { name: 'Enable automatic uploads?' });

    fireEvent.click(screen.getByLabelText('Certify bundle ownership'));
    fireEvent.click(screen.getByRole('button', { name: 'Enable automatic upload' }));

    expect(await screen.findByText(/single-use email verification/i)).toBeInTheDocument();
    expect(screen.queryByText(/receipt-issued enrollment grant/i)).not.toBeInTheDocument();
  });
});

describe('AutoUploadPanel status and controls', () => {
  it('keeps Settings compact until details are requested', async () => {
    vi.spyOn(api.autoUpload, 'status').mockResolvedValueOnce(status({
      mode: 'enabled',
      run_now_allowed: true,
      scope: {
        sources: ['codex'],
        projects: ['project-a'],
        entries: [['codex', 'project-a']],
      },
    }));

    renderControl(<AutoUploadPanel />);

    const toggle = await screen.findByRole('switch', { name: 'Automatic uploads' });
    expect(toggle).toHaveAttribute('aria-checked', 'true');
    expect(screen.queryByText('Sources represented')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Run now' })).not.toBeInTheDocument();

    await openPanelDetails();
    expect(screen.getByText('Sources represented')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Run now' })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Hide details' }));
    expect(screen.queryByText('Sources represented')).not.toBeInTheDocument();
  });

  it('starts the existing authorization flow from the off switch', async () => {
    vi.spyOn(api.autoUpload, 'status').mockResolvedValueOnce(status({
      mode: 'off',
      offer_available: true,
    }));
    vi.spyOn(api.autoUpload, 'enable').mockRejectedValueOnce(authorizationRequired());

    renderControl(<AutoUploadPanel />);

    const toggle = await screen.findByRole('switch', { name: 'Automatic uploads' });
    expect(toggle).toHaveAttribute('aria-checked', 'false');
    fireEvent.click(toggle);

    expect(
      await screen.findByRole('heading', { name: 'Enable automatic uploads?' }),
    ).toBeInTheDocument();
  });

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
    await openPanelDetails();

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
    const firstSwitch = await screen.findByRole('switch', { name: 'Automatic uploads' });
    expect(firstSwitch).toHaveAttribute('aria-checked', 'true');
    expect(firstSwitch).toHaveTextContent('On');
    await openPanelDetails();
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
    const secondSwitch = await screen.findByRole('switch', { name: 'Automatic uploads' });
    expect(secondSwitch).toHaveAttribute('aria-checked', 'true');
    expect(secondSwitch).toHaveTextContent('Paused');
    await openPanelDetails();
    expect(screen.getByText('Retrying')).toBeInTheDocument();
    expect(screen.getByText('Sealed recovery pending')).toBeInTheDocument();
    second.unmount();

    statusSpy.mockResolvedValueOnce(status({ overlay: 'revocation_pending' }));
    renderControl(<AutoUploadPanel />);
    const thirdSwitch = await screen.findByRole('switch', { name: 'Automatic uploads' });
    expect(thirdSwitch).toHaveAttribute('aria-checked', 'false');
    expect(thirdSwitch).toHaveTextContent('Off');
    await openPanelDetails();
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
    const enabled = status({ mode: 'enabled', run_now_allowed: true });
    const paused = status({ mode: 'paused' });
    const stalePoll = deferred<AutoUploadStatus>();
    const statusSpy = vi.spyOn(api.autoUpload, 'status')
      .mockResolvedValueOnce(enabled)
      .mockReturnValueOnce(stalePoll.promise);
    vi.spyOn(api.autoUpload, 'pause').mockResolvedValue(paused);

    renderControl(<AutoUploadPanel />);
    const toggle = await screen.findByRole('switch', { name: 'Automatic uploads' });
    expect(toggle).toHaveTextContent('On');
    await openPanelDetails();

    fireEvent.click(screen.getByRole('button', { name: 'Refresh status' }));
    expect(statusSpy).toHaveBeenCalledTimes(2);

    fireEvent.click(screen.getByRole('button', { name: 'Pause' }));
    await waitFor(() => expect(toggle).toHaveTextContent('Paused'));

    await act(async () => {
      stalePoll.resolve(enabled);
      await flushPromises();
    });
    expect(toggle).toHaveTextContent('Paused');
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
    const toggle = await screen.findByRole('switch', { name: 'Automatic uploads' });
    expect(toggle).toHaveTextContent('On');

    await openPanelDetails();
    fireEvent.click(screen.getByRole('button', { name: 'Refresh status' }));
    fireEvent.click(toggle);
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Turn off' }));

    await waitFor(() => expect(disableSpy).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(toggle).toHaveTextContent('Off'));
    expect(toggle).toHaveAttribute('aria-checked', 'false');

    await act(async () => {
      staleGet.resolve(enabled);
      await flushPromises();
    });
    expect(toggle).toHaveTextContent('Off');
    expect(toggle).toHaveAttribute('aria-checked', 'false');
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
    await openPanelDetails();

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

    await openPanelDetails();
    expect(await screen.findByText(/it will not retry automatically/i)).toBeInTheDocument();
    expect(screen.queryByText(/can be offered after a successful hosted manual share/i)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Retry revocation' }));

    await waitFor(() => expect(disableSpy).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(
      screen.getByRole('switch', { name: 'Automatic uploads' }),
    ).toHaveAttribute('aria-checked', 'false'));
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
    await openPanelDetails();
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
    await openPanelDetails();
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
    await openPanelDetails();
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
    await openPanelDetails();
    fireEvent.click(await screen.findByRole('button', { name: 'Review scope and terms' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel' }));
    fireEvent.click(screen.getByRole('button', { name: 'Review scope and terms' }));

    await act(async () => {
      freshChallenge.reject(freshError);
      await flushPromises();
    });
    fireEvent.click(await screen.findByRole('button', { name: 'View scope and terms' }));
    expect(await screen.findByText('Fresh authorization text')).toBeInTheDocument();

    await act(async () => {
      staleChallenge.reject(staleError);
      await flushPromises();
    });
    expect(screen.getByText('Fresh authorization text')).toBeInTheDocument();
    expect(screen.queryByText('Stale authorization text')).not.toBeInTheDocument();
  });
});
