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
    offer_available: false,
    scope: { sources: [], projects: [] },
    cap: 5,
    cadence_days: 7,
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
    authorization: {
      version: 'recurring-v2',
      text: 'I authorize capped recurring uploads of eligible future traces.',
    },
    retention: {
      version: 'retention-v3',
      text: 'Hosted retention terms for recurring uploads.',
    },
    scope: { sources: ['claude'], projects: ['project-a'] },
    ai: { enabled: true, backend: 'codex' },
    cap: 5,
    cadence_days: 7,
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
  it('requires a manual receipt and server capability, then persists dismissal', async () => {
    const statusSpy = vi.spyOn(api.autoUpload, 'status');

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
    expect(screen.getByText(/upload without individual review/i)).toBeInTheDocument();

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
});

describe('AutoUploadPanel authorization', () => {
  it('shows the distinct recurring wording, retains the selected hook, and rejects a stale GET after enable', async () => {
    const initial = status({
      mode: 'enabled',
      run_now_allowed: true,
      scope: { sources: ['claude'], projects: ['project-a'] },
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

    const agentSelect = await screen.findByLabelText('Run on agent sessions');
    expect(agentSelect).toHaveValue('claude');
    expect(enableSpy).toHaveBeenNthCalledWith(1, { agent: 'claude', challenge_only: true });
    expect(screen.getByRole('heading', { name: 'Authorize future automatic uploads' })).toBeInTheDocument();
    expect(screen.getByText(/separate from the consent you gave/i)).toBeInTheDocument();
    expect(screen.getByText('I authorize capped recurring uploads of eligible future traces.')).toBeInTheDocument();
    expect(screen.getByText('Hosted retention terms for recurring uploads.')).toBeInTheDocument();
    expect(screen.getByText(/can upload without my reviewing each bundle/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('checkbox'));
    fireEvent.click(screen.getByRole('button', { name: 'Enable automatic upload' }));

    await waitFor(() => expect(enableSpy).toHaveBeenCalledTimes(2));
    expect(enableSpy).toHaveBeenNthCalledWith(2, {
      agent: 'claude',
      accepted_authorization_version: 'recurring-v2',
      accepted_retention_version: 'retention-v3',
    });
    expect(await screen.findByText('recurring-v2')).toBeInTheDocument();

    await act(async () => {
      staleGet.resolve(initial);
      await flushPromises();
    });

    expect(screen.getByText('recurring-v2')).toBeInTheDocument();
    expect(screen.queryByText('recurring-v1')).not.toBeInTheDocument();
  });
});

describe('AutoUploadPanel status and controls', () => {
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
        exclusion_counts: { held_or_embargoed: 2, source_excluded: 4 },
      },
    }));

    renderControl(<AutoUploadPanel />);

    const reviewLink = await screen.findByRole('link', { name: 'Review 2 in Share' });
    expect(reviewLink).toHaveAttribute('href', '/share');
    expect(screen.getByText('Outside enrolled sources: 4')).toBeInTheDocument();
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
