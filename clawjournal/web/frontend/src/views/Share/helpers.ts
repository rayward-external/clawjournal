import type { Share as ShareType } from '../../types.ts';
import {
  CONFIDENCE_THRESHOLD,
  DEFAULT_SHARE_QUEUE_SIZE,
  STEPS,
} from './types.ts';
import type {
  BlockedShareSession,
  BucketCounts,
  RedactedSessionData,
  RedactionBucket,
  ShareReadyStats,
  StepKey,
} from './types.ts';

export function blockedSessionsFromError(err: unknown): BlockedShareSession[] {
  const body = (err as { body?: { blocked_sessions?: unknown } } | null)?.body;
  const raw = body?.blocked_sessions;
  if (!Array.isArray(raw)) return [];
  return raw.filter((item): item is BlockedShareSession => (
    !!item
    && typeof item === 'object'
    && typeof (item as { session_id?: unknown }).session_id === 'string'
  ));
}

export function hexAlpha(hex: string, alpha: number): string {
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return `rgba(${r},${g},${b},${alpha})`;
}

export function sourceFullLabel(s: { source: string; client_origin?: string | null; runtime_channel?: string | null }): { label: string; color: string } {
  if (s.source === 'codex') return s.client_origin === 'desktop' ? { label: 'Codex Desktop', color: '#0891b2' } : { label: 'Codex', color: '#16a34a' };
  if (s.source === 'claude') {
    if (s.client_origin === 'desktop' || s.runtime_channel === 'local-agent') return { label: 'Claude Desktop', color: '#7c3aed' };
    return { label: 'Claude Code', color: '#d97706' };
  }
  if (s.source === 'openclaw') return { label: 'OpenClaw', color: '#6b7280' };
  return { label: s.source, color: '#6b7280' };
}

export function autoDescription(share: ShareType): string {
  if (share.submission_note) return share.submission_note;
  if (share.sessions && share.sessions.length > 0) {
    const projects = [...new Set(share.sessions.map(s => s.project).filter(Boolean))].slice(0, 3);
    if (projects.length > 0) return `${share.session_count} sessions from ${projects.join(', ')}`;
  }
  return `${share.session_count} sessions`;
}

export function formatDate(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

export function outcomeBadge(outcome: string | null): string {
  if (!outcome) return '';
  const b = outcome.toLowerCase();
  if (b === 'resolved') return '✓ resolved';
  if (b === 'partial') return '~ partial';
  if (b === 'failed') return '✗ failed';
  if (b === 'abandoned') return '✗ abandoned';
  if (b === 'exploratory') return '— exploratory';
  if (b === 'trivial') return '— trivial';
  if (b.includes('pass')) return '✓ passed';
  if (b.includes('fail')) return '✗ failed';
  if (b.includes('analysis')) return '— analysis';
  if (b.includes('completed')) return '✓ completed';
  if (b.includes('errored')) return '✗ errored';
  return '';
}

export const formatTokens = (t: number) => t >= 1_000_000 ? `${(t / 1_000_000).toFixed(1)}M` : `${(t / 1000).toFixed(0)}k`;
export const formatBytes = (bytes: number) => {
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  if (bytes >= 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${bytes} B`;
};

// Matches SessionDetail's formula: input + output (cache tokens excluded).
export const sessionTotalTokens = (s: { input_tokens?: number; output_tokens?: number }) =>
  (s.input_tokens || 0) + (s.output_tokens || 0);

// Map raw redaction-log `type` strings into a small set of buckets the UI
// surfaces on Redact and Review. Everything else collapses to `other`.
export function bucketOf(type: string): RedactionBucket {
  const t = type.toLowerCase();
  if (t.includes('email')) return 'emails';
  if (t.includes('url')) return 'urls';
  if (t.includes('path') || t.includes('username') || t.includes('home')) return 'paths';
  if (t.includes('time') || t.includes('date')) return 'timestamps';
  if (t.startsWith('trufflehog')) return 'tokens';
  if (t.includes('token') || t.includes('key') || t.includes('secret') || t.includes('jwt') || t.includes('cred') || t.includes('auth')) return 'tokens';
  return 'other';
}

export const emptyBuckets = (): BucketCounts => ({ tokens: 0, emails: 0, paths: 0, timestamps: 0, urls: 0, other: 0 });

export function formatShareDestination(url: string): string {
  try {
    const parsed = new URL(url);
    const path = parsed.pathname && parsed.pathname !== '/' ? parsed.pathname : '';
    return `${parsed.hostname}${path}`;
  } catch {
    return url;
  }
}

export function queueFromStats(stats: ShareReadyStats): string[] {
  const validIds = new Set(stats.sessions.map((s) => s.session_id));
  return (stats.recommended_session_ids || [])
    .filter((id) => validIds.has(id))
    .slice(0, DEFAULT_SHARE_QUEUE_SIZE);
}

export function completedKeysForStep(step: StepKey): Set<string> {
  const idx = STEPS.findIndex((s) => s.key === step);
  return new Set(STEPS.slice(0, Math.max(0, idx)).map((s) => s.key));
}

export function parseStep(value: string | null): StepKey {
  return STEPS.some((s) => s.key === value) ? value as StepKey : 'queue';
}

export function classify(d: RedactedSessionData | undefined): 'checking' | 'clear' | 'review' {
  if (!d || d.loading) return 'checking';
  if (d.aiCoverage === 'rules_only') return 'review';
  const lowConf = (d.aiPiiFindings || []).some(f => f.confidence < CONFIDENCE_THRESHOLD);
  if (lowConf) return 'review';
  return 'clear';
}

export interface RedactionCategory {
  label: string;
  count: number;
  source: 'rules' | 'ai';
}

export function aggregateCategories(data: RedactedSessionData | undefined): RedactionCategory[] {
  const out: RedactionCategory[] = [];
  const buckets = data?.buckets;
  if (buckets) {
    if (buckets.tokens > 0) out.push({ label: `${buckets.tokens} secret${buckets.tokens === 1 ? '' : 's'}`, count: buckets.tokens, source: 'rules' });
    if (buckets.emails > 0) out.push({ label: `${buckets.emails} email${buckets.emails === 1 ? '' : 's'}`, count: buckets.emails, source: 'rules' });
    if (buckets.paths > 0) out.push({ label: `${buckets.paths} file path${buckets.paths === 1 ? '' : 's'}`, count: buckets.paths, source: 'rules' });
    if (buckets.urls > 0) out.push({ label: `${buckets.urls} URL${buckets.urls === 1 ? '' : 's'}`, count: buckets.urls, source: 'rules' });
    if (buckets.timestamps > 0) out.push({ label: `${buckets.timestamps} timestamps coarsened`, count: buckets.timestamps, source: 'rules' });
    if (buckets.other > 0) out.push({ label: `${buckets.other} other`, count: buckets.other, source: 'rules' });
  }
  const findings = data?.aiPiiFindings || [];
  if (findings.length > 0) {
    const byType: Record<string, number> = {};
    for (const f of findings) {
      const k = f.entity_type.replace(/_/g, ' ');
      byType[k] = (byType[k] || 0) + 1;
    }
    for (const [k, n] of Object.entries(byType).sort((a, b) => b[1] - a[1])) {
      out.push({ label: `${n} ${k}${n === 1 ? '' : 's'}`, count: n, source: 'ai' });
    }
  }
  return out;
}
