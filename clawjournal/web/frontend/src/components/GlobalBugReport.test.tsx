import { fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api } from '../api.ts';
import { IndexRecoveryScreen } from './IndexRecoveryScreen.tsx';
import { GlobalBugReport } from './GlobalBugReport.tsx';

afterEach(() => {
  vi.restoreAllMocks();
});

describe('GlobalBugReport', () => {
  it('keeps a non-overlapping global launcher on the normal workbench', () => {
    render(<GlobalBugReport><main>Normal workbench</main></GlobalBugReport>);
    expect(screen.getByText('Normal workbench')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Report a problem' })).toHaveStyle({ bottom: '72px' });
  });

  it.each([
    ['recovery_required' as const, 'Your local index needs repair'],
    ['unavailable' as const, 'The local index is unavailable'],
  ])('keeps the launcher available while index state is %s', (status, heading) => {
    render(
      <GlobalBugReport>
        <IndexRecoveryScreen
          health={{ status, message: 'The local index cannot serve workbench views.' }}
          onHealthChange={() => {}}
        />
      </GlobalBugReport>,
    );
    expect(screen.getByRole('heading', { name: heading })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Report a problem' })).toBeInTheDocument();
  });

  it('survives a React render crash without adding the raw error to the draft', async () => {
    const canary = 'render failed at /home/alice/private-session?token=secret';
    vi.spyOn(console, 'error').mockImplementation(() => {});
    vi.spyOn(api.support, 'context').mockRejectedValue(new TypeError('daemon unavailable'));
    function Crasher(): never {
      throw new Error(canary);
    }

    render(<GlobalBugReport><Crasher /></GlobalBugReport>);
    expect(await screen.findByRole('heading', { name: 'Something went wrong' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Report a problem' }));
    fireEvent.change(screen.getByLabelText(/Summary/), { target: { value: 'Unexpected UI failure' } });
    fireEvent.change(screen.getByLabelText(/What happened/), { target: { value: 'The page stopped rendering.' } });
    fireEvent.click(screen.getByRole('button', { name: 'Review draft' }));

    const draft = screen.getByLabelText(/Review and edit the exact Markdown/) as HTMLTextAreaElement;
    expect(draft.value).toContain('- Surface: UI render error');
    expect(draft.value).not.toContain(canary);
    expect(draft.value).not.toContain('/home/alice');
  });
});
