import { useState } from 'react';
import { api, ApiError } from '../api.ts';
import type { IndexHealth } from '../types.ts';
import { colors, btnPrimary, fontFamily } from '../theme.ts';
import { Spinner } from './Spinner.tsx';

interface IndexRecoveryScreenProps {
  health: IndexHealth | null;
  probeError?: string | null;
  onHealthChange: (health: IndexHealth) => void;
}

const HEALTH_STATUSES = new Set([
  'ready',
  'checking',
  'recovery_required',
  'rebuilding',
  'unavailable',
]);

function healthFromError(error: unknown): IndexHealth | null {
  if (!(error instanceof ApiError)) return null;
  const value = error.body.index_health;
  if (
    !value
    || typeof value !== 'object'
    || !('status' in value)
    || typeof value.status !== 'string'
    || !HEALTH_STATUSES.has(value.status)
  ) {
    return null;
  }
  return value as IndexHealth;
}

function DiagnosticDetails({ health }: { health: IndexHealth }) {
  if (!health.detail && !health.database_path && !health.backup_path) return null;
  return (
    <details style={{ marginTop: 18, color: colors.gray500, fontSize: 12.5 }}>
      <summary style={{ cursor: 'pointer', fontWeight: 600 }}>Technical details</summary>
      <div style={{ marginTop: 8, lineHeight: 1.6, overflowWrap: 'anywhere' }}>
        {health.detail && <div>{health.detail}</div>}
        {health.database_path && <div>Index: <code>{health.database_path}</code></div>}
        {health.backup_path && <div>Backup: <code>{health.backup_path}</code></div>}
      </div>
    </details>
  );
}

export function IndexRecoveryScreen({
  health,
  probeError = null,
  onHealthChange,
}: IndexRecoveryScreenProps) {
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const startRecovery = async () => {
    if (submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const result = await api.index.rebuild();
      if (!result.index_health) {
        throw new Error('The workbench did not return recovery progress.');
      }
      onHealthChange(result.index_health);
    } catch (caught) {
      const nextHealth = healthFromError(caught);
      if (nextHealth) onHealthChange(nextHealth);
      setError(caught instanceof Error ? caught.message : 'Index recovery could not be started.');
    } finally {
      setSubmitting(false);
    }
  };

  let content: React.ReactNode;

  if (!health) {
    content = probeError ? (
      <div role="alert">
        <h1 style={{ margin: '0 0 8px', fontSize: 21, color: colors.gray900 }}>
          Cannot check the local index
        </h1>
        <p style={{ margin: 0, color: colors.red700, fontSize: 14, lineHeight: 1.55 }}>
          {probeError} ClawJournal will retry automatically.
        </p>
      </div>
    ) : (
      <Spinner text="Checking the local index..." />
    );
  } else if (health.status === 'checking') {
    content = (
      <div role="status" aria-live="polite" aria-busy="true">
        <Spinner text={health.message || 'Checking the local index...'} />
      </div>
    );
  } else if (health.status === 'rebuilding') {
    content = (
      <div role="status" aria-live="polite" aria-busy="true">
        <h1 style={{ margin: '0 0 6px', fontSize: 21, color: colors.gray900 }}>
          Rebuilding your local index
        </h1>
        <p style={{ margin: '0 0 2px', color: colors.gray600, fontSize: 14, lineHeight: 1.55 }}>
          ClawJournal keeps the original index in a backup while it rebuilds from your original session logs. Keep ClawJournal open until this finishes.
        </p>
        <Spinner text={health.message || 'Backing up and rebuilding...'} />
        <DiagnosticDetails health={health} />
      </div>
    );
  } else if (health.status === 'recovery_required') {
    const canRecover = health.automatic_recovery_available !== false;
    content = (
      <div role="alert">
        <h1 style={{ margin: '0 0 8px', fontSize: 21, color: colors.gray900 }}>
          Your local index needs repair
        </h1>
        <p style={{ margin: '0 0 12px', color: colors.gray600, fontSize: 14, lineHeight: 1.55 }}>
          {health.message || 'ClawJournal could not safely open its local index.'}
        </p>
        <p style={{ margin: '0 0 18px', color: colors.gray600, fontSize: 14, lineHeight: 1.55 }}>
          ClawJournal will first save the current database, then rebuild the index from your original session logs and restore every readable review and safety decision. Sharing and automatic uploads stay blocked until recovery finishes.
        </p>
        <p style={{ margin: '0 0 18px', color: colors.gray500, fontSize: 12.5, lineHeight: 1.55 }}>
          If this keeps happening on a shared or network home, stop ClawJournal, move the whole state directory to persistent local storage, and set <code>CLAWJOURNAL_HOME</code> to that directory before restarting.
        </p>
        {error && (
          <div role="alert" style={{
            marginBottom: 14,
            padding: '10px 12px',
            borderRadius: 8,
            border: `1px solid ${colors.red200}`,
            background: colors.red50,
            color: colors.red700,
            fontSize: 13,
          }}>
            {error}
          </div>
        )}
        {canRecover ? (
          <button
            type="button"
            onClick={startRecovery}
            disabled={submitting}
            style={{
              ...btnPrimary,
              fontWeight: 600,
              opacity: submitting ? 0.65 : 1,
              cursor: submitting ? 'wait' : 'pointer',
            }}
          >
            {submitting ? 'Starting safe rebuild...' : 'Back up and rebuild index'}
          </button>
        ) : (
          <p style={{ margin: 0, color: colors.red700, fontSize: 13.5, lineHeight: 1.55 }}>
            Automatic recovery is not available because the existing safety state could not be read reliably. No index files were changed. Check the workbench log for the next safe step.
          </p>
        )}
        <DiagnosticDetails health={health} />
      </div>
    );
  } else if (health.status === 'unavailable') {
    content = (
      <div role="alert">
        <h1 style={{ margin: '0 0 8px', fontSize: 21, color: colors.gray900 }}>
          The local index is unavailable
        </h1>
        <p style={{ margin: '0 0 12px', color: colors.red700, fontSize: 14, lineHeight: 1.55 }}>
          {health.message || 'ClawJournal could not open the local index safely.'}
        </p>
        <p style={{ margin: 0, color: colors.gray600, fontSize: 13.5, lineHeight: 1.55 }}>
          Background index work and uploads were not started. Check that the state directory is writable and available, then restart ClawJournal.
        </p>
        <p style={{ margin: '12px 0 0', color: colors.gray500, fontSize: 12.5, lineHeight: 1.55 }}>
          On a shared or network home, move the whole state directory to persistent local storage and set <code>CLAWJOURNAL_HOME</code> to it before restarting.
        </p>
        <DiagnosticDetails health={health} />
      </div>
    );
  } else {
    content = <Spinner text="Opening the workbench..." />;
  }

  return (
    <main style={{
      flex: 1,
      minHeight: 0,
      boxSizing: 'border-box',
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      padding: 24,
      background: colors.gray50,
      fontFamily,
    }}>
      <section style={{
        width: '100%',
        maxWidth: 590,
        padding: '28px 30px',
        boxSizing: 'border-box',
        borderRadius: 12,
        border: `1px solid ${colors.gray200}`,
        background: colors.white,
        boxShadow: '0 8px 28px rgba(42, 40, 37, 0.08)',
      }}>
        <div style={{ marginBottom: 20, color: colors.gray800, fontSize: 17, fontWeight: 700 }}>
          ClawJournal
        </div>
        {content}
      </section>
    </main>
  );
}
