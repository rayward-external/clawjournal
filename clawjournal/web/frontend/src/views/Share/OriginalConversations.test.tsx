import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { cleanup } from '@testing-library/react';
import { api } from '../../api.ts';
import { OriginalConversations } from './OriginalConversations.tsx';

const offer = {enabled: true, consent_version: 'originals-v1', purpose: 'Only to improve redaction.', retention_days: 30};
const preview = {document: {schema: 'example', sessions: [{messages: ['invented@example.org']}]}, preview_digest: 'reviewed-digest'};
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it('starts unselected and reads originals only when the user chooses them', async () => {
  const read = vi.spyOn(api.shares, 'originals').mockResolvedValue(preview);
  const onChange = vi.fn(), onPending = vi.fn();
  render(<OriginalConversations shareId="share" offer={offer} disabled={false} onChange={onChange} onPending={onPending} />);
  expect(screen.getByRole('checkbox')).not.toBeChecked();
  expect(read).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('checkbox'));
  expect(onPending).toHaveBeenLastCalledWith(true);
  await waitFor(() => expect(onChange).toHaveBeenLastCalledWith('reviewed-digest'));
  expect(screen.getByText(/invented@example.org/)).toBeInTheDocument();
  expect(onPending).toHaveBeenLastCalledWith(false);
  fireEvent.click(screen.getByRole('checkbox'));
  expect(onChange).toHaveBeenLastCalledWith(null);
  expect(screen.queryByText(/invented@example.org/)).not.toBeInTheDocument();
});

it('keeps submission blocked after a missing-original error until consent is removed', async () => {
  vi.spyOn(api.shares, 'originals').mockRejectedValue(new Error('Original no longer exists'));
  const onChange = vi.fn(), onPending = vi.fn();
  render(<OriginalConversations shareId="share" offer={offer} disabled={false} onChange={onChange} onPending={onPending} />);
  fireEvent.click(screen.getByRole('checkbox'));
  expect(await screen.findByRole('alert')).toHaveTextContent('Original no longer exists');
  expect(onPending).toHaveBeenLastCalledWith(true);
  expect(onChange).toHaveBeenLastCalledWith(null);
  fireEvent.click(screen.getByRole('checkbox'));
  expect(onPending).toHaveBeenLastCalledWith(false);
});

it('ignores a late preview after the user removes consent', async () => {
  let resolve!: (value: typeof preview) => void;
  vi.spyOn(api.shares, 'originals').mockReturnValue(new Promise(done => { resolve = done; }));
  const onChange = vi.fn(), onPending = vi.fn();
  render(<OriginalConversations shareId="share" offer={offer} disabled={false} onChange={onChange} onPending={onPending} />);
  fireEvent.click(screen.getByRole('checkbox'));
  fireEvent.click(screen.getByRole('checkbox'));
  await act(async () => { resolve(preview); });
  expect(onChange).toHaveBeenLastCalledWith(null);
  expect(screen.queryByText(/invented@example.org/)).not.toBeInTheDocument();
});

it('explains when collection is unavailable and disables consent', () => {
  render(<OriginalConversations shareId="share" offer={{...offer, enabled: false}} disabled={false} onChange={vi.fn()} onPending={vi.fn()} />);
  expect(screen.getByRole('checkbox')).toBeDisabled();
  expect(screen.getByText(/not enabled on this server/)).toBeInTheDocument();
});

it('downloads only on request and uses the approved preview digest', async () => {
  vi.spyOn(api.shares, 'originals').mockResolvedValue(preview);
  const download = vi.spyOn(api.shares, 'downloadOriginals').mockResolvedValue();
  render(<OriginalConversations shareId="share" offer={offer} disabled={false} onChange={vi.fn()} onPending={vi.fn()} />);
  fireEvent.click(screen.getByRole('checkbox'));
  const button = await screen.findByRole('button', {name: /Download originals/});
  expect(download).not.toHaveBeenCalled();
  fireEvent.click(button);
  expect(download).toHaveBeenCalledWith('share', 'reviewed-digest');
});
