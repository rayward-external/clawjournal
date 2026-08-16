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
  it('shows a passive spinner while the startup integrity check is running', () => {
    render(
      <IndexRecoveryScreen
        health={{
          status: 'checking',
          message: 'Checking the local index before enabling database work...',
        }}
        onHealthChange={vi.fn()}
      />,
    );

    const status = screen.getByRole('status');
    expect(status).toHaveAttribute('aria-busy', 'true');
    expect(screen.getByText('Checking the local index before enabling database work...')).toBeInTheDocument();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

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

  it.each([
    { storage_risk: 'unknown' as const, filesystem_type: 'unknown' },
    { storage_risk: 'local' as const, filesystem_type: 'ext4' },
  ])('keeps recovery available for $storage_risk storage', storage => {
    render(
      <IndexRecoveryScreen
        health={recoveryRequired({
          ...storage,
          storage_migration_required: false,
        })}
        onHealthChange={vi.fn()}
      />,
    );

    expect(screen.getByRole('button', { name: 'Back up and rebuild index' })).toBeEnabled();
  });

  it('requires a complete home migration and withholds sensitive storage diagnostics', () => {
    const rebuild = vi.spyOn(api.index, 'rebuild');
    render(
      <IndexRecoveryScreen
        health={recoveryRequired({
          storage_risk: 'network',
          filesystem_type: 'nfs',
          storage_migration_required: true,
          database_path: '/network/users/alice/.clawjournal/index.db',
          backup_path: '/network/users/alice/.clawjournal/index-backups/recovery-1',
          detail: 'Mounted from server:/private/home.',
        })}
        onHealthChange={vi.fn()}
      />,
    );

    const alert = screen.getByRole('alert');
    expect(alert).toHaveAccessibleName('Copy ClawJournal state to local storage first');
    expect(screen.getByRole('list', { name: 'Required storage migration steps' })).toHaveTextContent(
      'Copy the entire CLAWJOURNAL_HOME directory',
    );
    expect(alert).toHaveTextContent('Keep the original unchanged until recovery succeeds');
    expect(alert).toHaveTextContent('Do not copy only the index database');
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
    expect(screen.queryByText(/network\/users\/alice|server:\/private\/home/i)).not.toBeInTheDocument();
    expect(rebuild).not.toHaveBeenCalled();
  });

  it('fails closed when an older backend reports network risk without the migration flag', () => {
    render(
      <IndexRecoveryScreen
        health={recoveryRequired({
          storage_risk: 'network',
          filesystem_type: 'nfs',
        })}
        onHealthChange={vi.fn()}
      />,
    );

    expect(screen.getByRole('heading', { name: 'Copy ClawJournal state to local storage first' })).toBeInTheDocument();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it('shows the same migration-only path for an unavailable healthy or missing index', () => {
    render(
      <IndexRecoveryScreen
        health={{
          status: 'unavailable',
          message: 'This raw backend message must stay hidden.',
          storage_risk: 'network',
          filesystem_type: 'nfs4',
          storage_migration_required: true,
          database_path: '/private/cluster/alice/index.db',
        }}
        onHealthChange={vi.fn()}
      />,
    );

    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent('will not open, create, or rebuild');
    expect(alert).toHaveTextContent('offer repair only if it is still needed');
    expect(alert).not.toHaveTextContent('raw backend message');
    expect(alert).not.toHaveTextContent('/private/cluster/alice');
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
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
