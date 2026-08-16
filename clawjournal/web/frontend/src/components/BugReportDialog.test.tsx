import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from '../api.ts';
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

    expect(screen.getByText(/does not capture a screenshot, upload the draft, or submit an issue/i)).toBeInTheDocument();
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
