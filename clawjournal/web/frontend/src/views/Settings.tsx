import { useEffect, useState } from 'react';
import { api } from '../api.ts';
import type { WorkbenchConfig } from '../types.ts';
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

  useEffect(() => {
    api.config.get()
      .then(setCfg)
      .catch(e => setError(e instanceof Error ? e.message : 'Could not load settings'));
  }, []);

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
        <div style={titleStyle}>Export source scope</div>
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
            <option key={s} value={s}>{s === 'all' ? 'All agents' : s}</option>
          ))}
        </select>
      </div>

      {/* Project confirmation */}
      <div style={cardStyle}>
        <div style={titleStyle}>Project confirmation</div>
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
        <div style={titleStyle}>AI-assisted PII review (share default)</div>
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
        <div style={titleStyle}>AI scoring backend</div>
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
        <div style={titleStyle}>Background AI scoring</div>
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
        <div style={titleStyle}>Benchmark tab</div>
        <p style={helpStyle}>
          Show or hide the personalized weekly Benchmark tab in the sidebar. Updates on the next
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
