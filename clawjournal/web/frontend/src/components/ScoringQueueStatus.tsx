import { useCallback, useEffect, useState } from 'react';
import { api } from '../api.ts';
import type { ScoringQueueStatus as QueueStatus } from '../types.ts';
import { colors, btnSecondary } from '../theme.ts';
import { useToast } from './Toast.tsx';

const POLL_INTERVAL_MS = 5_000;

const ACTION_REQUIRED_COPY: Record<string, string> = {
  backend_missing: 'Choose and confirm an AI scoring backend to continue.',
  backend_auth: 'The scoring backend needs you to sign in again.',
  backend_unavailable: 'The scoring backend is unavailable. Retry after fixing it.',
};

const STAGE_COPY: Record<string, string> = {
  queued: 'Queued',
  preparing: 'Preparing trace',
  locating_evidence: 'Locating evidence',
  final_scoring: 'Final scoring',
  persisting: 'Saving score',
};

interface Props {
  compact?: boolean;
  controls?: boolean;
}

export function ScoringQueueStatus({ compact = false, controls = false }: Props) {
  const { toast } = useToast();
  const [status, setStatus] = useState<QueueStatus | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setStatus(await api.scoringStatus());
      setUnavailable(false);
    } catch {
      // Queue observability must never block Inbox or Settings. Keep the last
      // successful snapshot if one exists and expose only a fixed local error.
      setUnavailable(true);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [refresh]);

  async function control(action: 'pause' | 'resume' | 'retry_failed') {
    if (busy) return;
    setBusy(true);
    try {
      const result = await api.scoringControl(action);
      setStatus(result.status);
      setUnavailable(false);
      if (action === 'retry_failed') {
        toast(`${result.retried ?? 0} failed scoring job${result.retried === 1 ? '' : 's'} queued`, 'success');
      } else {
        toast(action === 'pause' ? 'Background scoring paused' : 'Background scoring resumed', 'success');
      }
    } catch (error) {
      toast(error instanceof Error ? error.message : 'Could not update background scoring', 'error');
    } finally {
      setBusy(false);
    }
  }

  if (!status) {
    if (!controls || !unavailable) return null;
    return <p role="status" style={{ margin: 0, fontSize: 12.5, color: colors.gray500 }}>Scoring queue status is temporarily unavailable.</p>;
  }

  const counts = status.counts;
  const total = counts.pending + counts.running + counts.retry_wait + counts.succeeded + counts.failed;
  if (compact && total === 0 && status.worker_state === 'idle' && status.enabled) return null;

  const current = status.current;
  const progress = current?.progress_current != null && current.progress_total != null
    ? ` ${current.progress_current}/${current.progress_total}`
    : '';
  const actionRequired = status.action_required_code
    ? ACTION_REQUIRED_COPY[status.action_required_code] ?? 'The scoring backend needs attention.'
    : null;

  return (
    <div
      aria-live="polite"
      style={{
        display: 'flex', alignItems: compact ? 'center' : 'flex-start', gap: 10,
        flexWrap: 'wrap', padding: compact ? '7px 10px' : 0,
        marginBottom: compact ? 8 : 0,
        border: compact ? `1px solid ${colors.gray200}` : undefined,
        borderRadius: compact ? 8 : undefined,
        background: compact ? colors.gray50 : undefined,
        color: colors.gray700, fontSize: 12.5,
      }}
    >
      <strong style={{ color: colors.gray800 }}>AI scoring</strong>
      <span>
        {counts.pending} pending · {counts.running} scoring · {counts.retry_wait} retrying ·{' '}
        {counts.failed} failed · {counts.succeeded} completed
      </span>
      {current && (
        <span style={{ color: colors.primary500, fontWeight: 600 }}>
          {STAGE_COPY[current.stage] ?? 'Working'}{progress}
        </span>
      )}
      {!status.enabled && <span style={{ color: colors.gray500 }}>Paused</span>}
      {status.worker_state === 'cooldown' && <span style={{ color: colors.yellow700 }}>Backend cooldown</span>}
      {actionRequired && <span role="alert" style={{ color: colors.red700 }}>{actionRequired}</span>}
      {unavailable && <span style={{ color: colors.gray500 }}>Status refresh unavailable</span>}
      {controls && (
        <div style={{ display: 'flex', gap: 8, marginLeft: compact ? 'auto' : 0 }}>
          <button
            type="button"
            disabled={busy}
            onClick={() => void control(status.enabled ? 'pause' : 'resume')}
            style={{ ...btnSecondary, padding: '4px 9px', fontSize: 12 }}
          >
            {status.enabled ? 'Pause' : 'Resume'}
          </button>
          {counts.failed > 0 && (
            <button
              type="button"
              disabled={busy}
              onClick={() => void control('retry_failed')}
              style={{ ...btnSecondary, padding: '4px 9px', fontSize: 12 }}
            >
              Retry failed
            </button>
          )}
        </div>
      )}
    </div>
  );
}
