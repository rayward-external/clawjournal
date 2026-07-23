import type { Share as ShareType } from '../../types.ts';
import {
  CONFIDENCE_THRESHOLD,
  MAX_SHARE_QUEUE_SIZE,
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
  if (s.source === 'claude-science') return { label: 'Claude Science', color: '#9333ea' };
  if (s.source === 'openclaw') return { label: 'OpenClaw', color: '#6b7280' };
  if (s.source === 'workbuddy') return { label: 'WorkBuddy', color: '#0f766e' };
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
  if (b === 'interrupted') return '~ interrupted';
  if (b === 'inconclusive') return '— inconclusive';
  if (b === 'failed') return '✗ failed';
  if (b === 'abandoned') return '✗ abandoned';
  if (b === 'exploratory') return '— exploratory';
  if (b === 'trivial') return '— trivial';
  if (b === 'unscored') return '— unscored';
  if (b === 'unknown') return '— unknown';
  if (b.includes('pass')) return '✓ passed';
  if (b.includes('fail')) return '✗ failed';
  if (b.includes('analysis')) return '— analysis';
  if (b.includes('completed')) return '— completed';
  if (b.includes('errored')) return '✗ errored';
  return '';
}

// Plain-English gloss for each outcome badge — surfaced as a hover tooltip so
// users can tell the similar-looking states apart (notably failed vs errored).
// "Outcome" describes how the session ended; it is a label, not a score.
export function outcomeTooltip(outcome: string | null): string {
  if (!outcome) return '';
  const b = outcome.toLowerCase();
  if (b === 'resolved') return 'Outcome: the task was resolved successfully.';
  if (b === 'partial') return 'Outcome: useful progress was made but the task was not fully completed.';
  if (b === 'interrupted') return 'Outcome: interrupted — the user spoke last and the agent never replied.';
  if (b === 'inconclusive') return 'Outcome: inconclusive — the session ended without a decisive success or failure signal.';
  if (b === 'abandoned') return 'Outcome: abandoned before reaching a result.';
  if (b === 'exploratory') return 'Outcome: exploratory — no concrete change was expected.';
  if (b === 'trivial') return 'Outcome: trivial — minimal work.';
  if (b === 'unscored') return 'Outcome: unscored — no outcome signal is available yet.';
  if (b === 'unknown') return 'Outcome: unknown — the stored outcome was not recognized.';
  if (b.includes('pass')) return 'Outcome: a test run reported passing tests.';
  if (b.includes('fail')) return 'Outcome: a test or build explicitly failed.';
  if (b.includes('analysis')) return 'Outcome: analysis only — no code changes.';
  if (b.includes('completed')) return 'Outcome: ran to the end without a decisive success or failure signal.';
  if (b.includes('errored')) return 'Outcome: hit a runtime error (exception, traceback, etc.) near the end — distinct from a test/build failure.';
  return 'How this session ended (heuristic, not a score).';
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
  const recommended = (stats.recommended_session_ids || [])
    .filter((id) => validIds.has(id));

  // Start with the server's highest-value recommendations, then fill the
  // bounded queue from the remaining eligible traces. The complete eligible
  // pool stays available in the picker, but a share never starts with the
  // user's entire local history selected.
  return [...new Set([
    ...recommended,
    ...stats.sessions.map((s) => s.session_id).filter(Boolean),
  ])].slice(0, MAX_SHARE_QUEUE_SIZE);
}

function csvParam(params: URLSearchParams, name: string): string[] | null {
  if (!params.has(name)) return null;
  return (params.get(name) || '').split(',').filter(Boolean);
}

export function queueFromSelectionParams(
  stats: ShareReadyStats,
  params: URLSearchParams,
): string[] {
  // `ids=` is intentionally meaningful: it represents an explicitly empty
  // queue and must not fall back to the select-all default on reload.
  const explicitIds = csvParam(params, 'ids');
  if (explicitIds !== null) {
    return sanitizeQueueSelection(stats, explicitIds);
  }

  const excludedIds = new Set([
    ...(csvParam(params, 'exclude') || []),
    ...(csvParam(params, 'exclude_ids') || []),
  ]);
  return queueFromStats(stats).filter((id) => !excludedIds.has(id));
}

export function sanitizeQueueSelection(
  stats: ShareReadyStats,
  selection: string[],
): string[] {
  const eligibleIds = new Set(stats.sessions.map((session) => session.session_id));
  const seen = new Set<string>();
  const sanitized: string[] = [];
  for (const id of selection) {
    if (!eligibleIds.has(id) || seen.has(id)) continue;
    seen.add(id);
    sanitized.push(id);
    if (sanitized.length >= MAX_SHARE_QUEUE_SIZE) break;
  }
  return sanitized;
}

export function hasLockedQueueSelection(params: URLSearchParams): boolean {
  // A downstream step may only consume an exact, explicitly locked snapshot.
  // Relative exclusions could silently absorb newly eligible traces after a
  // reload. Exactly one exact carrier must be present; queue_ref points to a
  // local-only exact snapshot and is validated by queueState before use.
  const exactCarrierCount = Number(params.has('ids')) + Number(params.has('queue_ref'));
  return params.get('selection') === 'locked'
    && exactCarrierCount === 1
    && !params.has('exclude')
    && !params.has('exclude_ids');
}

export function writeQueueSelectionParams(
  params: URLSearchParams,
  stats: ShareReadyStats | null,
  queueOrder: string[],
  locked = false,
): void {
  // Until readyStats arrives we cannot tell default-all from an explicit
  // subset, so leave the selection encoding untouched.
  if (!stats) return;

  if (locked) {
    // Once Queue is confirmed, materialize the complete ordered set. Never
    // re-derive a downstream selection from future share-ready responses.
    params.set('ids', queueOrder.join(','));
    params.delete('exclude_ids');
    params.set('selection', 'locked');
    return;
  }

  params.delete('selection');

  const defaults = queueFromStats(stats);
  const selected = new Set(queueOrder);
  const excluded = defaults.filter((id) => !selected.has(id));
  const defaultOrderedSubset = defaults.filter((id) => selected.has(id));
  const sameOrder = defaultOrderedSubset.length === queueOrder.length
    && defaultOrderedSubset.every((id, index) => id === queueOrder[index]);

  if (sameOrder && excluded.length === 0) {
    // The common default needs no URL payload at all.
    params.delete('ids');
    params.delete('exclude_ids');
  } else if (queueOrder.length === 0) {
    params.set('ids', '');
    params.delete('exclude_ids');
  } else if (sameOrder && excluded.length < queueOrder.length) {
    // A small set of user deselections stays compact even for large queues.
    params.delete('ids');
    params.set('exclude_ids', excluded.join(','));
  } else {
    // Explicit/reordered subsets retain their exact order for deep links.
    params.set('ids', queueOrder.join(','));
    params.delete('exclude_ids');
  }
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
  if (d.aiCoverage === 'rules_only' || d.aiCoverage === 'disabled') return 'review';
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
