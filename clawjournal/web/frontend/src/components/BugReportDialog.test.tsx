import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError, api } from '../api.ts';
import { BUG_REPORT_CONTEXT_TIMEOUT_MS, BUG_REPORT_FILENAME, BUG_REPORT_URL } from '../bugReportDraft.ts';
import { BugReportDialog } from './BugReportDialog.tsx';

const originalCreateObjectURL = Object.getOwnPropertyDescriptor(URL, 'createObjectURL');
const originalRevokeObjectURL = Object.getOwnPropertyDescriptor(URL, 'revokeObjectURL');

function restoreUrlMethod(name: 'createObjectURL' | 'revokeObjectURL', descriptor?: PropertyDescriptor) {
  if (descriptor) Object.defineProperty(URL, name, descriptor);
  else delete (URL as unknown as Record<string, unknown>)[name];
}

const supportContext = {
  support_context_schema_version: 1,
  kind: 'workbench',
  package: { version: '0.2.0', revision: 'abcdef123456' },
  runtime: {
    python_version: '3.13.7',
    sqlite_version: '3.49.1',
    os_family: 'Linux',
    os_release: '6.8.0',
    architecture: 'x86_64',
  },
  schema: { expected_user_version: 12 },
  storage: { filesystem_type: 'ext4', storage_risk: 'local', storage_migration_required: false },
  index: { status: 'ready', condition: null },
  collection: { status: 'complete', unavailable_sections: [] },
};

const unavailableCapability = {
  available: false,
  purpose: '',
  terms_version: '',
  retention_policy_version: '',
  terms_text: '',
  retention_text: '',
  max_report_bytes: 0,
  message: 'Private support is unavailable.',
};

const privateCapability = {
  available: true,
  purpose: 'Troubleshoot and improve ClawJournal. Reports are not used for research or model training.',
  terms_version: 'support-2026-08-16',
  retention_policy_version: 'retention-30d-v1',
  terms_text: 'Private support maintainers may inspect this report only for product support.',
  retention_text: 'The report is retained for up to 30 days unless you delete it sooner.',
  max_report_bytes: 32_768,
  message: null,
};

const clientReportId = '123e4567-e89b-42d3-a456-426614174000';

function reportStatus(state: 'queued' | 'submitting' | 'ambiguous' | 'accepted' | 'rejected') {
  return {
    client_report_id: clientReportId,
    state,
    receipt_id: state === 'accepted' ? 'support-receipt-123' : null,
    message: null,
    created_at: '2026-08-16T00:00:00Z',
    expires_at: state === 'accepted' ? '2026-09-15T00:00:00Z' : null,
  };
}

function fillRequiredFields() {
  fireEvent.change(screen.getByLabelText(/Summary/), { target: { value: 'Workbench froze' } });
  fireEvent.change(screen.getByLabelText(/What happened/), {
    target: { value: 'I opened the review screen and the controls stopped responding.' },
  });
}

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  delete (navigator as unknown as { clipboard?: Clipboard }).clipboard;
  delete (navigator as unknown as { mediaDevices?: MediaDevices }).mediaDevices;
  restoreUrlMethod('createObjectURL', originalCreateObjectURL);
  restoreUrlMethod('revokeObjectURL', originalRevokeObjectURL);
});

beforeEach(() => {
  vi.spyOn(api.support, 'capability').mockResolvedValue(unavailableCapability);
  vi.spyOn(api.support, 'list').mockResolvedValue({ reports: [], truncated: false });
});

describe('BugReportDialog', () => {
  it('keeps collection local, enforces field bounds, and renders a reviewable draft', async () => {
    const getDisplayMedia = vi.fn();
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: { getDisplayMedia },
    });
    const localSet = vi.spyOn(window.localStorage, 'setItem');
    const sessionSet = vi.spyOn(window.sessionStorage, 'setItem');
    vi.spyOn(api.support, 'context').mockResolvedValue({
      ...supportContext,
      raw_url: 'https://localhost/session/private?token=secret',
      stack: 'at /home/alice/private.ts:1',
    });
    render(
      <BugReportDialog
        open
        onClose={() => {}}
        surface="workbench"
        location={{ pathname: '/session/private-session-id', search: '?token=secret' }}
      />,
    );

    expect(screen.getByText(/does not capture screenshots, logs, traces, or hidden diagnostics/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/Summary/)).toHaveAttribute('maxlength', '120');
    expect(screen.getByLabelText(/What happened/)).toHaveAttribute('maxlength', '4000');
    expect(screen.getByLabelText(/Expected behavior/)).toHaveAttribute('maxlength', '2000');
    await screen.findByText('Privacy-bounded local diagnostics are ready for review.');

    fillRequiredFields();
    fireEvent.click(screen.getByRole('button', { name: 'Review draft' }));
    const draft = screen.getByLabelText(/Review and edit the exact Markdown/) as HTMLTextAreaElement;
    expect(draft.value).toContain('- Route: `/session/:id`');
    expect(draft.value).toContain('### Browser host');
    expect(draft.value).toContain('### ClawJournal daemon runtime');
    expect(draft.value).not.toContain('private-session-id');
    expect(draft.value).not.toContain('token=secret');
    expect(draft.value).not.toContain('/home/alice');
    expect(draft).toHaveAttribute('maxlength', '20000');
    expect(getDisplayMedia).not.toHaveBeenCalled();
    expect(localSet).not.toHaveBeenCalled();
    expect(sessionSet).not.toHaveBeenCalled();
  });

  it('continues without diagnostics when the daemon is down', async () => {
    vi.spyOn(api.support, 'context').mockRejectedValue(new TypeError('private daemon error /home/alice'));
    render(<BugReportDialog open onClose={() => {}} surface="workbench" />);

    expect(await screen.findByText(/Local diagnostics are unavailable/)).toBeInTheDocument();
    fillRequiredFields();
    fireEvent.click(screen.getByRole('button', { name: 'Review draft' }));
    const draft = screen.getByLabelText(/Review and edit the exact Markdown/) as HTMLTextAreaElement;
    expect(draft.value).toContain('Daemon support context unavailable.');
    expect(draft.value).not.toContain('private daemon error');
    expect(screen.getByRole('button', { name: 'Copy draft' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Download .md' })).toBeEnabled();
  });

  it('times out a stuck local context request without trapping the reporter', async () => {
    vi.useFakeTimers();
    vi.spyOn(api.support, 'context').mockImplementation(signal => new Promise((_resolve, reject) => {
      signal?.addEventListener('abort', () => reject(new DOMException('private timeout detail', 'AbortError')));
    }));
    render(<BugReportDialog open onClose={() => {}} surface="workbench" />);

    await act(async () => {
      vi.advanceTimersByTime(BUG_REPORT_CONTEXT_TIMEOUT_MS);
      await Promise.resolve();
    });
    expect(screen.getByText(/Local diagnostics are unavailable/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Close bug report' })).toBeEnabled();
  });

  it('copies the exact edited draft and falls back to selecting it on denial', async () => {
    vi.spyOn(api.support, 'context').mockResolvedValue(supportContext);
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    render(<BugReportDialog open onClose={() => {}} surface="workbench" />);
    await screen.findByText('Privacy-bounded local diagnostics are ready for review.');
    fillRequiredFields();
    fireEvent.click(screen.getByRole('button', { name: 'Review draft' }));
    const draft = screen.getByLabelText(/Review and edit the exact Markdown/) as HTMLTextAreaElement;
    fireEvent.change(draft, { target: { value: 'reviewed exact markdown' } });
    fireEvent.click(screen.getByRole('button', { name: 'Copy draft' }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('reviewed exact markdown'));

    writeText.mockRejectedValueOnce(new DOMException('denied', 'NotAllowedError'));
    fireEvent.click(screen.getByRole('button', { name: 'Copy draft' }));
    expect(await screen.findByText(/press Ctrl\+C or Command\+C/)).toBeInTheDocument();
    expect(draft).toHaveFocus();
    expect(draft.selectionStart).toBe(0);
    expect(draft.selectionEnd).toBe(draft.value.length);
  });

  it('downloads the exact edited Markdown with a fixed name and opens only a blank issue URL', async () => {
    vi.spyOn(api.support, 'context').mockResolvedValue(supportContext);
    let downloadedBlob: Blob | null = null;
    const createObjectURL = vi.fn((blob: Blob) => {
      downloadedBlob = blob;
      return 'blob:test-report';
    });
    const revokeObjectURL = vi.fn();
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: createObjectURL });
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: revokeObjectURL });
    let downloadedName = '';
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      downloadedName = this.download;
    });
    const { rerender } = render(
      <BugReportDialog open onClose={() => {}} surface="workbench" />,
    );
    await screen.findByText('Privacy-bounded local diagnostics are ready for review.');
    fillRequiredFields();
    fireEvent.click(screen.getByRole('button', { name: 'Review draft' }));
    const draft = screen.getByLabelText(/Review and edit the exact Markdown/);
    fireEvent.change(draft, { target: { value: 'final local draft' } });

    fireEvent.click(screen.getByRole('button', { name: 'Download .md' }));
    expect(createObjectURL).toHaveBeenCalledOnce();
    expect(downloadedName).toBe(BUG_REPORT_FILENAME);
    expect(click).toHaveBeenCalledOnce();
    expect(downloadedBlob).toBeInstanceOf(Blob);
    const downloadedText = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result));
      reader.onerror = () => reject(reader.error);
      reader.readAsText(downloadedBlob as Blob);
    });
    expect(downloadedText).toBe('final local draft');

    const github = screen.getByRole('link', { name: 'Open blank GitHub issue' });
    expect(github).toHaveAttribute('href', BUG_REPORT_URL);
    expect(github.getAttribute('href')).not.toContain('?');
    expect(github).toHaveAttribute('rel', 'noopener noreferrer');

    rerender(<BugReportDialog open={false} onClose={() => {}} surface="workbench" />);
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:test-report');
  });

  it('shows private submission only for a validated available capability and requires separate consent', async () => {
    vi.spyOn(api.support, 'context').mockResolvedValue(supportContext);
    vi.mocked(api.support.capability).mockResolvedValue(privateCapability);
    const submit = vi.spyOn(api.support, 'submit').mockResolvedValue(reportStatus('queued'));
    render(<BugReportDialog open onClose={() => {}} surface="workbench" />);

    await waitFor(() => expect(api.support.capability).toHaveBeenCalled());
    fillRequiredFields();
    fireEvent.click(screen.getByRole('button', { name: 'Review draft' }));
    expect(await screen.findByText(privateCapability.purpose)).toBeInTheDocument();
    expect(screen.getByText(privateCapability.retention_text)).toBeInTheDocument();

    const draft = screen.getByLabelText(/Review and edit the exact Markdown/) as HTMLTextAreaElement;
    expect(draft).toHaveFocus();
    fireEvent.change(draft, { target: { value: '# Reviewed\n\nExact private report.' } });
    const send = screen.getByRole('button', { name: 'Send privately' });
    expect(send).toBeDisabled();
    expect(submit).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('checkbox', { name: /I reviewed the exact Markdown above/i }));
    expect(send).toBeEnabled();
    fireEvent.click(send);

    expect(await screen.findByRole('heading', { name: 'Private report: Queued' })).toHaveFocus();
    expect(submit).toHaveBeenCalledWith({
      report_markdown: '# Reviewed\n\nExact private report.',
      accepted_terms_version: privateCapability.terms_version,
      accepted_retention_policy_version: privateCapability.retention_policy_version,
    });
    expect(draft).toHaveAttribute('readonly');
  });

  it('clears consent and refreshes capability when terms change at submission', async () => {
    vi.spyOn(api.support, 'context').mockResolvedValue(supportContext);
    const updatedCapability = {
      ...privateCapability,
      terms_version: 'support-2026-08-18',
      terms_text: 'Newest private support terms.',
    };
    vi.mocked(api.support.capability)
      .mockResolvedValueOnce(privateCapability)
      .mockResolvedValueOnce(updatedCapability);
    vi.spyOn(api.support, 'submit').mockRejectedValue(new ApiError(
      409,
      'terms changed',
      { code: 'terms_mismatch' },
    ));
    render(<BugReportDialog open onClose={() => {}} surface="workbench" />);

    await waitFor(() => expect(api.support.capability).toHaveBeenCalledTimes(1));
    fillRequiredFields();
    fireEvent.click(screen.getByRole('button', { name: 'Review draft' }));
    await screen.findByText(privateCapability.purpose);
    fireEvent.click(screen.getByRole('checkbox', { name: /I reviewed the exact Markdown above/i }));
    fireEvent.click(screen.getByRole('button', { name: 'Send privately' }));

    expect(await screen.findByText('Newest private support terms.')).toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: /support-2026-08-18/i })).not.toBeChecked();
    expect(screen.getByRole('button', { name: 'Send privately' })).toBeDisabled();
  });

  it('requires fresh consent after close and a support-terms change', async () => {
    vi.spyOn(api.support, 'context').mockResolvedValue(supportContext);
    vi.mocked(api.support.capability)
      .mockResolvedValueOnce(privateCapability)
      .mockResolvedValueOnce({
        ...privateCapability,
        terms_version: 'support-2026-08-17',
        retention_policy_version: 'retention-30d-v2',
        terms_text: 'Updated private support terms.',
      });
    const submit = vi.spyOn(api.support, 'submit');
    const { rerender } = render(
      <BugReportDialog open onClose={() => {}} surface="workbench" />,
    );

    await waitFor(() => expect(api.support.capability).toHaveBeenCalledTimes(1));
    fillRequiredFields();
    fireEvent.click(screen.getByRole('button', { name: 'Review draft' }));
    await screen.findByText(privateCapability.purpose);
    fireEvent.click(screen.getByRole('checkbox', { name: /I reviewed the exact Markdown above/i }));
    expect(screen.getByRole('button', { name: 'Send privately' })).toBeEnabled();

    rerender(<BugReportDialog open={false} onClose={() => {}} surface="workbench" />);
    rerender(<BugReportDialog open onClose={() => {}} surface="workbench" />);

    await waitFor(() => expect(api.support.capability).toHaveBeenCalledTimes(2));
    expect(await screen.findByText('Updated private support terms.')).toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: /support-2026-08-17.*retention-30d-v2/i })).not.toBeChecked();
    expect(screen.getByRole('button', { name: 'Send privately' })).toBeDisabled();
    expect(submit).not.toHaveBeenCalled();
  });

  it('restores recent private receipts after reopen and allows check and delete', async () => {
    vi.spyOn(api.support, 'context').mockResolvedValue(supportContext);
    const accepted = reportStatus('accepted');
    const queuedId = '223e4567-e89b-42d3-a456-426614174001';
    const queued = { ...reportStatus('queued'), client_report_id: queuedId };
    vi.mocked(api.support.list).mockResolvedValue({ reports: [accepted, queued], truncated: false });
    const status = vi.spyOn(api.support, 'status').mockResolvedValue({
      ...reportStatus('accepted'),
      client_report_id: queuedId,
      receipt_id: 'support-receipt-queued',
    });
    const remove = vi.spyOn(api.support, 'remove').mockResolvedValue({
      client_report_id: accepted.client_report_id,
      state: 'deleted',
    });

    render(<BugReportDialog open onClose={() => {}} surface="workbench" />);

    expect(await screen.findByRole('heading', { name: 'Recent private reports from this device' })).toBeInTheDocument();
    expect(screen.getByText(/support-receipt-123/)).toBeInTheDocument();
    expect(screen.getByLabelText(/Summary/)).toBeEnabled();

    fireEvent.click(screen.getByRole('button', { name: `Check status for report ${queuedId}` }));
    expect(await screen.findByText(/support-receipt-queued/)).toBeInTheDocument();
    expect(status).toHaveBeenCalledWith(queuedId);

    fireEvent.click(screen.getByRole('button', { name: `Delete report ${accepted.client_report_id}` }));
    fireEvent.click(screen.getByRole('button', { name: `Confirm delete report ${accepted.client_report_id}` }));
    await waitFor(() => expect(remove).toHaveBeenCalledWith(accepted.client_report_id));
    expect(screen.queryByText(/support-receipt-123/)).not.toBeInTheDocument();
    expect(await screen.findByText('The saved private report was deleted.')).toBeInTheDocument();
    expect(screen.getByLabelText(/Summary/)).toBeEnabled();
  });

  it('moves a submitted receipt to recent reports before composing another problem', async () => {
    vi.spyOn(api.support, 'context').mockResolvedValue(supportContext);
    vi.mocked(api.support.capability).mockResolvedValue(privateCapability);
    vi.spyOn(api.support, 'submit').mockResolvedValue(reportStatus('queued'));
    const remove = vi.spyOn(api.support, 'remove');
    render(<BugReportDialog open onClose={() => {}} surface="workbench" />);

    await waitFor(() => expect(api.support.capability).toHaveBeenCalled());
    fillRequiredFields();
    fireEvent.click(screen.getByRole('button', { name: 'Review draft' }));
    await screen.findByText(privateCapability.purpose);
    fireEvent.click(screen.getByRole('checkbox', { name: /I reviewed the exact Markdown above/i }));
    fireEvent.click(screen.getByRole('button', { name: 'Send privately' }));
    await screen.findByRole('heading', { name: 'Private report: Queued' });

    fireEvent.click(screen.getByRole('button', { name: 'Report another problem' }));

    expect(screen.queryByRole('heading', { name: /Private report:/ })).not.toBeInTheDocument();
    expect(await screen.findByRole('heading', { name: 'Recent private reports from this device' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: `Check status for report ${clientReportId}` })).toBeEnabled();
    expect(screen.getByLabelText(/Summary/)).toHaveValue('');
    expect(remove).not.toHaveBeenCalled();
  });

  it('renders real queue, send, confirmation, receipt, and deletion states through the daemon', async () => {
    vi.spyOn(api.support, 'context').mockResolvedValue(supportContext);
    vi.mocked(api.support.capability).mockResolvedValue(privateCapability);
    vi.spyOn(api.support, 'submit').mockResolvedValue(reportStatus('queued'));
    const status = vi.spyOn(api.support, 'status')
      .mockResolvedValueOnce(reportStatus('submitting'))
      .mockResolvedValueOnce(reportStatus('ambiguous'))
      .mockResolvedValueOnce(reportStatus('accepted'));
    const remove = vi.spyOn(api.support, 'remove').mockResolvedValue({
      client_report_id: clientReportId,
      state: 'deleted',
    });
    render(<BugReportDialog open onClose={() => {}} surface="workbench" />);

    await waitFor(() => expect(api.support.capability).toHaveBeenCalled());
    fillRequiredFields();
    fireEvent.click(screen.getByRole('button', { name: 'Review draft' }));
    await screen.findByText(privateCapability.purpose);
    fireEvent.click(screen.getByRole('checkbox', { name: /I reviewed the exact Markdown above/i }));
    fireEvent.click(screen.getByRole('button', { name: 'Send privately' }));
    await screen.findByRole('heading', { name: 'Private report: Queued' });

    fireEvent.click(screen.getByRole('button', { name: 'Check status' }));
    await screen.findByRole('heading', { name: 'Private report: Sending' });
    fireEvent.click(screen.getByRole('button', { name: 'Check status' }));
    await screen.findByRole('heading', { name: 'Private report: Confirming' });
    fireEvent.click(screen.getByRole('button', { name: 'Check status' }));
    await screen.findByRole('heading', { name: 'Private report: Received' });
    expect(screen.getByText('support-receipt-123')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Copy draft' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Download .md' })).toBeEnabled();
    expect(screen.getByRole('link', { name: 'Open blank GitHub issue' })).toHaveAttribute('href', BUG_REPORT_URL);
    expect(status).toHaveBeenCalledTimes(3);
    expect(status).toHaveBeenLastCalledWith(clientReportId);

    fireEvent.click(screen.getByRole('button', { name: 'Delete private report' }));
    expect(screen.getByRole('button', { name: 'Confirm delete' })).toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: 'Confirm delete' }));
    expect(await screen.findByText(/private report was deleted/i)).toBeInTheDocument();
    expect(remove).toHaveBeenCalledWith(clientReportId);
    expect(screen.queryByRole('heading', { name: /Private report:/ })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Send privately' })).toBeDisabled();
  });

  it('shows rejection without leaking an error and leaves every local fallback usable', async () => {
    const getDisplayMedia = vi.fn();
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: { getDisplayMedia },
    });
    const localSet = vi.spyOn(window.localStorage, 'setItem');
    const sessionSet = vi.spyOn(window.sessionStorage, 'setItem');
    vi.spyOn(api.support, 'context').mockResolvedValue(supportContext);
    vi.mocked(api.support.capability).mockResolvedValue(privateCapability);
    vi.spyOn(api.support, 'submit').mockResolvedValue({
      ...reportStatus('rejected'),
      message: 'The report was not accepted.',
    });
    render(<BugReportDialog open onClose={() => {}} surface="workbench" />);

    await waitFor(() => expect(api.support.capability).toHaveBeenCalled());
    fillRequiredFields();
    fireEvent.click(screen.getByRole('button', { name: 'Review draft' }));
    await screen.findByText(privateCapability.purpose);
    fireEvent.click(screen.getByRole('checkbox', { name: /I reviewed the exact Markdown above/i }));
    fireEvent.click(screen.getByRole('button', { name: 'Send privately' }));

    await screen.findByRole('heading', { name: 'Private report: Rejected' });
    expect(screen.getByRole('button', { name: 'Retry privately' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Copy draft' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Download .md' })).toBeEnabled();
    expect(screen.getByRole('link', { name: 'Open blank GitHub issue' })).toHaveAttribute('href', BUG_REPORT_URL);
    expect(getDisplayMedia).not.toHaveBeenCalled();
    expect(localSet).not.toHaveBeenCalled();
    expect(sessionSet).not.toHaveBeenCalled();
  });

  it('never shows Send privately when capability lookup is unavailable', async () => {
    vi.spyOn(api.support, 'context').mockResolvedValue(supportContext);
    vi.mocked(api.support.capability).mockRejectedValue(new TypeError('offline private detail'));
    const submit = vi.spyOn(api.support, 'submit');
    render(<BugReportDialog open onClose={() => {}} surface="workbench" />);

    fillRequiredFields();
    fireEvent.click(screen.getByRole('button', { name: 'Review draft' }));
    expect(await screen.findByText(/Private submission is unavailable right now/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Send privately' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Copy draft' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Download .md' })).toBeEnabled();
    expect(screen.getByRole('link', { name: 'Open blank GitHub issue' })).toBeInTheDocument();
    expect(submit).not.toHaveBeenCalled();
    expect(screen.queryByText(/offline private detail/)).not.toBeInTheDocument();
  });

  it('traps focus, restores it on Escape, and preserves textarea Enter', async () => {
    vi.spyOn(api.support, 'context').mockResolvedValue(supportContext);

    function Harness() {
      const [open, setOpen] = useState(false);
      return (
        <>
          <button type="button" onClick={() => setOpen(true)}>Open reporter</button>
          <BugReportDialog open={open} onClose={() => setOpen(false)} surface="workbench" />
        </>
      );
    }

    render(<Harness />);
    const opener = screen.getByRole('button', { name: 'Open reporter' });
    opener.focus();
    fireEvent.click(opener);
    const summary = screen.getByLabelText(/Summary/);
    expect(summary).toHaveFocus();

    const happened = screen.getByLabelText(/What happened/);
    expect(fireEvent.keyDown(happened, { key: 'Enter' })).toBe(true);
    fillRequiredFields();

    const close = screen.getByRole('button', { name: 'Close bug report' });
    close.focus();
    fireEvent.keyDown(close, { key: 'Tab', shiftKey: true });
    expect(screen.getByRole('button', { name: 'Review draft' })).toHaveFocus();

    fireEvent.keyDown(document.activeElement as HTMLElement, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });
});
