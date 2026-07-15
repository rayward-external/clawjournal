import { useEffect, useState } from 'react';
import { api } from '../api.ts';
import type { AutoUploadPreview, AutoUploadStatus, WorkbenchConfig } from '../types.ts';
import { useToast } from '../components/Toast.tsx';
import { colors, selectStyle, btnPrimary } from '../theme.ts';

type ConfigPatch = Partial<{
  source: string;
  scorer_backend: string;
  confirm_projects: boolean;
  ai_pii_review_enabled: boolean;
  benchmark_tab_enabled: boolean;
  scoring_warmup_declined: boolean;
}>;

const cardStyle: React.CSSProperties = {
  background: colors.white,
  border: `1px solid ${colors.gray200}`,
  borderRadius: 10,
  padding: '16px 20px',
  marginBottom: 14,
};

const titleStyle: React.CSSProperties = {
  margin: '0 0 4px', fontSize: 14, fontWeight: 600, color: colors.gray800,
};

const helpStyle: React.CSSProperties = {
  margin: '0 0 12px', fontSize: 12.5, color: colors.gray500, lineHeight: 1.5,
};

export function Settings() {
  const { toast } = useToast();
  const [cfg, setCfg] = useState<WorkbenchConfig | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [autoUpload, setAutoUpload] = useState<AutoUploadStatus | null>(null);
  const [autoPreview, setAutoPreview] = useState<AutoUploadPreview | null>(null);
  const [autoBusy, setAutoBusy] = useState(false);

  useEffect(() => {
    api.config.get()
      .then(setCfg)
      .catch(e => setError(e instanceof Error ? e.message : 'Could not load settings'));
    api.autoUpload.status()
      .then(setAutoUpload)
      .catch(e => toast(e instanceof Error ? e.message : 'Could not load automatic sharing status', 'error'));
  }, []);

  async function refreshAutoUpload() {
    setAutoUpload(await api.autoUpload.status());
  }

  async function enableAutoUpload() {
    setAutoBusy(true);
    try {
      const terms = await api.autoUpload.terms();
      const accepted = window.confirm(
        `Enable automatic weekly sharing?\n\nAt most five future eligible traces from the exact sources and projects shown here may upload without per-bundle review. Append-only traces must remain unchanged for 24 hours. Due work starts in the background on a supported Claude Code or Codex SessionStart; Run now starts one extra capped cycle and resets the seven-day clock. Local anonymization, redaction, findings, optional AI-PII processing, and both TruffleHog gates still apply. You can preview, hold, pause, or disable this at any time.\n\n${terms.consent_text}\n\n${terms.retention_text}\n\nBy continuing, you accept these recurring terms and certify that you are authorized to share traces from this exact scope.`
      );
      if (!accepted) return;
      const status = await api.autoUpload.enable({
        accept_terms: true,
        ownership_certification: true,
        consent_version: terms.consent_version,
        retention_policy_version: terms.retention_policy_version,
      });
      setAutoUpload(status);
      setAutoPreview(null);
      toast('Automatic weekly sharing enabled', 'success');
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Could not enable automatic sharing', 'error');
    } finally {
      setAutoBusy(false);
    }
  }

  async function previewAutoUpload() {
    setAutoBusy(true);
    try {
      setAutoPreview(await api.autoUpload.preview());
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Could not preview automatic sharing', 'error');
    } finally {
      setAutoBusy(false);
    }
  }

  async function runAutoUpload() {
    setAutoBusy(true);
    try {
      const result = await api.autoUpload.run();
      await refreshAutoUpload();
      setAutoPreview(null);
      toast(result.status === 'no_work' ? 'No eligible traces to share' : 'Automatic share completed', 'success');
    } catch (e) {
      await refreshAutoUpload().catch(() => undefined);
      toast(e instanceof Error ? e.message : 'Automatic share failed', 'error');
    } finally {
      setAutoBusy(false);
    }
  }

  async function pauseAutoUpload() {
    setAutoBusy(true);
    try {
      setAutoUpload(await api.autoUpload.pause());
      toast('Automatic sharing paused', 'success');
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Could not pause automatic sharing', 'error');
    } finally {
      setAutoBusy(false);
    }
  }

  async function resumeAutoUpload() {
    setAutoBusy(true);
    try {
      setAutoUpload(await api.autoUpload.resume());
      toast('Automatic sharing resumed', 'success');
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Could not resume automatic sharing', 'error');
    } finally {
      setAutoBusy(false);
    }
  }

  async function disableAutoUpload() {
    if (!window.confirm('Disable automatic sharing and remove its system scheduler?')) return;
    setAutoBusy(true);
    try {
      setAutoUpload(await api.autoUpload.disable());
      setAutoPreview(null);
      toast('Automatic sharing disabled', 'success');
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Could not disable automatic sharing', 'error');
    } finally {
      setAutoBusy(false);
    }
  }

  async function save(patch: ConfigPatch) {
    setSaving(true);
    try {
      const next = await api.config.update(patch);
      setCfg(next);
      toast('Settings saved', 'success');
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Save failed', 'error');
    } finally {
      setSaving(false);
    }
  }

  if (error) {
    return (
      <div style={{ padding: 24, maxWidth: 720, margin: '0 auto' }}>
        <h2 style={{ fontSize: 20, fontWeight: 600, color: colors.gray900 }}>Settings</h2>
        <p style={{ color: colors.red700, fontSize: 14 }}>{error}</p>
      </div>
    );
  }
  if (!cfg) {
    return (
      <div style={{ padding: 24, maxWidth: 720, margin: '0 auto', color: colors.gray500 }}>
        Loading settings…
      </div>
    );
  }

  return (
    <div style={{ padding: 24, maxWidth: 720, margin: '0 auto' }}>
      <h2 style={{ margin: '0 0 4px', fontSize: 20, fontWeight: 600, color: colors.gray900 }}>Settings</h2>
      <p style={{ margin: '0 0 18px', fontSize: 13, color: colors.gray500 }}>
        Local configuration for this workbench. These mirror the <code>clawjournal config</code> flags.
      </p>

      {/* Export source scope */}
      <div style={cardStyle}>
        <h3 style={titleStyle}>Export source scope</h3>
        <p style={helpStyle}>
          Which agents’ traces are eligible for export and sharing. Required (with project
          confirmation below) before any export.
        </p>
        <select
          style={{ ...selectStyle, minWidth: 200 }}
          value={cfg.source ?? ''}
          disabled={saving}
          onChange={e => save({ source: e.target.value })}
        >
          <option value="" disabled>Select a source…</option>
          {cfg.source_choices.map(s => (
            <option key={s} value={s}>{s === 'all' ? 'All agents' : s === 'claude-science' ? 'Claude Science' : s === 'workbuddy' ? 'WorkBuddy' : s}</option>
          ))}
        </select>
      </div>

      {/* Project confirmation */}
      <div style={cardStyle}>
        <h3 style={titleStyle}>Project confirmation</h3>
        <p style={helpStyle}>
          Confirms you’ve reviewed which project folders are included (after applying any
          exclusions). Required before export.
        </p>
        {cfg.projects_confirmed ? (
          <span style={{ fontSize: 13, color: colors.green500, fontWeight: 600 }}>✓ Projects confirmed</span>
        ) : (
          <button
            style={{ ...btnPrimary, fontWeight: 600 }}
            disabled={saving}
            onClick={() => save({ confirm_projects: true })}
          >
            Confirm all projects
          </button>
        )}
      </div>

      {/* AI-PII review default */}
      <div style={cardStyle}>
        <h3 style={titleStyle}>AI-assisted PII review (share default)</h3>
        <p style={helpStyle}>
          When on, the share flow adds an AI pass over your <strong>already-redacted</strong>,
          anonymized transcript to flag contextual identifiers. This step sends that text to your
          configured AI backend — it is the one part of redaction that leaves your device.
        </p>
        <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13, color: colors.gray700 }}>
          <input
            type="checkbox"
            checked={cfg.ai_pii_review_enabled}
            disabled={saving}
            onChange={e => save({ ai_pii_review_enabled: e.target.checked })}
          />
          Default AI-PII review on for new shares
        </label>
      </div>

      <div style={cardStyle}>
        <h3 style={titleStyle}>Automatic weekly sharing</h3>
        <p style={helpStyle}>
          Once enabled, a supported Claude Code or Codex SessionStart checks whether seven days
          have elapsed. One cycle selects at most five future eligible traces from the enrolled
          scope. Stored failure-value scores only order candidates; automatic runs never score.
        </p>
        {!autoUpload ? (
          <span style={{ fontSize: 13, color: colors.gray500 }}>Loading status...</span>
        ) : (
          <>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10, fontSize: 12.5, color: colors.gray600, marginBottom: 12 }}>
              <div><strong style={{ color: colors.gray800 }}>State:</strong> {autoUpload.state}</div>
              <div><strong style={{ color: colors.gray800 }}>Pending:</strong> {autoUpload.pending_count}</div>
              <div><strong style={{ color: colors.gray800 }}>Sources:</strong> {autoUpload.source_scope === 'all' ? 'all agents' : autoUpload.sources?.join(', ') || 'not enrolled'}</div>
              <div><strong style={{ color: colors.gray800 }}>Projects:</strong> {autoUpload.included_projects?.join(', ') || 'not enrolled'}</div>
              <div><strong style={{ color: colors.gray800 }}>Next run:</strong> {autoUpload.next_due_at ? new Date(autoUpload.next_due_at).toLocaleString() : 'not scheduled'}</div>
              <div><strong style={{ color: colors.gray800 }}>Last success:</strong> {autoUpload.last_success_at ? new Date(autoUpload.last_success_at).toLocaleString() : 'never'}</div>
              <div><strong style={{ color: colors.gray800 }}>Last upload:</strong> {autoUpload.last_trace_count ?? 0} traces</div>
            </div>
            {autoUpload.last_error && (
              <div style={{ marginBottom: 12, padding: '9px 10px', background: colors.yellow50, border: `1px solid ${colors.yellow200}`, borderRadius: 7, color: colors.yellow700, fontSize: 12.5 }}>
                {autoUpload.last_error}{autoUpload.required_action ? ` Action: ${autoUpload.required_action}.` : ''}
              </div>
            )}
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
              {autoUpload.state === 'off' && autoUpload.capability_available && autoUpload.manual_share_completed && (
                <button style={{ ...btnPrimary, fontWeight: 600 }} disabled={autoBusy} onClick={enableAutoUpload}>
                  Enable
                </button>
              )}
              {autoUpload.state === 'paused' && (
                <button style={{ ...btnPrimary, fontWeight: 600 }} disabled={autoBusy} onClick={resumeAutoUpload}>Resume</button>
              )}
              {autoUpload.state === 'enabled' && (
                <>
                  <button style={{ ...btnPrimary, fontWeight: 600 }} disabled={autoBusy} onClick={previewAutoUpload}>Preview</button>
                  <button style={{ ...btnPrimary, fontWeight: 600 }} disabled={autoBusy} onClick={runAutoUpload}>Run now</button>
                  <button style={{ ...btnPrimary, background: colors.gray600, fontWeight: 600 }} disabled={autoBusy} onClick={pauseAutoUpload}>Pause</button>
                </>
              )}
              {autoUpload.enrolled && autoUpload.state !== 'off' && (
                <button style={{ ...btnPrimary, background: colors.red700, fontWeight: 600 }} disabled={autoBusy} onClick={disableAutoUpload}>Disable</button>
              )}
            </div>
            {autoPreview && (
              <div style={{ marginTop: 14, borderTop: `1px solid ${colors.gray200}`, paddingTop: 12 }}>
                <strong style={{ fontSize: 12.5, color: colors.gray800 }}>{autoPreview.count} trace(s) would be uploaded</strong>
                {autoPreview.sessions.slice(0, 10).map(session => (
                  <div key={session.session_id} style={{ fontSize: 12, color: colors.gray600, marginTop: 6 }}>
                    {session.source} / {session.project}: {session.display_title}
                  </div>
                ))}
                {autoPreview.count > 10 && <div style={{ fontSize: 12, color: colors.gray500, marginTop: 6 }}>And {autoPreview.count - 10} more.</div>}
              </div>
            )}
          </>
        )}
      </div>

      {/* Advanced — scoring + UI toggles most users never change. Collapsed by
          default so the page leads with the export-gate essentials above. */}
      <button
        onClick={() => setShowAdvanced(v => !v)}
        style={{
          display: 'flex', alignItems: 'center', gap: 6, width: '100%',
          background: 'none', border: 'none', cursor: 'pointer',
          padding: '4px 0', margin: '8px 0 12px',
          fontSize: 13, fontWeight: 600, color: colors.gray700,
        }}
      >
        <span style={{ fontSize: 11, color: colors.gray500 }}>{showAdvanced ? '▾' : '▸'}</span>
        Advanced
        <span style={{ fontWeight: 400, color: colors.gray400, fontSize: 12 }}>
          scoring backend, background scoring, Benchmark tab
        </span>
      </button>

      {showAdvanced && (
      <>
      {/* Scorer backend */}
      <div style={cardStyle}>
        <h3 style={titleStyle}>AI scoring backend</h3>
        <p style={helpStyle}>
          The agent CLI used to score sessions and generate benchmarks. Scoring sends your
          anonymized transcript to this backend.
          {cfg.scorer_backend_detected
            ? ` Detected on this machine: ${cfg.scorer_backend_detected}.`
            : ' No backend auto-detected.'}
        </p>
        <select
          style={{ ...selectStyle, minWidth: 200 }}
          value={cfg.scorer_backend ?? ''}
          disabled={saving}
          onChange={e => save({ scorer_backend: e.target.value || 'none' })}
        >
          <option value="">Auto / not set</option>
          {cfg.scorer_backend_choices.map(b => (
            <option key={b} value={b}>{b}</option>
          ))}
        </select>
      </div>

      {/* Background AI scoring */}
      <div style={cardStyle}>
        <h3 style={titleStyle}>Background AI scoring</h3>
        <p style={helpStyle}>
          When on, ClawJournal scores recent traces in the background using your configured AI
          backend (each trace is anonymized on this machine before it is sent, and the agent may
          incur usage cost). Turn it off to stop the auto-scorer and the first-run prompt.
        </p>
        <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13, color: colors.gray700 }}>
          <input
            type="checkbox"
            checked={!cfg.scoring_warmup_declined}
            disabled={saving}
            onChange={e => save({ scoring_warmup_declined: !e.target.checked })}
          />
          Enable background AI scoring
        </label>
      </div>

      {/* Benchmark tab visibility */}
      <div style={cardStyle}>
        <h3 style={titleStyle}>Benchmark tab</h3>
        <p style={helpStyle}>
          Show or hide the personalized weekly Benchmark tab under Analytics. Updates on the next
          refresh.
        </p>
        <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13, color: colors.gray700 }}>
          <input
            type="checkbox"
            checked={cfg.benchmark_tab_enabled}
            disabled={saving}
            onChange={e => save({ benchmark_tab_enabled: e.target.checked })}
          />
          Show the Benchmark tab
        </label>
      </div>
      </>
      )}

      <p style={{ fontSize: 11.5, color: colors.gray400, marginTop: 4 }}>
        Settings are stored locally in <code>~/.clawjournal/config.json</code>.
      </p>
    </div>
  );
}
