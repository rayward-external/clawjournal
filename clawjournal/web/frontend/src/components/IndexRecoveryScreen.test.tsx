import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { api, ApiError } from '../api.ts';
import type { IndexHealth, IndexRebuildResponse } from '../types.ts';
import { IndexRecoveryScreen } from './IndexRecoveryScreen.tsx';

function recoveryRequired(overrides: Partial<IndexHealth> = {}): IndexHealth {
  return {
    status: 'recovery_required',
    message: 'The workbench index is damaged and must be rebuilt.',
    automatic_recovery_available: true,
    database_path: 'D:/state/index.db',
    ...overrides,
  };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('IndexRecoveryScreen', () => {
  it('offers one explicit action without adding a second confirmation dialog', () => {
    render(
      <IndexRecoveryScreen
        health={recoveryRequired()}
        onHealthChange={vi.fn()}
      />,
    );

    expect(screen.getByRole('heading', { name: 'Your local index needs repair' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Back up and rebuild index' })).toBeEnabled();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.getByText(/Sharing and automatic uploads stay blocked/)).toBeInTheDocument();
  });

  it('submits once, disables the action, and accepts an asynchronous rebuilding response', async () => {
    let resolveRebuild!: (value: IndexRebuildResponse) => void;
    const rebuild = vi.spyOn(api.index, 'rebuild').mockImplementationOnce(() => (
      new Promise(resolve => { resolveRebuild = resolve; })
    ));
    const onHealthChange = vi.fn();
    render(
      <IndexRecoveryScreen
        health={recoveryRequired()}
        onHealthChange={onHealthChange}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Back up and rebuild index' }));
    const busyButton = screen.getByRole('button', { name: 'Starting safe rebuild...' });
    expect(busyButton).toBeDisabled();
    fireEvent.click(busyButton);
    expect(rebuild).toHaveBeenCalledTimes(1);

    const rebuilding: IndexHealth = {
      status: 'rebuilding',
      stage: 'queued',
      message: 'Starting the safe index recovery...',
    };
    await act(async () => {
      resolveRebuild({ ok: true, index_health: rebuilding });
    });

    expect(onHealthChange).toHaveBeenCalledWith(rebuilding);
  });

  it('also accepts a rebuild that completes in the initial response', async () => {
    const ready: IndexHealth = {
      status: 'ready',
      backup_path: 'D:/state/index-backups/recovery-2',
      restored_state_counts: { session_decisions: 2 },
      warnings: [],
    };
    vi.spyOn(api.index, 'rebuild').mockResolvedValueOnce({ ok: true, index_health: ready });
    const onHealthChange = vi.fn();
    render(
      <IndexRecoveryScreen
        health={recoveryRequired()}
        onHealthChange={onHealthChange}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Back up and rebuild index' }));

    await waitFor(() => expect(onHealthChange).toHaveBeenCalledWith(ready));
  });

  it('keeps a failed recovery retryable and adopts health returned with the error', async () => {
    const failedHealth = recoveryRequired({
      message: 'Recovery did not finish. The original backup is intact.',
      backup_path: 'D:/state/index-backups/recovery-1',
    });
    vi.spyOn(api.index, 'rebuild').mockRejectedValueOnce(new ApiError(
      500,
      'The source-log rescan did not complete cleanly.',
      { index_health: failedHealth },
    ));
    const onHealthChange = vi.fn();
    render(
      <IndexRecoveryScreen
        health={recoveryRequired()}
        onHealthChange={onHealthChange}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Back up and rebuild index' }));

    expect(await screen.findByText('The source-log rescan did not complete cleanly.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Back up and rebuild index' })).toBeEnabled();
    expect(onHealthChange).toHaveBeenCalledWith(failedHealth);
  });

  it('shows progress or safe diagnostics without exposing a rebuild action', async () => {
    const { rerender } = render(
      <IndexRecoveryScreen
        health={{ status: 'rebuilding', message: 'Restoring local decisions...' }}
        onHealthChange={vi.fn()}
      />,
    );

    expect(screen.getByRole('heading', { name: 'Rebuilding your local index' })).toBeInTheDocument();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();

    rerender(
      <IndexRecoveryScreen
        health={{
          status: 'unavailable',
          message: 'The state directory is not writable.',
          automatic_recovery_available: false,
        }}
        onHealthChange={vi.fn()}
      />,
    );

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'The local index is unavailable' })).toBeInTheDocument();
    });
    expect(screen.getByText('The state directory is not writable.')).toBeInTheDocument();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });
});
