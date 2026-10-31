import { fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from '../../api.ts';
import { AttachmentsPanel } from './AttachmentsPanel.tsx';
import type { RedactedSessionData } from './types.ts';

const data: RedactedSessionData = {
  loading: false, messages: [], selectedAttachmentIds: [],
  attachments: [
    { id: 'text', message_index: 2, name: 'results.txt', availability: 'extracted_text', can_include: true, reason: '', original_text: 'contact alice@example.test; loss=0.125' },
    { id: 'image', message_index: 3, name: 'figure.png', availability: 'reference_only', can_include: false, reason: 'The export contains no attachment contents.', original_text: '' },
  ],
};

describe('attachment choices', () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });
  it('starts unchecked, distinguishes references and sends only the selected ID', () => {
    const onChange = vi.fn();
    render(<AttachmentsPanel sessionId="session-test" title="Experiment" data={data} onChange={onChange} />);
    const boxes = screen.getAllByRole('checkbox');
    expect(boxes[0]).not.toBeChecked();
    expect(boxes[1]).not.toBeChecked();
    expect(boxes[1]).toBeDisabled();
    fireEvent.click(boxes[0]);
    expect(onChange).toHaveBeenCalledWith(['text']);
    expect(screen.getByText(/Message 3/)).toBeInTheDocument();
  });

  it('shows local original separately from scanned content and supports deselection', () => {
    const onChange = vi.fn();
    render(<AttachmentsPanel sessionId="session-test" title="Experiment" data={{ ...data, selectedAttachmentIds: ['text'], messages: [{
      role: 'user', content: '', extra: { attachment_review: { schema_version: 'clawjournal.attachments.v1', items: [{
        attachment_id: 'attachment-1', message_index: 2, availability: 'extracted_text', status: 'included', text: 'contact [REDACTED_EMAIL]; loss=0.125', name: 'results.txt',
      }] } },
    }] }} onChange={onChange} />);
    fireEvent.click(screen.getByRole('button', { name: 'Preview results.txt' }));
    expect(screen.getByText('contact alice@example.test; loss=0.125')).toBeInTheDocument();
    expect(screen.getByText('contact [REDACTED_EMAIL]; loss=0.125')).toBeInTheDocument();
    fireEvent.click(screen.getAllByRole('checkbox')[0]);
    expect(onChange).toHaveBeenCalledWith([]);
  });

  it('prevents another change during scanning', () => {
    render(<AttachmentsPanel sessionId="session-test" title="Experiment" data={{ ...data, loading: true }} onChange={vi.fn()} />);
    expect(screen.getAllByRole('checkbox')[0]).toBeDisabled();
    expect(screen.getByRole('status')).toHaveTextContent('Scanning');
  });

  it('discloses missing visual context when a file provides only text', () => {
    render(<AttachmentsPanel sessionId="session-test" title="Experiment" data={{ ...data,
      attachments: [{ ...data.attachments![0], name: 'paper.pdf', visual_context_missing: true }],
    }} onChange={vi.fn()} />);
    expect(screen.getByText(/Missing figures or layout will be flagged/)).toBeInTheDocument();
  });

  it('loads an image only when opened and revokes the local URL when closed', async () => {
    const fetchPreview = vi.spyOn(api.sessions, 'attachmentPreview').mockResolvedValue(new Blob(['pixels'], { type: 'image/png' }));
    const create = vi.fn(() => 'blob:local-preview');
    const revoke = vi.fn();
    vi.stubGlobal('URL', class extends URL {
      static createObjectURL = create;
      static revokeObjectURL = revoke;
    });
    const onChange = vi.fn();
    render(<AttachmentsPanel sessionId="session-test" title="Experiment" data={{ ...data, reviewedRevision: 'revision-one',
      attachments: [{ ...data.attachments![1], availability: 'original_file', can_preview_image: true }],
    }} onChange={onChange} />);
    expect(fetchPreview).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Preview figure.png' }));
    const preview = await screen.findByRole('img', { name: 'Local preview of figure.png' });
    expect(preview).toHaveAttribute('src', 'blob:local-preview');
    expect(fetchPreview).toHaveBeenCalledWith('session-test', 'image', 'revision-one', expect.any(AbortSignal));
    expect(screen.getByRole('checkbox')).toBeDisabled();
    expect(screen.getByRole('checkbox')).not.toBeChecked();
    expect(onChange).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Hide contents' }));
    expect(revoke).toHaveBeenCalledWith('blob:local-preview');
  });

  it('shows a failed image preview without changing sharing consent', async () => {
    vi.spyOn(api.sessions, 'attachmentPreview').mockRejectedValue(new Error('The conversation changed. Refresh the attachment list.'));
    const onChange = vi.fn();
    render(<AttachmentsPanel sessionId="session-test" title="Experiment" data={{ ...data, reviewedRevision: 'revision-one',
      attachments: [{ ...data.attachments![1], availability: 'original_file', can_preview_image: true }],
    }} onChange={onChange} />);
    fireEvent.click(screen.getByRole('button', { name: 'Preview figure.png' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('The conversation changed');
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
    expect(onChange).not.toHaveBeenCalled();
  });
});
