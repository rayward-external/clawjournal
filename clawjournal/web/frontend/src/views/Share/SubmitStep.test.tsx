import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api, ApiError } from '../../api.ts';
import type { AutoUploadStatus } from '../../types.ts';
import { SubmitStep, type SubmitStepProps } from './SubmitStep.tsx';
import { globalStyles } from './styles.tsx';

vi.mock('./successChime.ts', () => ({
  cancelSuccessChime: vi.fn(),
  playSuccessChime: vi.fn(),
  primeSuccessChime: vi.fn(),
}));

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function automaticUploadStatus(
  overrides: Partial<AutoUploadStatus> = {},
): AutoUploadStatus {
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
    eligibility: {
      selected_count: 0,
      eligible_count: 0,
      exclusion_counts: {},
    },
    last_result: null,
    ...overrides,
  };
}

function authorizationRequired() {
  return new ApiError(409, 'Authorization required', {
    code: 'authorization_required',
    authorization_profile_hash: 'profile-hash-v3',
    authorization: {
      version: 'recurring-v3',
      text: 'I authorize capped recurring uploads of eligible future traces.',
    },
    retention: {
      version: 'retention-v1',
      text: 'The service retains accepted redacted bundles and receipt metadata.',
    },
    ownership_certification: {
      version: 'ownership-v1',
      text: 'I certify every automatically uploaded bundle is mine to share.',
    },
    scope: {
      sources: ['codex'],
      projects: ['codex:project-a'],
      entries: [['codex', 'codex:project-a']],
    },
    ai: { enabled: false, backend: null },
    cap: 5,
    cadence_days: 1,
    maximum_bundle_size: 50 * 1024 * 1024,
    destination_origin: 'https://share.example.test',
  });
}

function renderSubmit(overrides: {
  onSubmitted?: SubmitStepProps['onSubmitted'];
  toast?: SubmitStepProps['toast'];
  shareDestination?: SubmitStepProps['shareDestination'];
} = {}) {
  const onSubmitted = overrides.onSubmitted
    ?? vi.fn<SubmitStepProps['onSubmitted']>();
  const toast = overrides.toast ?? vi.fn<SubmitStepProps['toast']>();
  render(
    <SubmitStep
      stepperHeader={null}
      shareId="share-1"
      bundle={{ traces: 1, created: 'Jul 25', approxSize: '2 KB' }}
      shareDestination={overrides.shareDestination ?? {
          configured: true,
          daemon_upload_supported: true,
          submissions_open: true,
          preferred_upload_flow: 'browser_zip',
          cli_ingest_supported: false,
          share_page_url: 'https://share.example.test/share',
        }}
      aiPiiEnabled={false}
      onSubmitted={onSubmitted}
      onDownloadZip={vi.fn()}
      globalStyles={globalStyles}
      toast={toast}
    />,
  );
  return { onSubmitted, toast };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('SubmitStep automatic-upload opt-in', () => {
  it('keeps the accepted email domains behind a concise details link', async () => {
    vi.spyOn(api.share, 'consent').mockResolvedValue({
      consent_text: 'Manual share consent.',
      retention_text: 'Manual retention terms.',
      consent_version: 'consent-v1',
      retention_policy_version: 'retention-v1',
    });
    vi.spyOn(api.share, 'uploadStatus').mockResolvedValue({
      verified_email: null,
      token_valid: false,
      expires_at: null,
      pending_email: null,
    });
    vi.spyOn(api.autoUpload, 'status').mockResolvedValue(
      automaticUploadStatus({ mode: 'enabled' }),
    );

    renderSubmit({
      shareDestination: {
        configured: true,
        daemon_upload_supported: true,
        submissions_open: true,
        preferred_upload_flow: 'browser_zip',
        cli_ingest_supported: false,
        share_page_url: 'https://share.example.test/share',
        supported_institution_email_policy: {
          domain_suffixes: ['.edu', '.ac.uk', 'rayward.ai'],
          explicit_collaborators_supported: true,
        },
      },
    });

    const details = await screen.findByRole('button', {
      name: 'View accepted domains',
    });
    expect(screen.getByText(
      /Academic and approved collaborator emails are supported/,
    )).toBeInTheDocument();
    expect(screen.queryByText(/^Accepted domains:/)).not.toBeInTheDocument();

    fireEvent.click(details);

    expect(details).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByText(
      'Accepted domains: .edu, .ac.uk, rayward.ai',
    )).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', {
      name: 'Hide accepted domains',
    }));
    expect(screen.queryByText(/^Accepted domains:/)).not.toBeInTheDocument();
  });

  it('uses the manual receipt grant to enable automatic uploads in one submit', async () => {
    const calls: string[] = [];
    const enableRequest = deferred<AutoUploadStatus>();
    vi.spyOn(api.share, 'consent').mockResolvedValue({
      consent_text: 'Manual share consent.',
      retention_text: 'Manual retention terms.',
      consent_version: 'consent-v1',
      retention_policy_version: 'retention-v1',
    });
    vi.spyOn(api.share, 'uploadStatus').mockResolvedValue({
      verified_email: 'participant@example.edu',
      token_valid: true,
      expires_at: '2099-01-01T00:00:00Z',
      pending_email: null,
    });
    vi.spyOn(api.autoUpload, 'status').mockResolvedValue(
      automaticUploadStatus(),
    );
    const enableSpy = vi.spyOn(api.autoUpload, 'enable')
      .mockRejectedValueOnce(authorizationRequired())
      .mockImplementationOnce(async () => {
        calls.push('enable');
        return enableRequest.promise;
      });
    vi.spyOn(api.autoUpload, 'enableProgress').mockResolvedValue({
      progress_id: 'progress-issue165',
      stage: 'scanning',
      message: 'Refreshing Codex source logs: 42/118 projects',
      source: 'codex',
      current_project: 42,
      total_projects: 118,
      updated_at: '2026-07-29T00:00:00Z',
    });
    vi.spyOn(api.shares, 'upload').mockImplementation(async () => {
      calls.push('upload');
      return {
        ok: true,
        shared_at: '2026-07-25T00:00:00Z',
        receipt_id: 'receipt-1',
        hosted_status: 'accepted',
        session_count: 1,
        bundle_hash: 'bundle-hash',
        redaction_summary: { total_redactions: 0, by_type: {} },
      };
    });
    const { onSubmitted, toast } = renderSubmit();

    const automaticUpload = await screen.findByLabelText(
      'Enable automatic uploads after this share',
    );
    expect(automaticUpload).toBeChecked();
    expect(enableSpy).toHaveBeenNthCalledWith(1, {
      agent: 'all',
      challenge_only: true,
      prepare_for_manual_share: true,
    });

    fireEvent.click(screen.getByText('View details'));
    expect(screen.getByText(/Recurring authorization · recurring-v3/)).toBeInTheDocument();
    expect(screen.getByText(/codex → codex:project-a/)).toBeInTheDocument();

    fireEvent.click(screen.getByText('I accept the displayed consent and data-use terms.'));
    fireEvent.click(screen.getByText(/I certify this bundle and future automatically uploaded bundles/));
    fireEvent.click(screen.getByRole('button', {
      name: 'Submit and enable automatic uploads',
    }));

    expect(await screen.findByText(
      'Refreshing Codex source logs: 42/118 projects',
      {},
      { timeout: 2_000 },
    )).toBeInTheDocument();
    expect(screen.getByRole('button', {
      name: 'Refreshing history...',
    })).toBeDisabled();
    await act(async () => {
      enableRequest.resolve(automaticUploadStatus({ mode: 'enabled' }));
      await Promise.resolve();
    });

    await waitFor(() => {
      expect(onSubmitted).toHaveBeenCalledWith(
        'receipt-1',
        'accepted',
        null,
      );
    });
    expect(calls).toEqual(['upload', 'enable']);
    expect(enableSpy).toHaveBeenNthCalledWith(2, {
      agent: 'codex',
      accepted_authorization_version: 'recurring-v3',
      accepted_retention_version: 'retention-v1',
      accepted_ownership_certification_version: 'ownership-v1',
      accepted_authorization_profile_hash: 'profile-hash-v3',
      progress_id: expect.any(String),
    });
    expect(toast).toHaveBeenCalledWith(
      'Submitted and automatic uploads enabled',
      'success',
    );
  });

  it('lets the participant opt out before the manual submit', async () => {
    vi.spyOn(api.share, 'consent').mockResolvedValue({
      consent_text: 'Manual share consent.',
      retention_text: 'Manual retention terms.',
      consent_version: 'consent-v1',
      retention_policy_version: 'retention-v1',
    });
    vi.spyOn(api.share, 'uploadStatus').mockResolvedValue({
      verified_email: 'participant@example.edu',
      token_valid: true,
      expires_at: '2099-01-01T00:00:00Z',
      pending_email: null,
    });
    vi.spyOn(api.autoUpload, 'status').mockResolvedValue(
      automaticUploadStatus(),
    );
    const enableSpy = vi.spyOn(api.autoUpload, 'enable')
      .mockRejectedValueOnce(authorizationRequired());
    vi.spyOn(api.shares, 'upload').mockResolvedValue({
      ok: true,
      shared_at: '2026-07-25T00:00:00Z',
      receipt_id: 'receipt-opt-out',
      hosted_status: 'accepted',
      session_count: 1,
      bundle_hash: 'bundle-hash',
      redaction_summary: { total_redactions: 0, by_type: {} },
    });
    const { onSubmitted, toast } = renderSubmit();

    const automaticUpload = await screen.findByLabelText(
      'Enable automatic uploads after this share',
    );
    expect(automaticUpload).toBeChecked();
    fireEvent.click(automaticUpload);
    expect(automaticUpload).not.toBeChecked();

    fireEvent.click(screen.getByText('I accept the displayed consent and data-use terms.'));
    fireEvent.click(screen.getByText(/I certify this bundle is mine to submit/));
    fireEvent.click(screen.getByRole('button', {
      name: 'Submit to ClawJournal Research',
    }));

    await waitFor(() => {
      expect(onSubmitted).toHaveBeenCalledWith(
        'receipt-opt-out',
        'accepted',
        null,
      );
    });
    expect(enableSpy).toHaveBeenCalledTimes(1);
    expect(toast).toHaveBeenCalledWith('Submitted', 'success');
  });

  it('does not offer the combined action without a supported receipt grant', async () => {
    vi.spyOn(api.share, 'consent').mockResolvedValue({
      consent_text: 'Manual share consent.',
      retention_text: 'Manual retention terms.',
      consent_version: 'consent-v1',
      retention_policy_version: 'retention-v1',
    });
    vi.spyOn(api.share, 'uploadStatus').mockResolvedValue({
      verified_email: 'participant@example.edu',
      token_valid: true,
      expires_at: '2099-01-01T00:00:00Z',
      pending_email: null,
    });
    vi.spyOn(api.autoUpload, 'status').mockResolvedValue(
      automaticUploadStatus(),
    );
    const enableSpy = vi.spyOn(api.autoUpload, 'enable').mockRejectedValue(
      new ApiError(400, 'Receipt grant unavailable', {
        code: 'enrollment_grant_unavailable',
      }),
    );

    renderSubmit();

    await waitFor(() => {
      expect(enableSpy).toHaveBeenCalledWith({
        agent: 'all',
        challenge_only: true,
        prepare_for_manual_share: true,
      });
    });
    expect(screen.queryByLabelText(
      'Enable automatic uploads after this share',
    )).not.toBeInTheDocument();
    expect(screen.getByRole('button', {
      name: 'Submit to ClawJournal Research',
    })).toBeInTheDocument();
  });

  it('locks the automatic-upload choice once submission begins', async () => {
    vi.spyOn(api.share, 'consent').mockResolvedValue({
      consent_text: 'Manual share consent.',
      retention_text: 'Manual retention terms.',
      consent_version: 'consent-v1',
      retention_policy_version: 'retention-v1',
    });
    vi.spyOn(api.share, 'uploadStatus').mockResolvedValue({
      verified_email: 'participant@example.edu',
      token_valid: true,
      expires_at: '2099-01-01T00:00:00Z',
      pending_email: null,
    });
    vi.spyOn(api.autoUpload, 'status').mockResolvedValue(
      automaticUploadStatus(),
    );
    const enableSpy = vi.spyOn(api.autoUpload, 'enable')
      .mockRejectedValueOnce(authorizationRequired())
      .mockResolvedValueOnce(automaticUploadStatus({ mode: 'enabled' }));
    let completeUpload: (
      result: Awaited<ReturnType<typeof api.shares.upload>>,
    ) => void = () => {};
    vi.spyOn(api.shares, 'upload').mockImplementation(() => new Promise(
      resolve => {
        completeUpload = resolve;
      },
    ));
    const { onSubmitted } = renderSubmit();

    const automaticUpload = await screen.findByLabelText(
      'Enable automatic uploads after this share',
    );
    fireEvent.click(screen.getByText(
      'I accept the displayed consent and data-use terms.',
    ));
    fireEvent.click(screen.getByText(
      /I certify this bundle and future automatically uploaded bundles/,
    ));
    fireEvent.click(screen.getByRole('button', {
      name: 'Submit and enable automatic uploads',
    }));

    await waitFor(() => {
      expect(automaticUpload).toBeDisabled();
    });
    fireEvent.click(automaticUpload);
    expect(automaticUpload).toBeChecked();

    completeUpload({
      ok: true,
      shared_at: '2026-07-25T00:00:00Z',
      receipt_id: 'receipt-locked-choice',
      hosted_status: 'accepted',
      session_count: 1,
      bundle_hash: 'bundle-hash',
      redaction_summary: { total_redactions: 0, by_type: {} },
    });

    await waitFor(() => {
      expect(onSubmitted).toHaveBeenCalledWith(
        'receipt-locked-choice',
        'accepted',
        null,
      );
    });
    expect(enableSpy).toHaveBeenCalledTimes(2);
  });

  it('shows an existing enrollment as checked and locked without re-enrolling', async () => {
    vi.spyOn(api.share, 'consent').mockResolvedValue({
      consent_text: 'Manual share consent.',
      retention_text: 'Manual retention terms.',
      consent_version: 'consent-v1',
      retention_policy_version: 'retention-v1',
    });
    vi.spyOn(api.share, 'uploadStatus').mockResolvedValue({
      verified_email: 'participant@example.edu',
      token_valid: true,
      expires_at: '2099-01-01T00:00:00Z',
      pending_email: null,
    });
    vi.spyOn(api.autoUpload, 'status').mockResolvedValue(
      automaticUploadStatus({
        mode: 'enabled',
        scope: {
          sources: ['codex'],
          projects: ['codex:project-a'],
          entries: [['codex', 'codex:project-a']],
        },
        authorization: { version: 'recurring-v3', text: null },
        retention: { version: 'retention-v1', text: null },
      }),
    );
    const enableSpy = vi.spyOn(api.autoUpload, 'enable');
    vi.spyOn(api.shares, 'upload').mockResolvedValue({
      ok: true,
      shared_at: '2026-07-25T00:00:00Z',
      receipt_id: 'receipt-existing',
      hosted_status: 'accepted',
      session_count: 1,
      bundle_hash: 'bundle-hash',
      redaction_summary: { total_redactions: 0, by_type: {} },
    });
    const { onSubmitted } = renderSubmit();

    const automaticUpload = await screen.findByLabelText(
      'Enable automatic uploads after this share',
    );
    expect(automaticUpload).toBeChecked();
    expect(automaticUpload).toBeDisabled();
    expect(screen.getByText(/Automatic uploads are already enabled/)).toBeInTheDocument();

    fireEvent.click(screen.getByText('View details'));
    expect(screen.getByText('Existing recurring enrollment')).toBeInTheDocument();
    expect(screen.getByText(/codex → codex:project-a/)).toBeInTheDocument();

    fireEvent.click(screen.getByText('I accept the displayed consent and data-use terms.'));
    fireEvent.click(screen.getByText(/I certify this bundle is mine to submit/));
    fireEvent.click(screen.getByRole('button', {
      name: 'Submit to ClawJournal Research',
    }));

    await waitFor(() => {
      expect(onSubmitted).toHaveBeenCalledWith(
        'receipt-existing',
        'accepted',
        null,
      );
    });
    expect(enableSpy).not.toHaveBeenCalled();
  });

  it('keeps a successful manual receipt when automatic enrollment fails', async () => {
    vi.spyOn(api.share, 'consent').mockResolvedValue({
      consent_text: 'Manual share consent.',
      retention_text: 'Manual retention terms.',
      consent_version: 'consent-v1',
      retention_policy_version: 'retention-v1',
    });
    vi.spyOn(api.share, 'uploadStatus').mockResolvedValue({
      verified_email: 'participant@example.edu',
      token_valid: true,
      expires_at: '2099-01-01T00:00:00Z',
      pending_email: null,
    });
    vi.spyOn(api.autoUpload, 'status').mockResolvedValue(
      automaticUploadStatus(),
    );
    vi.spyOn(api.autoUpload, 'enable')
      .mockRejectedValueOnce(authorizationRequired())
      .mockRejectedValueOnce(new ApiError(409, 'Scope changed', {
        code: 'authorization_required',
      }));
    vi.spyOn(api.shares, 'upload').mockResolvedValue({
      ok: true,
      shared_at: '2026-07-25T00:00:00Z',
      receipt_id: 'receipt-2',
      hosted_status: 'accepted',
      session_count: 1,
      bundle_hash: 'bundle-hash',
      redaction_summary: { total_redactions: 0, by_type: {} },
    });
    const { onSubmitted, toast } = renderSubmit();

    const automaticUpload = await screen.findByLabelText(
      'Enable automatic uploads after this share',
    );
    expect(automaticUpload).toBeChecked();
    fireEvent.click(screen.getByText('I accept the displayed consent and data-use terms.'));
    fireEvent.click(screen.getByText(/I certify this bundle and future automatically uploaded bundles/));
    fireEvent.click(screen.getByRole('button', {
      name: 'Submit and enable automatic uploads',
    }));

    await waitFor(() => {
      expect(onSubmitted).toHaveBeenCalledWith(
        'receipt-2',
        'accepted',
        null,
      );
    });
    expect(toast).toHaveBeenCalledWith(
      'Submitted. Automatic upload still needs review on the receipt page.',
      'info',
    );
  });
});
