import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import type { Policy } from '../types.ts';
import { api } from '../api.ts';
import { useToast } from '../components/Toast.tsx';
import { ConfirmDialog } from '../components/ConfirmDialog.tsx';
import { Spinner } from '../components/Spinner.tsx';
import { colors, inputStyle as baseInputStyle } from '../theme.ts';

const POLICY_TYPE_OPTIONS: { label: string; value: string }[] = [
  { label: 'Redact String', value: 'redact_string' },
  { label: 'Redact Username', value: 'redact_username' },
  { label: 'Exclude Project', value: 'exclude_project' },
  { label: 'Block Domain', value: 'block_domain' },
];

const TYPE_LABELS: Record<string, string> = {
  redact_string: 'Redact String',
  redact_username: 'Redact Username',
  exclude_project: 'Exclude Project',
  block_domain: 'Block Domain',
};

const PRESET_RULES: { label: string; type: string; value: string; reason: string }[] = [
  { label: 'Your Company Domain', type: 'redact_string', value: '@yourcompany.com', reason: 'Redact company email domain' },
  { label: 'Internal Domains', type: 'block_domain', value: '*.internal', reason: 'Block internal domain references' },
  { label: 'Company Name', type: 'redact_string', value: 'YourCompanyName', reason: 'Redact company name from traces' },
  { label: 'Slack Workspace URL', type: 'redact_string', value: 'yourteam.slack.com', reason: 'Redact Slack workspace URL' },
];

export function Policies() {
  const { toast } = useToast();
  const [policies, setPolicies] = useState<Policy[]>([]);
  const [newType, setNewType] = useState('redact_string');
  const [newValue, setNewValue] = useState('');
  const [newReason, setNewReason] = useState('');
  const [loading, setLoading] = useState(true);
  const [deleteTarget, setDeleteTarget] = useState<Policy | null>(null);

  const loadPolicies = useCallback(async () => {
    try {
      const data = await api.policies.list();
      setPolicies(data);
    } catch (e: unknown) {
      toast(e instanceof Error ? e.message : 'Failed to load policies', 'error');
    } finally {
      setLoading(false);
    }
  }, [toast]);

  useEffect(() => {
    loadPolicies();
  }, [loadPolicies]);

  async function handleAdd() {
    if (!newValue.trim()) return;
    try {
      await api.policies.add(newType, newValue.trim(), newReason.trim() || undefined);
      setNewValue('');
      setNewReason('');
      await loadPolicies();
      toast('Policy added', 'success');
    } catch (e: unknown) {
      toast(e instanceof Error ? e.message : 'Failed to add policy', 'error');
    }
  }

  async function handleDelete(policy: Policy) {
    try {
      await api.policies.remove(policy.policy_id);
      await loadPolicies();
      toast('Policy deleted', 'success');
    } catch (e: unknown) {
      toast(e instanceof Error ? e.message : 'Failed to delete policy', 'error');
    }
    setDeleteTarget(null);
  }

  const formInputStyle: React.CSSProperties = {
    ...baseInputStyle,
    padding: '8px 10px',
    borderRadius: 6,
    fontSize: 14,
  };

  return (
    <div style={{ padding: '24px', maxWidth: '960px', margin: '0 auto' }}>
      <div style={{ marginBottom: 16 }}>
        <Link to="/share" style={{ fontSize: 12.5, color: colors.gray500, textDecoration: 'none' }}>&larr; Back to share</Link>
      </div>
      <h2 style={{ margin: '0 0 4px', fontSize: '20px', fontWeight: 600, color: colors.gray900 }}>Redaction rules</h2>
      <p style={{ fontSize: 14, color: colors.gray500, margin: '0 0 20px 0' }}>Configure redaction and exclusion filters for shared traces</p>

      {/* Add Policy Form */}
      <div
        style={{
          background: colors.white,
          border: `1px solid ${colors.gray200}`,
          borderRadius: '8px',
          padding: '16px 20px',
          marginBottom: '24px',
        }}
      >
        <h3 style={{ margin: '0 0 12px', fontSize: '14px', fontWeight: 600, color: colors.gray700 }}>Add Policy</h3>
        <div style={{ display: 'flex', gap: '10px', alignItems: 'flex-end', flexWrap: 'wrap' }}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
            <label style={{ fontSize: '13px', fontWeight: 500, color: colors.gray500 }}>Type</label>
            <select
              value={newType}
              onChange={(e) => setNewType(e.target.value)}
              style={{
                ...formInputStyle,
                minWidth: '160px',
              }}
            >
              {POLICY_TYPE_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>
                  {opt.label}
                </option>
              ))}
            </select>
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '4px', flex: 1, minWidth: '180px' }}>
            <label style={{ fontSize: '13px', fontWeight: 500, color: colors.gray500 }}>Value</label>
            <input
              type="text"
              value={newValue}
              onChange={(e) => setNewValue(e.target.value)}
              placeholder="String, username, project, or domain..."
              style={formInputStyle}
              onKeyDown={(e) => { if (e.key === 'Enter') handleAdd(); }}
            />
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '4px', flex: 1, minWidth: '140px' }}>
            <label style={{ fontSize: '13px', fontWeight: 500, color: colors.gray500 }}>Reason (optional)</label>
            <input
              type="text"
              value={newReason}
              onChange={(e) => setNewReason(e.target.value)}
              placeholder="Why this policy exists..."
              style={formInputStyle}
              onKeyDown={(e) => { if (e.key === 'Enter') handleAdd(); }}
            />
          </div>
          <button
            onClick={handleAdd}
            disabled={!newValue.trim()}
            style={{
              padding: '8px 16px',
              background: newValue.trim() ? colors.blue500 : colors.gray400,
              color: colors.white,
              border: 'none',
              borderRadius: '6px',
              fontSize: '14px',
              fontWeight: 500,
              cursor: newValue.trim() ? 'pointer' : 'default',
              whiteSpace: 'nowrap',
            }}
          >
            Add
          </button>
        </div>
      </div>

      {/* Built-in redaction note */}
      <div
        style={{
          background: colors.green50,
          border: `1px solid ${colors.green200}`,
          borderRadius: '8px',
          padding: '14px 20px',
          marginBottom: '24px',
          fontSize: '14px',
          lineHeight: 1.6,
          color: colors.green700,
        }}
      >
        <div style={{ fontWeight: 600, marginBottom: 4 }}>Built-in redaction (always active)</div>
        <div style={{ color: colors.green800 }}>
          ClawJournal automatically redacts API keys (OpenAI, Anthropic, AWS, GitHub, HuggingFace, npm, PyPI, Slack),
          JWTs, database URLs, bearer tokens, private keys, emails, IP addresses, and high-entropy secrets.
          Use the rules below to add <strong>your own</strong> patterns — company names, internal URLs, team-specific strings.
        </div>
      </div>

      {/* Suggested Rules */}
      {(() => {
        const existingValues = new Set(policies.map(p => p.value));
        const availablePresets = PRESET_RULES.filter(p => !existingValues.has(p.value));
        if (loading || availablePresets.length === 0) return null;
        return (
        <div
          style={{
            background: colors.yellow50,
            border: `1px solid ${colors.yellow200}`,
            borderRadius: '8px',
            padding: '16px 20px',
            marginBottom: '24px',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
            <h3 style={{ margin: 0, fontSize: '14px', fontWeight: 600, color: colors.yellow700 }}>
              Suggested Rules
            </h3>
            <button
              onClick={async () => {
                try {
                  for (const preset of availablePresets) {
                    await api.policies.add(preset.type, preset.value, preset.reason);
                  }
                  await loadPolicies();
                  toast(`Added ${availablePresets.length} preset rules`, 'success');
                } catch (e: unknown) {
                  toast(e instanceof Error ? e.message : 'Failed to add presets', 'error');
                }
              }}
              style={{
                padding: '5px 14px',
                background: colors.yellow700,
                color: colors.white,
                border: 'none',
                borderRadius: 5,
                fontSize: 13,
                fontWeight: 500,
                cursor: 'pointer',
                whiteSpace: 'nowrap',
              }}
            >
              Add All
            </button>
          </div>
          <p style={{ fontSize: 13, color: colors.yellow700, margin: '0 0 12px' }}>
            Common redaction patterns you can add:
          </p>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {availablePresets.map((preset) => (
              <div
                key={preset.label}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 12,
                  padding: '8px 12px',
                  background: colors.white,
                  borderRadius: 6,
                  border: `1px solid ${colors.yellow200}`,
                }}
              >
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{ fontSize: 14, fontWeight: 600, color: colors.gray700 }}>{preset.label}</div>
                  <div style={{ fontSize: 12, color: colors.gray500, fontFamily: 'monospace', marginTop: 2 }}>
                    {TYPE_LABELS[preset.type]} &middot; {preset.value}
                  </div>
                </div>
                <button
                  onClick={async () => {
                    try {
                      await api.policies.add(preset.type, preset.value, preset.reason);
                      await loadPolicies();
                      toast(`Added: ${preset.label}`, 'success');
                    } catch (e: unknown) {
                      toast(e instanceof Error ? e.message : 'Failed to add preset', 'error');
                    }
                  }}
                  style={{
                    padding: '5px 14px',
                    background: colors.blue500,
                    color: colors.white,
                    border: 'none',
                    borderRadius: 5,
                    fontSize: 13,
                    fontWeight: 500,
                    cursor: 'pointer',
                    whiteSpace: 'nowrap',
                  }}
                >
                  Add
                </button>
              </div>
            ))}
          </div>
        </div>
        );
      })()}

      {/* Policies Table */}
      {loading ? (
        <Spinner text="Loading policies..." />
      ) : policies.length === 0 ? (
        null
      ) : (
        <div
          style={{
            background: colors.white,
            border: `1px solid ${colors.gray200}`,
            borderRadius: '8px',
            overflow: 'hidden',
          }}
        >
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '14px' }}>
            <thead>
              <tr
                style={{
                  textAlign: 'left',
                  background: colors.gray50,
                  borderBottom: `1px solid ${colors.gray200}`,
                }}
              >
                <th style={{ padding: '10px 16px', fontWeight: 600, color: colors.gray700 }}>Type</th>
                <th style={{ padding: '10px 16px', fontWeight: 600, color: colors.gray700 }}>Value</th>
                <th style={{ padding: '10px 16px', fontWeight: 600, color: colors.gray700 }}>Reason</th>
                <th style={{ padding: '10px 16px', fontWeight: 600, color: colors.gray700 }}>Created</th>
                <th style={{ padding: '10px 16px', fontWeight: 600, color: colors.gray700, width: '80px' }}>Actions</th>
              </tr>
            </thead>
            <tbody>
              {policies.map((policy, i) => (
                <tr
                  key={policy.policy_id}
                  style={{
                    borderBottom: `1px solid ${colors.gray200}`,
                    background: i % 2 === 0 ? colors.white : colors.gray50,
                  }}
                >
                  <td style={{ padding: '10px 16px', color: colors.gray700, fontWeight: 500 }}>
                    {TYPE_LABELS[policy.policy_type] ?? policy.policy_type}
                  </td>
                  <td style={{ padding: '10px 16px', fontFamily: 'monospace', color: colors.gray900, fontSize: '13px' }}>
                    {policy.value}
                  </td>
                  <td style={{ padding: '10px 16px', color: colors.gray500 }}>
                    {policy.reason || '—'}
                  </td>
                  <td style={{ padding: '10px 16px', color: colors.gray500, fontSize: '13px' }}>
                    {new Date(policy.created_at).toLocaleDateString()}
                  </td>
                  <td style={{ padding: '10px 16px' }}>
                    <button
                      onClick={() => setDeleteTarget(policy)}
                      style={{
                        padding: '4px 10px',
                        background: colors.red100,
                        color: colors.red700,
                        border: `1px solid ${colors.red200}`,
                        borderRadius: '5px',
                        fontSize: '13px',
                        fontWeight: 500,
                        cursor: 'pointer',
                      }}
                    >
                      Delete
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Delete confirmation */}
      <ConfirmDialog
        open={deleteTarget !== null}
        title="Delete policy?"
        message={deleteTarget ? `Remove the ${TYPE_LABELS[deleteTarget.policy_type] ?? deleteTarget.policy_type} rule for "${deleteTarget.value}"? This cannot be undone.` : ''}
        confirmLabel="Delete"
        variant="danger"
        onConfirm={() => deleteTarget && handleDelete(deleteTarget)}
        onCancel={() => setDeleteTarget(null)}
      />
    </div>
  );
}
