import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { api } from '../../api.ts';
import type { VisualReview } from '../../types.ts';
import { VisualAttachmentReview } from './VisualAttachmentReview.tsx';

const initial: VisualReview = {
  review_id: 'draft', attachment_id: 'attachment', reviewed_revision: 'revision', sharing_authorized: false,
  pages: [{ index: 0, width: 400, height: 200, status: 'unscanned', version: 'original', candidates: [], selected_candidate_ids: [], manual_boxes: [], warnings: [], redacted_text: '' }],
};
const scanned: VisualReview = { ...initial, pages: [{ ...initial.pages[0], status: 'scanned', version: 'scanned',
  candidates: [{ id: 0, text: 'alice@audit.test', start: 0, end: 16 }], selected_candidate_ids: [0],
  warnings: ['Review every page visually.'], redacted_text: '[REDACTED] loss=0.125',
}] };

describe('visual attachment review', () => {
  beforeEach(() => {
    vi.spyOn(api.sessions, 'visualPage').mockResolvedValue(new Blob(['pixels'], { type: 'image/png' }));
    vi.stubGlobal('URL', class extends URL {
      static createObjectURL = vi.fn(() => 'blob:local-page');
      static revokeObjectURL = vi.fn();
    });
  });
  afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

  it('only renders on request, requires scanning, and sends version-bound corrections', async () => {
    const process = vi.spyOn(api.sessions, 'visualReview').mockResolvedValueOnce(initial).mockResolvedValueOnce(scanned)
      .mockResolvedValueOnce({ ...scanned, pages: [{ ...scanned.pages[0], version: 'corrected', selected_candidate_ids: [] }] });
    const { unmount } = render(<VisualAttachmentReview sessionId="session" attachmentId="attachment" revision="revision" />);
    expect(process).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Start page review' }));
    expect(await screen.findByRole('img', { name: 'Original attachment page 1' })).toBeInTheDocument();
    expect(screen.queryByRole('img', { name: 'Redacted attachment page 1' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Scan this page' }));
    expect(await screen.findByRole('img', { name: 'Redacted attachment page 1' })).toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: 'Hide “alice@audit.test”' })).toBeChecked();
    fireEvent.click(screen.getByRole('checkbox', { name: 'Hide “alice@audit.test”' }));
    await waitFor(() => expect(process).toHaveBeenLastCalledWith('session', expect.objectContaining({
      action: 'update', review_id: 'draft', version: 'scanned', selected_candidate_ids: [], expected_revision: 'revision',
    }), expect.any(AbortSignal)));
    expect(screen.getByText(/Nothing is selected automatically/)).toBeInTheDocument();
    unmount();
    expect(URL.revokeObjectURL).toHaveBeenCalled();
  });

  it('converts a drawn screen rectangle to source pixels and supports undo', async () => {
    const process = vi.spyOn(api.sessions, 'visualReview').mockResolvedValueOnce(scanned)
      .mockResolvedValueOnce({ ...scanned, pages: [{ ...scanned.pages[0], version: 'manual', manual_boxes: [[20, 20, 100, 60]] }] })
      .mockResolvedValueOnce(scanned);
    vi.stubGlobal('PointerEvent', MouseEvent);
    render(<VisualAttachmentReview sessionId="session" attachmentId="attachment" revision="revision" />);
    fireEvent.click(screen.getByRole('button', { name: 'Start page review' }));
    const original = await screen.findByRole('img', { name: 'Original attachment page 1' });
    original.setPointerCapture = vi.fn();
    vi.spyOn(original, 'getBoundingClientRect').mockReturnValue({ x: 0, y: 0, left: 0, top: 0, right: 200, bottom: 100, width: 200, height: 100, toJSON() {} });
    fireEvent.click(screen.getByRole('button', { name: 'Hide an area' }));
    fireEvent.pointerDown(original, { clientX: 10, clientY: 10 });
    fireEvent.pointerUp(original, { clientX: 50, clientY: 30 });
    await waitFor(() => expect(process).toHaveBeenLastCalledWith('session', expect.objectContaining({ manual_boxes: [[20, 20, 100, 60]], version: 'scanned' }), expect.any(AbortSignal)));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Undo last hidden area' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: 'Undo last hidden area' }));
    await waitFor(() => expect(process).toHaveBeenLastCalledWith('session', expect.objectContaining({ manual_boxes: [], version: 'manual' }), expect.any(AbortSignal)));
  });

  it('reports scanner failure and keeps the page unscanned', async () => {
    vi.spyOn(api.sessions, 'visualReview').mockResolvedValueOnce(initial).mockRejectedValueOnce(new Error('Local text recognition is unavailable.'));
    render(<VisualAttachmentReview sessionId="session" attachmentId="attachment" revision="revision" />);
    fireEvent.click(screen.getByRole('button', { name: 'Start page review' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Scan this page' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Local text recognition is unavailable.');
    expect(screen.queryByRole('img', { name: 'Redacted attachment page 1' })).not.toBeInTheDocument();
    expect(screen.getByText(/0 pages scanned/)).toBeInTheDocument();
  });

  it('can restart after a failed page-image request without retaining the old error', async () => {
    vi.spyOn(api.sessions, 'visualReview').mockResolvedValueOnce(initial).mockResolvedValueOnce({ ...initial, review_id: 'new-draft' });
    vi.mocked(api.sessions.visualPage).mockRejectedValueOnce(new Error('The page expired.'));
    render(<VisualAttachmentReview sessionId="session" attachmentId="attachment" revision="revision" />);
    fireEvent.click(screen.getByRole('button', { name: 'Start page review' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('The page expired.');
    fireEvent.click(screen.getByRole('button', { name: 'Start page review again' }));
    expect(await screen.findByRole('img', { name: 'Original attachment page 1' })).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('starts the next PDF page unscanned instead of carrying previous page decisions', async () => {
    vi.spyOn(api.sessions, 'visualReview').mockResolvedValueOnce({ ...scanned, pages: [scanned.pages[0], { ...initial.pages[0], index: 1 }] });
    render(<VisualAttachmentReview sessionId="session" attachmentId="attachment" revision="revision" />);
    fireEvent.click(screen.getByRole('button', { name: 'Start page review' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Next page' }));
    expect(await screen.findByRole('img', { name: 'Original attachment page 2' })).toBeInTheDocument();
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Scan this page' })).toBeEnabled();
  });

  it('requires displayed final versions and inspection of every page before including the attachment', async () => {
    const twoPages: VisualReview = { ...scanned, pages: [scanned.pages[0], { ...scanned.pages[0], index: 1 }] };
    const checked: VisualReview = { ...twoPages, privacy_checked: true, redacted_name: 'results.pdf',
      pages: twoPages.pages.map(p => ({ ...p, version: `checked-${p.index}` })) };
    const process = vi.spyOn(api.sessions, 'visualReview').mockResolvedValueOnce(twoPages)
      .mockResolvedValueOnce(checked).mockResolvedValueOnce({ ...checked, approved: true });
    const onApproved = vi.fn();
    render(<VisualAttachmentReview sessionId="session" attachmentId="attachment" revision="revision" onApproved={onApproved} />);
    fireEvent.click(screen.getByRole('button', { name: 'Start page review' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Run full privacy check' }));
    const first = await screen.findByRole('checkbox', { name: /I inspected the final redacted page 1/ });
    expect(first).toBeDisabled();
    const approve = screen.getByRole('button', { name: 'Approve and include attachment' });
    expect(approve).toBeDisabled();
    fireEvent.load(await screen.findByRole('img', { name: 'Redacted attachment page 1' }));
    fireEvent.click(first);
    expect(approve).toBeDisabled();
    expect(onApproved).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Next page' }));
    const second = screen.getByRole('checkbox', { name: /I inspected the final redacted page 2/ });
    expect(second).toBeDisabled();
    fireEvent.load(await screen.findByRole('img', { name: 'Redacted attachment page 2' }));
    fireEvent.click(second);
    expect(approve).toBeEnabled();
    fireEvent.click(approve);
    await waitFor(() => expect(onApproved).toHaveBeenCalledWith('draft'));
    expect(process).toHaveBeenLastCalledWith('session', expect.objectContaining({
      action: 'approve', reviewed_pages: [0, 1], versions: ['checked-0', 'checked-1'],
    }), expect.any(AbortSignal));
  });
});
