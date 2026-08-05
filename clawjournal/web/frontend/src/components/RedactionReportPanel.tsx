import { useState, useEffect, useCallback } from 'react';
import type { RedactionLogEntry } from '../types.ts';
import { api } from '../api.ts';
import { useToast } from './Toast.tsx';
import { Spinner } from './Spinner.tsx';
import { colors } from '../theme.ts';

const TYPE_LABELS: Record<string, string> = {
  jwt: 'JWT Token', jwt_partial: 'JWT (partial)', db_url: 'Database URL',
  anthropic_key: 'Anthropic Key', openai_key: 'OpenAI Key',
  github_token: 'GitHub Token', hf_token: 'HuggingFace Token',
  pypi_token: 'PyPI Token', npm_token: 'npm Token',
  aws_key: 'AWS Key', aws_secret: 'AWS Secret',
  slack_token: 'Slack Token', discord_webhook: 'Discord Webhook',
  private_key: 'Private Key', cli_token_flag: 'CLI Token',
  env_secret: 'Env Secret', generic_secret: 'Generic Secret',
  bearer: 'Bearer Token', url_token: 'URL Token',
  ip_address: 'IP Address', email: 'Email', high_entropy: 'High Entropy',
};

function confidenceColor(c: number): string {
  if (c >= 0.90) return colors.green500;
  if (c >= 0.70) return colors.yellow400;
  return colors.red400;
}

function confidenceLabel(c: number): string {
  if (c >= 0.90) return 'High';
  if (c >= 0.70) return 'Medium';
  return 'Low';
}

interface RedactionReportPanelProps {
  sessionId: string;
  onScrollToMessage?: (messageIndex: number) => void;
}

export function RedactionReportPanel({ sessionId, onScrollToMessage }: RedactionReportPanelProps) {
  const { toast } = useToast();
  const [log, setLog] = useState<RedactionLogEntry[] | null>(null);
  const [count, setCount] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showAllowForm, setShowAllowForm] = useState<number | null>(null);
  const [allowText, setAllowText] = useState('');
  const [allowReason, setAllowReason] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const report = await api.sessions.redactionReport(sessionId);
      setLog(report.redaction_log);
      setCount(report.redaction_count);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load redaction report');
    } finally {
      setLoading(false);
    }
  }, [sessionId]);

  useEffect(() => { load(); }, [load]);

  const handleAllowCategory = async (entry: RedactionLogEntry) => {
    try {
      await api.allowlist.add({
        type: 'category',
        match_type: entry.type,
        reason: `Skipped all ${TYPE_LABELS[entry.type] ?? entry.type} detections`,
      });
      toast(`All "${TYPE_LABELS[entry.type] ?? entry.type}" findings will be skipped`, 'success');
      load(); // Refresh to reflect allowlist
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Failed to add allowlist entry', 'error');
    }
  };

  const handleAllowExact = async () => {
    if (!allowText.trim()) return;
    try {
      await api.allowlist.add({
        type: 'exact',
        text: allowText.trim(),
        reason: allowReason.trim() || undefined,
      });
      toast('Added to allowlist', 'success');
      setShowAllowForm(null);
      setAllowText('');
      setAllowReason('');
      load();
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Failed to add', 'error');
    }
  };

  if (loading) return <Spinner text="Scanning for redactions..." />;

  if (error) {
    return (
      <div style={{ padding: '12px 0', fontSize: 13, color: colors.red700 }}>
        {error}
      </div>
    );
  }

  if (!log || log.length === 0) {
    return (
      <div style={{ padding: '12px 0', fontSize: 13, color: colors.green700 }}>
        No secrets or sensitive content detected.
      </div>
    );
  }

  // Group by type for summary
  const byType: Record<string, number> = {};
  for (const entry of log) {
    byType[entry.type] = (byType[entry.type] ?? 0) + 1;
  }

  return (
    <div>
      {/* Summary */}
      <div style={{
        padding: '8px 12px', marginBottom: 8, borderRadius: 6,
        background: colors.yellow50, border: `1px solid ${colors.yellow200}`,
        fontSize: 13,
      }}>
        <strong style={{ color: colors.yellow700 }}>{count} redaction{count !== 1 ? 's' : ''}</strong>
        <span style={{ color: colors.yellow700 }}> &mdash; </span>
        {Object.entries(byType)
          .sort(([, a], [, b]) => b - a)
          .map(([type, cnt], i) => (
            <span key={type} style={{ color: colors.gray600 }}>
              {i > 0 && ', '}{cnt} {TYPE_LABELS[type] ?? type}
            </span>
          ))}
      </div>

      {/* Findings list */}
      <div style={{ maxHeight: 400, overflowY: 'auto' }}>
        {log.map((entry, i) => (
          <div key={i} style={{
            padding: '8px 0',
            borderBottom: `1px solid ${colors.gray100}`,
          }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
              {/* Type badge */}
              <span style={{
                padding: '1px 8px', borderRadius: 9999, fontSize: 11, fontWeight: 600,
                background: confidenceColor(entry.confidence) + '18',
                color: confidenceColor(entry.confidence),
              }}>
                {TYPE_LABELS[entry.type] ?? entry.type}
              </span>

              {/* Confidence */}
              <span style={{ fontSize: 11, color: confidenceColor(entry.confidence), fontWeight: 600 }}>
                {confidenceLabel(entry.confidence)} ({(entry.confidence * 100).toFixed(0)}%)
              </span>

              {/* Location */}
              {entry.message_index != null && (
                <span
                  style={{
                    fontSize: 11, color: colors.blue500, cursor: onScrollToMessage ? 'pointer' : 'default',
                    textDecoration: onScrollToMessage ? 'underline' : 'none',
                  }}
                  onClick={() => onScrollToMessage?.(entry.message_index!)}
                >
                  Message #{entry.message_index}, {entry.field}
                </span>
              )}

              {/* Length */}
              <span style={{ fontSize: 11, color: colors.gray400, marginLeft: 'auto' }}>
                {entry.original_length} chars
              </span>
            </div>

            {/* Context (for low/medium confidence) */}
            {(entry.context_before || entry.context_after) && (
              <div style={{
                fontSize: 12, color: colors.gray500, fontFamily: 'monospace',
                background: colors.gray50, padding: '4px 8px', borderRadius: 4, marginBottom: 4,
                wordBreak: 'break-all',
              }}>
                {entry.context_before && <span>{entry.context_before}</span>}
                <span style={{ background: colors.red100, color: colors.red700, padding: '0 3px', borderRadius: 2, fontWeight: 600 }}>[REDACTED]</span>
                {entry.context_after && <span>{entry.context_after}</span>}
              </div>
            )}

            {/* Actions */}
            <div style={{ display: 'flex', gap: 6, marginTop: 4 }}>
              <button
                onClick={() => handleAllowCategory(entry)}
                style={{
                  padding: '2px 8px', fontSize: 11, fontWeight: 500,
                  background: colors.white, border: `1px solid ${colors.gray300}`,
                  borderRadius: 4, cursor: 'pointer', color: colors.gray600,
                }}
              >
                Skip all {TYPE_LABELS[entry.type] ?? entry.type}
              </button>
              <button
                onClick={() => setShowAllowForm(showAllowForm === i ? null : i)}
                style={{
                  padding: '2px 8px', fontSize: 11, fontWeight: 500,
                  background: colors.white, border: `1px solid ${colors.gray300}`,
                  borderRadius: 4, cursor: 'pointer', color: colors.gray600,
                }}
              >
                Allow specific text
              </button>
            </div>

            {/* Inline allow form */}
            {showAllowForm === i && (
              <div style={{
                marginTop: 6, padding: 8, background: colors.gray50,
                borderRadius: 6, border: `1px solid ${colors.gray200}`,
              }}>
                <input
                  type="text"
                  value={allowText}
                  onChange={e => setAllowText(e.target.value)}
                  placeholder="Enter the exact text to allow..."
                  style={{
                    width: '100%', padding: '5px 8px', fontSize: 12,
                    border: `1px solid ${colors.gray300}`, borderRadius: 4,
                    boxSizing: 'border-box', fontFamily: 'monospace',
                  }}
                />
                <input
                  type="text"
                  value={allowReason}
                  onChange={e => setAllowReason(e.target.value)}
                  placeholder="Reason (optional)"
                  style={{
                    width: '100%', padding: '5px 8px', fontSize: 12, marginTop: 4,
                    border: `1px solid ${colors.gray300}`, borderRadius: 4,
                    boxSizing: 'border-box',
                  }}
                />
                <div style={{ display: 'flex', gap: 6, marginTop: 6 }}>
                  <button
                    onClick={handleAllowExact}
                    disabled={!allowText.trim()}
                    style={{
                      padding: '4px 12px', fontSize: 12, fontWeight: 600,
                      background: allowText.trim() ? colors.blue500 : colors.gray300,
                      color: colors.white, border: 'none', borderRadius: 4, cursor: 'pointer',
                    }}
                  >
                    Add to allowlist
                  </button>
                  <button
                    onClick={() => { setShowAllowForm(null); setAllowText(''); setAllowReason(''); }}
                    style={{
                      padding: '4px 12px', fontSize: 12, fontWeight: 600,
                      background: colors.white, color: colors.gray600,
                      border: `1px solid ${colors.gray300}`, borderRadius: 4, cursor: 'pointer',
                    }}
                  >
                    Cancel
                  </button>
                </div>
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
