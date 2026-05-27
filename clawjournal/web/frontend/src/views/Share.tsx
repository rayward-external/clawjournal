import { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import type { Session, Share as ShareType, ToolUse } from '../types.ts';
import { api } from '../api.ts';
import { useToast } from '../components/Toast.tsx';
import { Spinner } from '../components/Spinner.tsx';
import { RedactedText } from '../components/RedactedText.tsx';
import { SessionDrawer } from '../components/SessionDrawer.tsx';
import { ToolUseCard } from '../components/ToolUseCard.tsx';
import { Stepper } from '../components/Stepper.tsx';
import { TraceCard } from '../components/TraceCard.tsx';
import { colors } from '../theme.ts';

// ============================================================
// Helpers
// ============================================================

function hexAlpha(hex: string, alpha: number): string {
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return `rgba(${r},${g},${b},${alpha})`;
}

function sourceFullLabel(s: { source: string; client_origin?: string | null; runtime_channel?: string | null }): { label: string; color: string } {
  if (s.source === 'codex') return s.client_origin === 'desktop' ? { label: 'Codex Desktop', color: '#0891b2' } : { label: 'Codex', color: '#16a34a' };
  if (s.source === 'claude') {
    if (s.client_origin === 'desktop' || s.runtime_channel === 'local-agent') return { label: 'Claude Desktop', color: '#7c3aed' };
    return { label: 'Claude Code', color: '#d97706' };
  }
  if (s.source === 'openclaw') return { label: 'OpenClaw', color: '#6b7280' };
  return { label: s.source, color: '#6b7280' };
}

function SourceBadge({ s }: { s: { source: string; client_origin?: string | null; runtime_channel?: string | null } }) {
  const { label, color } = sourceFullLabel(s);
  return <span style={{ fontSize: 10, fontWeight: 600, padding: '1px 6px', borderRadius: 4, background: hexAlpha(color, 0.10), color, marginRight: 3 }}>{label}</span>;
}

function ThinkingBlock({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div style={{ margin: '6px 0' }}>
      <button
        onClick={() => setOpen(!open)}
        style={{
          background: 'none', border: 'none', color: colors.gray500,
          cursor: 'pointer', fontSize: 13, padding: 0, textDecoration: 'underline',
        }}
      >
        {open ? 'Hide thinking' : 'Show thinking'}
      </button>
      {open && (
        <pre style={{
          background: colors.yellow50, border: `1px solid ${colors.yellow200}`,
          borderRadius: 6, padding: 10, fontSize: 13,
          whiteSpace: 'pre-wrap', wordBreak: 'break-word',
          marginTop: 4, maxHeight: 300, overflow: 'auto',
        }}>
          {text}
        </pre>
      )}
    </div>
  );
}

function autoDescription(share: ShareType): string {
  if (share.submission_note) return share.submission_note;
  if (share.sessions && share.sessions.length > 0) {
    const projects = [...new Set(share.sessions.map(s => s.project).filter(Boolean))].slice(0, 3);
    if (projects.length > 0) return `${share.session_count} sessions from ${projects.join(', ')}`;
  }
  return `${share.session_count} sessions`;
}

function formatDate(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

function outcomeBadge(outcome: string | null): string {
  if (!outcome) return '';
  const b = outcome.toLowerCase();
  if (b === 'resolved') return '\u2713 resolved';
  if (b === 'partial') return '~ partial';
  if (b === 'failed') return '\u2717 failed';
  if (b === 'abandoned') return '\u2717 abandoned';
  if (b === 'exploratory') return '\u2014 exploratory';
  if (b === 'trivial') return '\u2014 trivial';
  if (b.includes('pass')) return '\u2713 passed';
  if (b.includes('fail')) return '\u2717 failed';
  if (b.includes('analysis')) return '\u2014 analysis';
  if (b.includes('completed')) return '\u2713 completed';
  if (b.includes('errored')) return '\u2717 errored';
  return '';
}

const formatTokens = (t: number) => t >= 1_000_000 ? `${(t / 1_000_000).toFixed(1)}M` : `${(t / 1000).toFixed(0)}k`;

// Matches SessionDetail's formula: input + output (cache tokens excluded).
const sessionTotalTokens = (s: { input_tokens?: number; output_tokens?: number }) =>
  (s.input_tokens || 0) + (s.output_tokens || 0);

// Map raw redaction-log `type` strings into a small set of buckets the UI
// surfaces on Redact and Review. Everything else collapses to `other`.
type RedactionBucket = 'tokens' | 'emails' | 'paths' | 'timestamps' | 'urls' | 'other';
function bucketOf(type: string): RedactionBucket {
  const t = type.toLowerCase();
  if (t.includes('email')) return 'emails';
  if (t.includes('url')) return 'urls';
  if (t.includes('path') || t.includes('username') || t.includes('home')) return 'paths';
  if (t.includes('time') || t.includes('date')) return 'timestamps';
  if (t.startsWith('trufflehog')) return 'tokens';
  if (t.includes('token') || t.includes('key') || t.includes('secret') || t.includes('jwt') || t.includes('cred') || t.includes('auth')) return 'tokens';
  return 'other';
}

interface BucketCounts {
  tokens: number;
  emails: number;
  paths: number;
  timestamps: number;
  urls: number;
  other: number;
}

const emptyBuckets = (): BucketCounts => ({ tokens: 0, emails: 0, paths: 0, timestamps: 0, urls: 0, other: 0 });
const SHARE_SHELL_WIDTH = '1120px';

// ============================================================
// Types
// ============================================================

type StepKey = 'queue' | 'redact' | 'review' | 'package' | 'done';

const STEPS = [
  { key: 'queue', label: 'Queue' },
  { key: 'redact', label: 'Redact' },
  { key: 'review', label: 'Review' },
  { key: 'package', label: 'Package' },
  { key: 'done', label: 'Done' },
];

interface ReadySession {
  session_id: string;
  project: string;
  model: string | null;
  source: string;
  display_title: string;
  ai_quality_score: number | null;
  ai_failure_value_score: number | null;
  ai_recovery_labels: string[];
  ai_failure_attribution: string | null;
  ai_failure_modes: string[];
  ai_learning_summary: string | null;
  user_messages: number;
  assistant_messages: number;
  tool_uses: number;
  input_tokens: number;
  output_tokens: number;
  outcome_badge: string | null;
  client_origin?: string | null;
  runtime_channel?: string | null;
  start_time?: string | null;
  review_status?: string;
}

interface ShareReadyStats {
  count: number;
  total_approved: number;
  projects: string[];
  models: string[];
  recommended_session_ids: string[];
  sessions: ReadySession[];
}

interface RedactedReviewMessage {
  role: 'user' | 'assistant' | 'system';
  content: string;
  thinking?: string;
  tool_uses?: ToolUse[];
  timestamp?: string;
}

interface AiPiiFindingLocal {
  entity_type: string;
  entity_text: string;
  confidence: number;
  field: string;
  source: string;
}

interface RedactedSessionData {
  messages: RedactedReviewMessage[];
  loading: boolean;
  redactionCount?: number;
  aiPiiFindings?: AiPiiFindingLocal[];
  aiCoverage?: 'full' | 'rules_only';
  buckets?: BucketCounts;
  trufflehogHits?: number;
}

const CONFIDENCE_THRESHOLD = 0.85;

function classify(d: RedactedSessionData | undefined): 'checking' | 'clear' | 'review' {
  if (!d || d.loading) return 'checking';
  if (d.aiCoverage === 'rules_only') return 'review';
  const lowConf = (d.aiPiiFindings || []).some(f => f.confidence < CONFIDENCE_THRESHOLD);
  if (lowConf) return 'review';
  return 'clear';
}

// ============================================================
// Icons (inline SVG, current light theme)
// ============================================================

function Icon({ name, size = 16 }: { name: string; size?: number }) {
  const common = { width: size, height: size, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', strokeWidth: 1.7, strokeLinecap: 'round' as const, strokeLinejoin: 'round' as const };
  switch (name) {
    case 'grip':
      return (<svg {...common}><circle cx="9" cy="6" r="1" fill="currentColor" /><circle cx="9" cy="12" r="1" fill="currentColor" /><circle cx="9" cy="18" r="1" fill="currentColor" /><circle cx="15" cy="6" r="1" fill="currentColor" /><circle cx="15" cy="12" r="1" fill="currentColor" /><circle cx="15" cy="18" r="1" fill="currentColor" /></svg>);
    case 'check':
      return (<svg {...common}><path d="M5 12l4 4 10-10" /></svg>);
    case 'x':
      return (<svg {...common}><path d="M6 6l12 12" /><path d="M18 6L6 18" /></svg>);
    case 'plus':
      return (<svg {...common}><path d="M12 5v14" /><path d="M5 12h14" /></svg>);
    case 'info':
      return (<svg {...common}><circle cx="12" cy="12" r="9" /><path d="M12 11v5" /><circle cx="12" cy="8" r="0.6" fill="currentColor" /></svg>);
    case 'download':
      return (<svg {...common}><path d="M12 4v12" /><path d="M7 11l5 5 5-5" /><path d="M5 20h14" /></svg>);
    case 'inbox':
      return (<svg {...common}><path d="M4 14h4l1 3h6l1-3h4" /><path d="M4 14l3-8h10l3 8v6H4z" /></svg>);
    case 'retry':
      return (<svg {...common}><path d="M3 12a9 9 0 1 0 3-6.7" /><path d="M3 4v5h5" /></svg>);
    case 'lock':
      return (<svg {...common}><rect x="5" y="11" width="14" height="9" rx="2" /><path d="M8 11V8a4 4 0 1 1 8 0v3" /></svg>);
    case 'sparkle':
      return (<svg {...common}><path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5L18 18M18 6l-2.5 2.5M8.5 15.5L6 18" /></svg>);
    case 'alert':
      return (<svg {...common}><path d="M12 3l10 18H2z" /><path d="M12 10v5" /><circle cx="12" cy="18" r="0.6" fill="currentColor" /></svg>);
    case 'chevron':
      return (<svg {...common}><path d="M6 9l6 6 6-6" /></svg>);
    case 'shield':
      return (<svg {...common}><path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z" /><path d="M9 12l2 2 4-4" /></svg>);
    case 'chart':
      return (<svg {...common}><path d="M3 20h18" /><rect x="5" y="12" width="3" height="6" /><rect x="10" y="7" width="3" height="11" /><rect x="15" y="3" width="3" height="15" /></svg>);
    default: return null;
  }
}

// ============================================================
// Shared UI bits
// ============================================================

function TrustChip({ icon, title, subtitle }: { icon: string; title: string; subtitle: string }) {
  return (
    <div style={{
      display: 'flex', alignItems: 'center', gap: 10,
      padding: '8px 12px', flex: 1, minWidth: 0,
      background: colors.white, border: `1px solid ${colors.primary200}`,
      borderRadius: 8,
    }}>
      <div style={{
        width: 28, height: 28, borderRadius: 8,
        background: colors.primary50, color: colors.primary500,
        display: 'grid', placeItems: 'center', flexShrink: 0,
      }}>
        <Icon name={icon} size={15} />
      </div>
      <div style={{ minWidth: 0 }}>
        <div style={{ fontSize: 12.5, fontWeight: 600, color: colors.gray900, whiteSpace: 'nowrap' }}>{title}</div>
        <div style={{
          fontSize: 11.5, color: colors.gray500, marginTop: 1,
          whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
        }}>{subtitle}</div>
      </div>
    </div>
  );
}

function UsageDisclosure({ onLearnMore }: { onLearnMore?: () => void }) {
  return (
    <div style={{
      display: 'flex', alignItems: 'stretch', gap: 8,
      padding: 10, marginBottom: 18,
      background: colors.primary50, border: `1px solid ${colors.primary200}`,
      borderRadius: 10, flexWrap: 'wrap',
    }}>
      <TrustChip icon="shield" title="Local only" subtitle="Redaction runs on your device" />
      <TrustChip icon="sparkle" title="Rules + AI redact" subtitle="Deterministic rules, then AI" />
      <TrustChip icon="chart" title="Eval & training only" subtitle="No ads, no resale, no profiling" />
      {onLearnMore && (
        <button
          onClick={onLearnMore}
          style={{
            display: 'inline-flex', alignItems: 'center', gap: 4,
            padding: '0 10px', color: colors.primary500,
            fontSize: 12, fontWeight: 500, border: 'none',
            background: 'transparent', cursor: 'pointer', whiteSpace: 'nowrap',
          }}
        >
          Learn more <Icon name="chevron" size={11} />
        </button>
      )}
    </div>
  );
}

function HelpModal({ onClose }: { onClose: () => void }) {
  const stages = [
    {
      num: 1,
      name: 'Deterministic rules',
      sub: 'Always on',
      desc: 'API keys, tokens, JWTs, private keys, database URLs, email addresses, user paths, and precise timestamps are removed first.',
    },
    {
      num: 2,
      name: 'Policy rules',
      sub: 'Configurable',
      desc: 'Custom strings, extra usernames, blocked domains, excluded projects, and an allowlist. Configured under Policies.',
    },
    {
      num: 3,
      name: 'AI-assisted review',
      sub: 'First line of judgement',
      desc: 'Names, orgs, private project names, and contextual identifiers are flagged. If AI is unavailable, the trace falls back to rules-only and you\u2019ll see a labeled reason.',
      accent: colors.primary500,
      accentBg: colors.primary100,
    },
    {
      num: 4,
      name: 'Your review',
      sub: 'Only when flagged',
      desc: 'Only traces the AI wasn\u2019t confident about reach you. Everything else clears automatically.',
      accent: colors.yellow400,
      accentBg: colors.yellow100,
    },
  ];
  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed', inset: 0, background: 'rgba(27,26,23,0.45)',
        backdropFilter: 'blur(3px)', display: 'grid', placeItems: 'center',
        zIndex: 100, animation: 'clawFadeIn 180ms ease both',
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          width: 'min(640px, 94vw)', background: colors.white,
          border: `1px solid ${colors.gray200}`, borderRadius: 14,
          padding: '24px 26px 26px', boxShadow: '0 18px 40px -12px rgba(0,0,0,0.25)',
          maxHeight: '88vh', overflow: 'auto', position: 'relative',
        }}
      >
        <button
          onClick={onClose}
          style={{
            position: 'absolute', top: 12, right: 12, padding: '6px 8px',
            background: 'transparent', border: 'none', cursor: 'pointer', color: colors.gray500,
            borderRadius: 6,
          }}
          title="Close"
        >
          <Icon name="x" size={14} />
        </button>
        <h3 style={{ margin: '0 0 4px', fontSize: 17, fontWeight: 600, color: colors.gray900 }}>How redaction works</h3>
        <p style={{ margin: '0 0 18px', color: colors.gray500, fontSize: 13 }}>
          Four layers sit between your raw local trace and the redacted zip you download.
        </p>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {stages.map((s, i) => (
            <div key={s.num} style={{
              display: 'grid', gridTemplateColumns: '28px 140px 1fr', gap: 12,
              padding: 12, background: colors.gray50,
              border: `1px solid ${colors.gray200}`, borderRadius: 8,
              position: 'relative',
            }}>
              {i < stages.length - 1 && (
                <span style={{
                  position: 'absolute', left: 26, top: '100%',
                  width: 1, height: 8, background: colors.gray300,
                }} />
              )}
              <div style={{
                width: 24, height: 24, borderRadius: '50%',
                background: s.accentBg || colors.white,
                color: s.accent || colors.gray500,
                border: s.accent ? 'none' : `1px solid ${colors.gray300}`,
                display: 'grid', placeItems: 'center', fontSize: 12,
                fontWeight: 600, fontVariantNumeric: 'tabular-nums',
              }}>{s.num}</div>
              <div style={{ fontSize: 13, color: colors.gray900, fontWeight: 500 }}>
                {s.name}
                <div style={{ fontSize: 11, color: colors.gray400, fontWeight: 400, marginTop: 2 }}>{s.sub}</div>
              </div>
              <div style={{ fontSize: 12.5, color: colors.gray600, lineHeight: 1.55 }}>{s.desc}</div>
            </div>
          ))}
        </div>
        <div style={{
          marginTop: 14, padding: '10px 12px',
          border: `1px dashed ${colors.gray300}`, borderRadius: 6,
          fontSize: 12, color: colors.gray500, fontStyle: 'italic', textAlign: 'center',
        }}>
          Your original trace stays in the local workbench. The zip file is a separate, redacted copy.
        </div>
        <div style={{ marginTop: 14, textAlign: 'right' }}>
          <Link to="/share/rules" style={{ fontSize: 12.5, color: colors.primary500, textDecoration: 'none' }}>
            Edit redaction policies &rarr;
          </Link>
        </div>
      </div>
    </div>
  );
}

// ============================================================
// Style helpers
// ============================================================

const btnPrimary = {
  display: 'inline-flex', alignItems: 'center', gap: 8,
  padding: '9px 18px', background: colors.gray900, color: colors.white,
  border: 'none', borderRadius: 8, fontSize: 13, fontWeight: 500,
  cursor: 'pointer', whiteSpace: 'nowrap' as const,
};

const btnSecondary = {
  display: 'inline-flex', alignItems: 'center', gap: 8,
  padding: '8px 14px', background: colors.white, color: colors.gray700,
  border: `1px solid ${colors.gray300}`, borderRadius: 8, fontSize: 13, fontWeight: 500,
  cursor: 'pointer', whiteSpace: 'nowrap' as const,
};

const btnGhost = {
  display: 'inline-flex', alignItems: 'center', gap: 6,
  padding: '5px 10px', background: 'transparent', color: colors.gray600,
  border: 'none', borderRadius: 6, fontSize: 12, fontWeight: 500,
  cursor: 'pointer',
};

// Status dot (prototype: green/amber/neutral with soft halo)
function StatusDot({ status }: { status: 'checking' | 'clear' | 'review' }) {
  const palette = status === 'clear'
    ? { dot: colors.green500, halo: colors.green100 }
    : status === 'review'
      ? { dot: colors.yellow400, halo: colors.yellow100 }
      : { dot: colors.gray400, halo: colors.gray200 };
  return (
    <span
      style={{
        display: 'inline-block', width: 10, height: 10, borderRadius: '50%',
        background: palette.dot, boxShadow: `0 0 0 3px ${palette.halo}`,
        flexShrink: 0, position: 'relative',
      }}
      aria-label={status}
    >
      {status === 'checking' && (
        <span
          style={{
            position: 'absolute', inset: -4, borderRadius: '50%',
            border: `1.5px solid ${palette.dot}`, borderTopColor: 'transparent',
            animation: 'clawSpin 900ms linear infinite',
          }}
        />
      )}
    </span>
  );
}

// ============================================================
// Main component
// ============================================================

export function Share() {
  const { toast } = useToast();
  const [searchParams, setSearchParams] = useSearchParams();

  const [activeStep, setActiveStep] = useState<StepKey>(
    () => (searchParams.get('step') as StepKey) || 'queue',
  );
  const [completedKeys, setCompletedKeys] = useState<Set<string>>(() => {
    const step = (searchParams.get('step') as StepKey) || 'queue';
    const idx = STEPS.findIndex((s) => s.key === step);
    return new Set(STEPS.slice(0, Math.max(0, idx)).map((s) => s.key));
  });

  const [readyStats, setReadyStats] = useState<ShareReadyStats | null>(null);
  const [shares, setShares] = useState<ShareType[]>([]);

  // Queue state: ordered list (drag-reorder) with a derived Set for lookups.
  const [queueOrder, setQueueOrder] = useState<string[]>(() => {
    const csv = searchParams.get('ids');
    return csv ? csv.split(',').filter(Boolean) : [];
  });
  const [selectionInitialized, setSelectionInitialized] = useState(() => !!searchParams.get('ids'));
  const queueSet = useMemo(() => new Set(queueOrder), [queueOrder]);

  const [note, setNote] = useState(() => searchParams.get('note') || '');
  const [drawerSessionId, setDrawerSessionId] = useState<string | null>(null);
  const [showAddTraces, setShowAddTraces] = useState(false);
  const [showHelp, setShowHelp] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  // Add-traces filter
  const [searchQuery, setSearchQuery] = useState('');
  const [sourceFilter, setSourceFilter] = useState('');
  const [projectFilter, setProjectFilter] = useState('');
  const [scoreFilter, setScoreFilter] = useState(0);
  const [dateFilter, setDateFilter] = useState('');

  // Redaction state
  const [redactedSessions, setRedactedSessions] = useState<Record<string, RedactedSessionData>>({});

  // Review state
  const [approvedIds, setApprovedIds] = useState<Set<string>>(new Set());
  const [expandedReviewIds, setExpandedReviewIds] = useState<Set<string>>(new Set());

  // Package state
  const [packagedShareId, setPackagedShareId] = useState<string | null>(
    () => searchParams.get('share'),
  );
  const [packageProgress, setPackageProgress] = useState(0);
  const [packageLog, setPackageLog] = useState('');
  const [packagingFailed, setPackagingFailed] = useState<string | null>(null);

  // Done state
  const [bundleInfo, setBundleInfo] = useState<{ traces: number; created: string; approxSize: string } | null>(null);

  // Candidates (empty queue hint)
  const [candidates, setCandidates] = useState<Session[]>([]);
  const [scoringBackend, setScoringBackend] = useState<{ backend: string | null; display_name: string | null } | null>(null);

  // =================================================
  // Initial load
  // =================================================

  useEffect(() => {
    Promise.all([
      api.shareReady({ includeUnapproved: true }),
      api.shares.list(),
      api.scoringBackend().catch(() => ({ backend: null, display_name: null })),
    ]).then(([stats, shareList, backend]) => {
      setReadyStats(stats);
      setShares(shareList);
      setScoringBackend(backend);
      if (!selectionInitialized && stats.sessions.length > 0) {
        // Server recommendation is approved high-value failure traces, capped
        // at 5. Trust it and only filter out ids the
        // client can't resolve (eg. excluded projects).
        const validIds = new Set(stats.sessions.map((s) => s.session_id));
        const recommended = (stats.recommended_session_ids || [])
          .filter((id) => validIds.has(id))
          .slice(0, 5);
        setQueueOrder(recommended);
        setSelectionInitialized(true);
      }
      if (stats.sessions.length === 0) {
        api.sessions.list({ status: 'new', sort: 'start_time', order: 'desc', limit: 10 })
          .then(setCandidates)
          .catch(() => setCandidates([]));
      }
      setLoading(false);
    }).catch((e) => {
      toast(e instanceof Error ? e.message : 'Failed to load data', 'error');
      setLoading(false);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // =================================================
  // URL sync
  // =================================================

  useEffect(() => {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      if (activeStep === 'queue') next.delete('step'); else next.set('step', activeStep);
      const csv = queueOrder.join(',');
      if (csv) next.set('ids', csv); else next.delete('ids');
      if (note) next.set('note', note); else next.delete('note');
      if (packagedShareId) next.set('share', packagedShareId); else next.delete('share');
      return next;
    }, { replace: true });
  }, [activeStep, queueOrder, note, packagedShareId, setSearchParams]);

  // Drop cached redacted entries when sessions leave the queue.
  useEffect(() => {
    setRedactedSessions((prev) => {
      let changed = false;
      const next: Record<string, RedactedSessionData> = {};
      for (const [sid, data] of Object.entries(prev)) {
        if (queueSet.has(sid)) next[sid] = data;
        else changed = true;
      }
      return changed ? next : prev;
    });
    setApprovedIds((prev) => {
      let changed = false;
      const next = new Set<string>();
      for (const id of prev) {
        if (queueSet.has(id)) next.add(id);
        else changed = true;
      }
      return changed ? next : prev;
    });
  }, [queueSet]);

  const reload = () => {
    api.shareReady({ includeUnapproved: true }).then((stats) => {
      setReadyStats(stats);
      if (stats.sessions.length === 0) {
        api.sessions.list({ status: 'new', sort: 'start_time', order: 'desc', limit: 10 })
          .then(setCandidates)
          .catch(() => { });
      } else {
        setCandidates([]);
      }
    }).catch(() => { });
    api.shares.list().then(setShares).catch(() => { });
  };

  const sessionById = useMemo(() => {
    const m: Record<string, ReadySession> = {};
    readyStats?.sessions.forEach((s) => { m[s.session_id] = s; });
    return m;
  }, [readyStats]);

  const queuedSessions = useMemo(
    () => queueOrder.map((id) => sessionById[id]).filter((s): s is ReadySession => !!s),
    [queueOrder, sessionById],
  );

  // =================================================
  // Queue actions
  // =================================================

  const removeFromQueue = (id: string) => {
    setQueueOrder((prev) => prev.filter((x) => x !== id));
  };

  const addToQueue = (id: string) => {
    setQueueOrder((prev) => prev.includes(id) ? prev : [...prev, id]);
  };

  const reorderQueue = (fromId: string, overId: string) => {
    if (fromId === overId) return;
    setQueueOrder((prev) => {
      const fromIdx = prev.indexOf(fromId);
      const overIdx = prev.indexOf(overId);
      if (fromIdx === -1 || overIdx === -1) return prev;
      const next = [...prev];
      const [moved] = next.splice(fromIdx, 1);
      next.splice(overIdx, 0, moved);
      return next;
    });
  };

  const startFreshShare = useCallback(() => {
    setQueueOrder([]);
    setCompletedKeys(new Set());
    setPackagedShareId(null);
    setNote('');
    setRedactedSessions({});
    setApprovedIds(new Set());
    setExpandedReviewIds(new Set());
    setBundleInfo(null);
    setPackageProgress(0);
    setPackageLog('');
    setPackagingFailed(null);
    setActiveStep('queue');
  }, []);

  const onStepClick = (key: string) => {
    const k = key as StepKey;
    if (k === activeStep) return;
    if (k === 'queue') { setActiveStep('queue'); return; }
    if (completedKeys.has(k)) setActiveStep(k);
  };

  // =================================================
  // Step 2: Redact
  // =================================================

  // Serialize redaction: the local Claude CLI we call for AI PII review is
  // single-threaded per install and starts timing out under parallel load.
  // One trace at a time is slower wall-clock but dramatically cuts the
  // rules-only fallback rate. A single automatic retry on failure catches
  // transient timeouts without doubling the wait for the common case.
  const REDACTION_CONCURRENCY = 2;
  const REDACTION_RETRIES = 1;
  const redactionStartedRef = useRef(false);

  const runRedaction = useCallback(async () => {
    if (redactionStartedRef.current) return;
    redactionStartedRef.current = true;
    const sessions = queuedSessions;
    const cached = redactedSessions;
    const missing = sessions.filter((s) => !cached[s.session_id]);

    // mark missing ones as loading up-front so the per-trace list renders
    if (missing.length > 0) {
      setRedactedSessions((prev) => {
        const next = { ...prev };
        missing.forEach((s) => {
          if (!next[s.session_id]) next[s.session_id] = { messages: [], loading: true };
        });
        return next;
      });
    }

    const fetchReport = async (sessionId: string) => {
      let lastErr: unknown = null;
      for (let attempt = 0; attempt <= REDACTION_RETRIES; attempt++) {
        try {
          return await api.sessions.redactionReport(sessionId, { aiPii: true });
        } catch (e) {
          lastErr = e;
          if (attempt < REDACTION_RETRIES) {
            // brief pause to let a flaky CLI/model settle before retrying
            await new Promise((r) => setTimeout(r, 800));
          }
        }
      }
      throw lastErr;
    };

    const processOne = async (s: ReadySession) => {
      try {
        const report = await fetchReport(s.session_id);
        const msgs: RedactedReviewMessage[] = (report.redacted_session.messages || []).map((m) => ({
          role: m.role,
          content: m.content || '',
          thinking: m.thinking,
          tool_uses: m.tool_uses,
          timestamp: m.timestamp,
        }));
        const buckets = emptyBuckets();
        for (const entry of report.redaction_log || []) {
          buckets[bucketOf(entry.type)] += 1;
        }
        const trufflehogHits = (report.redaction_log || [])
          .filter((entry) => entry.type && entry.type.startsWith('trufflehog'))
          .length;
        setRedactedSessions((prev) => ({
          ...prev,
          [s.session_id]: {
            messages: msgs, loading: false,
            redactionCount: report.redaction_count,
            aiPiiFindings: report.ai_pii_findings || [],
            aiCoverage: report.ai_coverage || 'rules_only',
            buckets,
            trufflehogHits,
          },
        }));
      } catch {
        setRedactedSessions((prev) => ({
          ...prev,
          [s.session_id]: {
            messages: [{ role: 'system', content: '(unable to load redacted content)' }],
            loading: false,
            redactionCount: 0,
            aiCoverage: 'rules_only',
            buckets: emptyBuckets(),
          },
        }));
      }
    };

    for (let i = 0; i < missing.length; i += REDACTION_CONCURRENCY) {
      const batch = missing.slice(i, i + REDACTION_CONCURRENCY);
      await Promise.all(batch.map(processOne));
    }
    redactionStartedRef.current = false;
  }, [queuedSessions, redactedSessions]);

  const handleStartRedaction = () => {
    setCompletedKeys((prev) => new Set([...prev, 'queue']));
    setActiveStep('redact');
    void runRedaction();
  };

  // if Redact step is the active step and there are sessions lacking data, kick it off
  useEffect(() => {
    if (activeStep !== 'redact') return;
    const anyMissing = queuedSessions.some((s) => !redactedSessions[s.session_id] || redactedSessions[s.session_id].loading);
    if (anyMissing) void runRedaction();
  }, [activeStep, queuedSessions, redactedSessions, runRedaction]);

  const redactAllDone = queuedSessions.length > 0 && queuedSessions.every((s) => {
    const d = redactedSessions[s.session_id];
    return d && !d.loading;
  });

  const goToReview = () => {
    setCompletedKeys((prev) => new Set([...prev, 'queue', 'redact']));
    // Auto-expand the first unapproved trace for immediate attention.
    const firstUnapproved = queuedSessions.find((s) => !approvedIds.has(s.session_id));
    if (firstUnapproved) {
      setExpandedReviewIds(new Set([firstUnapproved.session_id]));
    }
    setActiveStep('review');
  };

  // =================================================
  // Step 3: Review
  // =================================================

  const approveTrace = (id: string) => {
    setApprovedIds((prev) => new Set([...prev, id]));
    // auto-advance: collapse this row, expand next unapproved
    setExpandedReviewIds((prev) => {
      const n = new Set(prev);
      n.delete(id);
      const currentIdx = queuedSessions.findIndex((s) => s.session_id === id);
      for (let i = 1; i <= queuedSessions.length; i++) {
        const next = queuedSessions[(currentIdx + i) % queuedSessions.length];
        if (!next) break;
        if (!approvedIds.has(next.session_id) && next.session_id !== id) {
          n.add(next.session_id);
          break;
        }
      }
      return n;
    });
  };

  const approveAllClean = () => {
    setApprovedIds((prev) => {
      const n = new Set(prev);
      queuedSessions.forEach((s) => {
        if (classify(redactedSessions[s.session_id]) === 'clear') n.add(s.session_id);
      });
      return n;
    });
  };

  const retryAiReview = async (id: string) => {
    setRedactedSessions((prev) => ({
      ...prev,
      [id]: { ...(prev[id] || { messages: [] }), loading: true },
    }));
    // One automatic retry mirrors the initial-run policy.
    let report: Awaited<ReturnType<typeof api.sessions.redactionReport>> | null = null;
    for (let attempt = 0; attempt <= 1; attempt++) {
      try {
        report = await api.sessions.redactionReport(id, { aiPii: true });
        break;
      } catch {
        if (attempt === 0) await new Promise((r) => setTimeout(r, 800));
      }
    }
    if (!report) {
      toast('AI review retry failed', 'error');
      setRedactedSessions((prev) => ({
        ...prev,
        [id]: { ...(prev[id] || { messages: [] }), loading: false },
      }));
      return;
    }
    const msgs: RedactedReviewMessage[] = (report.redacted_session.messages || []).map((m) => ({
      role: m.role,
      content: m.content || '',
      thinking: m.thinking,
      tool_uses: m.tool_uses,
      timestamp: m.timestamp,
    }));
    const buckets = emptyBuckets();
    for (const entry of report.redaction_log || []) buckets[bucketOf(entry.type)] += 1;
    const trufflehogHits = (report.redaction_log || [])
      .filter((entry) => entry.type && entry.type.startsWith('trufflehog'))
      .length;
    setRedactedSessions((prev) => ({
      ...prev,
      [id]: {
        messages: msgs, loading: false,
        redactionCount: report.redaction_count,
        aiPiiFindings: report.ai_pii_findings || [],
        aiCoverage: report.ai_coverage || 'rules_only',
        buckets,
        trufflehogHits,
      },
    }));
  };

  const toggleReviewExpand = (id: string) => {
    setExpandedReviewIds((prev) => {
      const n = new Set(prev);
      if (n.has(id)) n.delete(id); else n.add(id);
      return n;
    });
  };

  // =================================================
  // Step 4: Package
  // =================================================

  const packagingStartedRef = useRef(false);

  const runPackage = useCallback(async () => {
    if (packagingStartedRef.current) return;
    packagingStartedRef.current = true;
    setPackagingFailed(null);
    setPackageProgress(0);
    setPackageLog('Allocating bundle...');
    setCompletedKeys((prev) => new Set([...prev, 'queue', 'redact', 'review']));

    const approvedList = queuedSessions.filter((s) => approvedIds.has(s.session_id));
    const logLines = [
      'Allocating bundle...',
      'Writing manifest.json...',
      ...approvedList.map((s) => `Adding ${s.session_id.slice(0, 10)}.jsonl...`),
      'Writing redaction-audit.json...',
      'Compressing...',
      'Sealing bundle...',
    ];

    const animStart = Date.now();
    const duration = 2200 + approvedList.length * 220;

    const timers: number[] = [];
    logLines.forEach((line, i) => {
      timers.push(window.setTimeout(() => setPackageLog(line), (duration / logLines.length) * i));
    });
    const tick = window.setInterval(() => {
      const elapsed = Date.now() - animStart;
      setPackageProgress(Math.min(95, Math.round((elapsed / duration) * 95)));
    }, 60);

    const clearAllTimers = () => {
      window.clearInterval(tick);
      timers.forEach((t) => window.clearTimeout(t));
    };

    try {
      const ids = approvedList.map((s) => s.session_id);
      const { share_id } = await api.shares.create(ids, note || undefined);
      await api.shares.export(share_id);

      // Finish the animation cleanly before exposing the share id — the
      // `packageProgress >= 100 && packagedShareId` combination triggers the
      // fallback useEffect that flips the stepper to Done.
      const animRemaining = Math.max(0, duration - (Date.now() - animStart));
      await new Promise((r) => window.setTimeout(r, animRemaining));
      clearAllTimers();
      setPackageProgress(100);
      setPackageLog('Done.');

      // Bundle info — use `undefined` locale (empty-array form is a known
      // source of RangeError in some Intl configs).
      const totalTokens = approvedList.reduce((sum, s) => sum + sessionTotalTokens(s), 0);
      const approxMB = Math.max(0.1, (totalTokens * 0.3) / (1024 * 1024));
      try {
        setBundleInfo({
          traces: approvedList.length,
          created: new Date().toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' }),
          approxSize: approxMB >= 1 ? `${approxMB.toFixed(1)} MB` : `${(approxMB * 1024).toFixed(0)} KB`,
        });
      } catch {
        setBundleInfo({
          traces: approvedList.length,
          created: new Date().toISOString().slice(11, 16),
          approxSize: approxMB >= 1 ? `${approxMB.toFixed(1)} MB` : `${(approxMB * 1024).toFixed(0)} KB`,
        });
      }

      // Setting `packagedShareId` last — the fallback useEffect watches this
      // to force the step transition, so it must come after every other
      // success-path state update.
      setPackagedShareId(share_id);

      // Best-effort browser download + refresh. None of these should block
      // the step transition.
      try { await api.shares.download(share_id); } catch { /* user can still click Download again */ }
      try { reload(); } catch { /* ignore */ }
      try { toast('Bundle ready', 'success'); } catch { /* ignore */ }
    } catch (err: unknown) {
      clearAllTimers();
      const msg = err instanceof Error ? err.message : 'Package failed';
      setError(msg);
      setPackagingFailed(msg);
      setPackageLog(`Failed: ${msg}`);
      toast(msg, 'error');
    } finally {
      packagingStartedRef.current = false;
    }
  }, [queuedSessions, approvedIds, note, toast]);

  const handleStartPackage = () => {
    if (queuedSessions.length === 0) return;
    setCompletedKeys((prev) => new Set([...prev, 'queue', 'redact', 'review']));
    setActiveStep('package');
    void runPackage();
  };

  // kick off packaging when the step becomes active (eg. back-forward)
  useEffect(() => {
    if (activeStep === 'package' && !packagingStartedRef.current && !packagedShareId) {
      void runPackage();
    }
  }, [activeStep, packagedShareId, runPackage]);

  // Belt-and-suspenders: advance to Done once the share id lands and the
  // animation has finished. Runs even if the inline `setActiveStep('done')`
  // inside `runPackage` fell through a silent error path.
  useEffect(() => {
    if (activeStep === 'package' && packagedShareId && packageProgress >= 100 && !packagingFailed) {
      setCompletedKeys((prev) => new Set([...prev, 'package']));
      setActiveStep('done');
    }
  }, [activeStep, packagedShareId, packageProgress, packagingFailed]);

  // =================================================
  // Step 5: Done actions
  // =================================================

  const handleDownloadZip = async () => {
    if (!packagedShareId) return;
    try {
      await api.shares.download(packagedShareId);
      toast('Download started', 'success');
    } catch (err: unknown) {
      toast(err instanceof Error ? err.message : 'Download failed', 'error');
    }
  };

  // =================================================
  // Render
  // =================================================

  if (loading) {
    return <div style={{ padding: '32px 24px 48px', maxWidth: SHARE_SHELL_WIDTH, margin: '0 auto' }}>
      <Spinner text="Loading share data..." />
    </div>;
  }

  const stepperHeader = (
    <Stepper
      steps={STEPS}
      activeKey={activeStep}
      completedKeys={completedKeys}
      onStepClick={onStepClick}
    />
  );

  const globalStyles = (
    <style>{`
      @keyframes clawSpin { to { transform: rotate(360deg); } }
      @keyframes clawFadeIn { from { opacity: 0 } to { opacity: 1 } }
      @keyframes clawChipPop { from { opacity: 0; transform: translateY(4px) scale(.85); } to { opacity: 1; transform: translateY(0) scale(1); } }
      @keyframes clawRingOut { to { transform: scale(2); opacity: 0; } }
      @keyframes clawCheckIn { from { transform: scale(0); opacity: 0; } to { transform: scale(1); opacity: 1; } }
      @keyframes clawConfetti { 0% { transform: translate(0,0) rotate(0); opacity: 1; } 100% { transform: translate(var(--cdx), var(--cdy)) rotate(var(--cr)); opacity: 0; } }
      @keyframes clawPkgDrop { 0% { opacity: 0; transform: translate(-50%,-30px) scale(.85); } 15% { opacity: 1; } 70% { opacity: 1; transform: translate(-50%, 80px) scale(.85); } 100% { opacity: 0; transform: translate(-50%, 120px) scale(.3); } }
      @keyframes clawThump { 0% { transform: scale(1); } 40% { transform: scale(1.02) translateY(2px); } 100% { transform: scale(1); } }
    `}</style>
  );

  // =====================================================
  // STEP 1: QUEUE
  // =====================================================
  if (activeStep === 'queue') {
    return (
      <QueueStep
        stepperHeader={stepperHeader}
        readyStats={readyStats}
        shares={shares}
        candidates={candidates}
        scoringBackend={scoringBackend}
        queueOrder={queueOrder}
        queuedSessions={queuedSessions}
        sessionById={sessionById}
        note={note}
        setNote={setNote}
        onRemove={removeFromQueue}
        onAdd={addToQueue}
        onReorder={reorderQueue}
        onHelp={() => setShowHelp(true)}
        onContinue={handleStartRedaction}
        drawerSessionId={drawerSessionId}
        setDrawerSessionId={setDrawerSessionId}
        showAddTraces={showAddTraces}
        setShowAddTraces={setShowAddTraces}
        searchQuery={searchQuery}
        setSearchQuery={setSearchQuery}
        sourceFilter={sourceFilter}
        setSourceFilter={setSourceFilter}
        projectFilter={projectFilter}
        setProjectFilter={setProjectFilter}
        scoreFilter={scoreFilter}
        setScoreFilter={setScoreFilter}
        dateFilter={dateFilter}
        setDateFilter={setDateFilter}
        reload={reload}
        globalStyles={globalStyles}
        showHelp={showHelp}
        setShowHelp={setShowHelp}
        toast={toast}
      />
    );
  }

  // =====================================================
  // STEP 2: REDACT
  // =====================================================
  if (activeStep === 'redact') {
    return (
      <RedactStep
        stepperHeader={stepperHeader}
        queuedSessions={queuedSessions}
        redactedSessions={redactedSessions}
        allDone={redactAllDone}
        onBack={() => setActiveStep('queue')}
        onContinue={goToReview}
        globalStyles={globalStyles}
        showHelp={showHelp}
        setShowHelp={setShowHelp}
      />
    );
  }

  // =====================================================
  // STEP 3: REVIEW
  // =====================================================
  if (activeStep === 'review') {
    return (
      <ReviewStep
        stepperHeader={stepperHeader}
        queuedSessions={queuedSessions}
        redactedSessions={redactedSessions}
        approvedIds={approvedIds}
        expandedIds={expandedReviewIds}
        onToggleExpand={toggleReviewExpand}
        onApprove={approveTrace}
        onApproveAllClean={approveAllClean}
        onRemove={removeFromQueue}
        onRetryAi={retryAiReview}
        onBack={() => setActiveStep('redact')}
        onPackage={handleStartPackage}
        onHelp={() => setShowHelp(true)}
        globalStyles={globalStyles}
        showHelp={showHelp}
        setShowHelp={setShowHelp}
      />
    );
  }

  // =====================================================
  // STEP 4: PACKAGE
  // =====================================================
  if (activeStep === 'package') {
    return (
      <PackageStep
        stepperHeader={stepperHeader}
        approvedCount={queuedSessions.filter((s) => approvedIds.has(s.session_id)).length}
        approvedList={queuedSessions.filter((s) => approvedIds.has(s.session_id))}
        progress={packageProgress}
        log={packageLog}
        failed={packagingFailed}
        onRetry={runPackage}
        onBack={() => setActiveStep('review')}
        globalStyles={globalStyles}
      />
    );
  }

  // =====================================================
  // STEP 5: DONE
  // =====================================================
  if (activeStep === 'done') {
    return (
      <DoneStep
        stepperHeader={stepperHeader}
        bundle={bundleInfo}
        onDownloadAgain={handleDownloadZip}
        onNew={() => { startFreshShare(); reload(); }}
        globalStyles={globalStyles}
        error={error}
      />
    );
  }

  return null;
}

// ============================================================
// Step 1: Queue component
// ============================================================

interface QueueStepProps {
  stepperHeader: React.ReactNode;
  readyStats: ShareReadyStats | null;
  shares: ShareType[];
  candidates: Session[];
  scoringBackend: { backend: string | null; display_name: string | null } | null;
  queueOrder: string[];
  queuedSessions: ReadySession[];
  sessionById: Record<string, ReadySession>;
  note: string;
  setNote: (s: string) => void;
  onRemove: (id: string) => void;
  onAdd: (id: string) => void;
  onReorder: (fromId: string, overId: string) => void;
  onHelp: () => void;
  onContinue: () => void;
  drawerSessionId: string | null;
  setDrawerSessionId: (id: string | null) => void;
  showAddTraces: boolean;
  setShowAddTraces: (b: boolean) => void;
  searchQuery: string; setSearchQuery: (s: string) => void;
  sourceFilter: string; setSourceFilter: (s: string) => void;
  projectFilter: string; setProjectFilter: (s: string) => void;
  scoreFilter: number; setScoreFilter: (n: number) => void;
  dateFilter: string; setDateFilter: (s: string) => void;
  reload: () => void;
  globalStyles: React.ReactNode;
  showHelp: boolean;
  setShowHelp: (b: boolean) => void;
  toast: (msg: string, kind?: 'success' | 'error') => void;
}

function QueueStep(p: QueueStepProps) {
  const [dragId, setDragId] = useState<string | null>(null);

  const allSessions = p.readyStats?.sessions || [];
  const totalTokens = p.queuedSessions.reduce((sum, s) => sum + sessionTotalTokens(s), 0);
  const uniqueProjects = [...new Set(p.queuedSessions.map(s => s.project).filter(Boolean))];

  const onDragStart = (e: React.DragEvent, id: string) => {
    setDragId(id);
    e.dataTransfer.effectAllowed = 'move';
    try { e.dataTransfer.setData('text/plain', id); } catch { /* ignore */ }
  };
  const onDragOver = (e: React.DragEvent, overId: string) => {
    e.preventDefault();
    if (!dragId || dragId === overId) return;
    p.onReorder(dragId, overId);
  };
  const onDragEnd = () => setDragId(null);

  // Add-traces filter list (everything not in queue)
  const sources = [...new Set(allSessions.map(s => s.source).filter(Boolean))].sort();
  const projects = [...new Set(allSessions.map(s => s.project).filter(Boolean))].sort();
  // eslint-disable-next-line react-hooks/purity
  const dateCutoffMs = p.dateFilter ? (Date.now() - ((p.dateFilter === '7d' ? 7 : p.dateFilter === '30d' ? 30 : 90) * 86_400_000)) : null;

  const available = allSessions.filter((s) => !p.queueOrder.includes(s.session_id)).filter(s => {
    if (p.searchQuery && !(s.display_title || '').toLowerCase().includes(p.searchQuery.toLowerCase())
      && !(s.project || '').toLowerCase().includes(p.searchQuery.toLowerCase())) return false;
    if (p.sourceFilter && s.source !== p.sourceFilter) return false;
    if (p.projectFilter && s.project !== p.projectFilter) return false;
    if (p.scoreFilter > 0 && (s.ai_failure_value_score == null || s.ai_failure_value_score < p.scoreFilter)) return false;
    if (dateCutoffMs && (!s.start_time || new Date(s.start_time).getTime() < dateCutoffMs)) return false;
    return true;
  });

  const historyShares = p.shares.filter(b => b.status === 'shared' || b.status === 'exported');

  return (
    <div style={{ padding: '32px 24px 48px', maxWidth: SHARE_SHELL_WIDTH, margin: '0 auto' }}>
      {p.globalStyles}
      {p.stepperHeader}

      {allSessions.length === 0 && p.candidates.length === 0 ? (
        <>
          <h1 style={{ margin: '0 0 4px', fontSize: 22, fontWeight: 600, color: colors.gray900 }}>Share</h1>
          <p style={{ margin: '0 0 18px', fontSize: 14, color: colors.gray500 }}>
            Build a redacted bundle of your traces to share for model evaluation and training.
          </p>
          <div style={{
            minHeight: 280, padding: '60px 28px', textAlign: 'center',
            background: colors.gray50, border: `1px dashed ${colors.gray300}`,
            borderRadius: 12, color: colors.gray500,
            display: 'flex', flexDirection: 'column', justifyContent: 'center', alignItems: 'center',
          }}>
            <div style={{
              width: 52, height: 52, borderRadius: 12, background: colors.white,
              display: 'grid', placeItems: 'center', margin: '0 auto 16px',
              color: colors.gray400, border: `1px solid ${colors.gray200}`,
            }}>
              <Icon name="inbox" size={24} />
            </div>
            <h3 style={{ color: colors.gray900, fontWeight: 500, margin: '0 0 6px', fontSize: 16 }}>
              No traces ready to share
            </h3>
            <p style={{ margin: '0 auto 20px', maxWidth: '38ch', fontSize: 13 }}>
              Approve traces in Sessions to build a bundle.
            </p>
            <Link to="/" style={{ ...btnPrimary, textDecoration: 'none' }}>Go to Sessions</Link>
          </div>
        </>
      ) : p.queuedSessions.length === 0 ? (
        <>
          <h1 style={{ margin: '0 0 4px', fontSize: 22, fontWeight: 600, color: colors.gray900 }}>Share</h1>
          <p style={{ margin: '0 0 18px', fontSize: 14, color: colors.gray500 }}>
            Build a redacted bundle of your traces to share for model evaluation and training.
          </p>
          {p.candidates.length > 0 ? (
            <>
              <div style={{ marginBottom: 12 }}>
                <h3 style={{ margin: '0 0 4px', fontSize: 15, fontWeight: 600, color: colors.gray900 }}>
                  Top traces to review
                </h3>
                <p style={{ margin: 0, fontSize: 12, color: colors.gray500 }}>
                  {p.scoringBackend?.display_name
                    ? `Scored by ${p.scoringBackend.display_name}`
                    : 'Scored by your configured agent'}
                </p>
              </div>
              <div style={{ border: `1px solid ${colors.gray200}`, borderRadius: 8, overflow: 'hidden', marginBottom: 24 }}>
                {p.candidates.map((s) => (
                  <TraceCard
                    key={s.session_id}
                    session={s}
                    showSelection={false}
                    showQuickActions={true}
                    quickActionMode="share"
                    onStatusChange={(newStatus) => {
                      if (newStatus === 'approved') p.onAdd(s.session_id);
                      p.reload();
                    }}
                  />
                ))}
              </div>
            </>
          ) : (
            <div style={{
              minHeight: 280, padding: '48px 28px', textAlign: 'center',
              background: colors.gray50, border: `1px dashed ${colors.gray300}`,
              borderRadius: 12, color: colors.gray500,
              display: 'flex', flexDirection: 'column', justifyContent: 'center', alignItems: 'center',
            }}>
              <div style={{
                width: 52, height: 52, borderRadius: 12, background: colors.white,
                display: 'grid', placeItems: 'center', margin: '0 auto 16px',
                color: colors.gray400, border: `1px solid ${colors.gray200}`,
              }}>
                <Icon name="inbox" size={24} />
              </div>
              <h3 style={{ color: colors.gray900, fontWeight: 500, margin: '0 0 6px', fontSize: 16 }}>
                Your queue is empty
              </h3>
              <p style={{ margin: '0 auto 18px', maxWidth: '40ch', fontSize: 13 }}>
                Add traces to build a bundle. The recommended set is based on your recent work.
              </p>
              <button onClick={() => p.setShowAddTraces(true)} style={btnPrimary}>
                <Icon name="plus" size={13} />
                Add traces
              </button>
            </div>
          )}
        </>
      ) : (
        <>
          <h1 style={{ margin: '0 0 12px', fontSize: 22, fontWeight: 600, color: colors.gray900 }}>
            What would you like to share?
          </h1>
          <UsageDisclosure onLearnMore={p.onHelp} />

          {/* Bundle summary */}
          <div style={{
            display: 'flex', alignItems: 'center', justifyContent: 'space-between',
            padding: '12px 14px', background: colors.gray50,
            border: `1px solid ${colors.gray200}`, borderRadius: 8, marginBottom: 10,
          }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
              <div style={{ fontSize: 13, color: colors.gray900, fontWeight: 500 }}>draft-bundle</div>
              <div style={{ fontSize: 12, color: colors.gray500, fontVariantNumeric: 'tabular-nums' }}>
                {p.queuedSessions.length} trace{p.queuedSessions.length === 1 ? '' : 's'} &middot; ~{formatTokens(totalTokens)} tokens
                {uniqueProjects.length > 0 && ` · ${uniqueProjects.length} project${uniqueProjects.length !== 1 ? 's' : ''}`}
              </div>
            </div>
            <span
              style={{
                display: 'inline-flex', alignItems: 'center', gap: 6,
                color: colors.gray500, fontSize: 11.5,
              }}
              title="Drag the handle on the left of each row to reorder"
            >
              <Icon name="grip" size={12} />
              Drag to reorder
            </span>
          </div>

          {/* Trace list */}
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginBottom: 18 }} onDragEnd={onDragEnd}>
            {p.queuedSessions.map((s) => {
              const isDragging = dragId === s.session_id;
              return (
                <div
                  key={s.session_id}
                  draggable
                  onDragStart={(e) => onDragStart(e, s.session_id)}
                  onDragOver={(e) => onDragOver(e, s.session_id)}
                  onDrop={(e) => { e.preventDefault(); setDragId(null); }}
                  style={{
                    display: 'grid', gridTemplateColumns: '20px 1fr auto', gap: 12,
                    alignItems: 'center', padding: '12px 14px',
                    background: isDragging ? colors.gray100 : colors.white,
                    border: `1px solid ${isDragging ? colors.primary400 : colors.gray200}`,
                    borderRadius: 8,
                    boxShadow: isDragging ? '0 10px 24px -10px rgba(0,0,0,0.2)' : 'none',
                    cursor: isDragging ? 'grabbing' : 'default',
                    transition: 'border-color 140ms, background 140ms, box-shadow 200ms',
                  }}
                >
                  <div style={{ color: colors.gray400, cursor: 'grab', display: 'grid', placeItems: 'center' }} title="Drag to reorder">
                    <Icon name="grip" size={14} />
                  </div>
                  <div style={{ minWidth: 0 }}>
                    <div style={{
                      fontSize: 13.5, color: colors.gray900, fontWeight: 500,
                      whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
                    }}>
                      {s.display_title || 'Untitled'}
                    </div>
                    <div style={{ fontSize: 11.5, color: colors.gray500, display: 'flex', gap: 10, alignItems: 'center', marginTop: 2 }}>
                      <SourceBadge s={s} />
                      <span>{s.project}</span>
                      <span style={{ opacity: 0.5 }}>&middot;</span>
                      <span>{formatTokens(sessionTotalTokens(s))} tokens</span>
                      {s.tool_uses > 0 && (<>
                        <span style={{ opacity: 0.5 }}>&middot;</span>
                        <span>{s.tool_uses} tools</span>
                      </>)}
                      {s.ai_failure_value_score != null && (<>
                        <span style={{ opacity: 0.5 }}>&middot;</span>
                        <span style={{ color: '#991b1b', fontWeight: 700 }}>{s.ai_failure_value_score} failure value</span>
                      </>)}
                      {s.ai_quality_score != null && (<>
                        <span style={{ opacity: 0.5 }}>&middot;</span>
                        <span
                          style={{ color: colors.gray500 }}
                          title="Legacy productivity score"
                        >
                          productivity {s.ai_quality_score}/5
                        </span>
                      </>)}
                      {s.ai_failure_attribution && (<>
                        <span style={{ opacity: 0.5 }}>&middot;</span>
                        <span>{s.ai_failure_attribution.replace(/_/g, ' ')}</span>
                      </>)}
                      {s.ai_failure_value_score == null && (<>
                        <span style={{ opacity: 0.5 }}>&middot;</span>
                        <span
                          style={{ color: colors.gray500, fontStyle: 'italic' }}
                          title={s.ai_quality_score == null
                            ? "This session hasn't been scored yet. Click Preview → Score with AI, or run `clawjournal score` from a terminal."
                            : "This legacy-scored session is missing failure value. Re-score it with AI before sharing."}
                        >
                          {s.ai_quality_score == null ? 'unscored' : 'failure value missing'}
                        </span>
                      </>)}
                      {s.outcome_badge && (<>
                        <span style={{ opacity: 0.5 }}>&middot;</span>
                        <span>{outcomeBadge(s.outcome_badge)}</span>
                      </>)}
                    </div>
                  </div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                    <button
                      onClick={() => p.setDrawerSessionId(s.session_id)}
                      style={btnGhost}
                      title="Preview"
                    >
                      Preview
                    </button>
                    <button
                      onClick={() => p.onRemove(s.session_id)}
                      style={{ ...btnGhost, color: colors.red700 }}
                      title="Remove from bundle"
                    >
                      Remove
                    </button>
                  </div>
                </div>
              );
            })}
          </div>

          {/* Add more traces */}
          <div style={{ marginBottom: 18 }}>
            <button
              onClick={() => p.setShowAddTraces(!p.showAddTraces)}
              style={{
                ...btnGhost, color: colors.gray600, fontSize: 13,
                padding: '6px 10px', border: `1px solid ${colors.gray300}`,
                borderRadius: 6, background: colors.white,
              }}
            >
              <Icon name={p.showAddTraces ? 'chevron' : 'plus'} size={12} />
              {p.showAddTraces ? 'Hide' : 'Add more traces'}
              {!p.showAddTraces && available.length > 0 && (
                <span style={{ color: colors.gray400, fontWeight: 400, marginLeft: 4 }}>({available.length} available)</span>
              )}
            </button>

            {p.showAddTraces && (
              <div style={{
                marginTop: 10, padding: 12,
                background: colors.gray50, border: `1px solid ${colors.gray200}`, borderRadius: 8,
              }}>
                <div style={{ display: 'flex', gap: 8, marginBottom: 10, flexWrap: 'wrap' }}>
                  <input
                    type="text"
                    value={p.searchQuery}
                    onChange={e => p.setSearchQuery(e.target.value)}
                    placeholder="Search by title or project..."
                    style={{
                      flex: 1, minWidth: 200, padding: '6px 10px', fontSize: 13,
                      border: `1px solid ${colors.gray300}`, borderRadius: 6,
                      outline: 'none', background: colors.white,
                    }}
                  />
                  {sources.length > 1 && (
                    <select value={p.sourceFilter} onChange={e => p.setSourceFilter(e.target.value)}
                      style={{ padding: '6px 8px', fontSize: 12, border: `1px solid ${colors.gray300}`, borderRadius: 6, background: colors.white }}>
                      <option value="">All sources</option>
                      {sources.map(src => <option key={src} value={src}>{src}</option>)}
                    </select>
                  )}
                  {projects.length > 1 && (
                    <select value={p.projectFilter} onChange={e => p.setProjectFilter(e.target.value)}
                      style={{ padding: '6px 8px', fontSize: 12, border: `1px solid ${colors.gray300}`, borderRadius: 6, background: colors.white, maxWidth: 180 }}>
                      <option value="">All projects</option>
                      {projects.map(pr => <option key={pr} value={pr}>{pr}</option>)}
                    </select>
                  )}
                  <select value={p.scoreFilter} onChange={e => p.setScoreFilter(Number(e.target.value))}
                    style={{ padding: '6px 8px', fontSize: 12, border: `1px solid ${colors.gray300}`, borderRadius: 6, background: colors.white }}>
                    <option value={0}>Any failure value</option>
                    <option value={3}>3+ failure value</option>
                    <option value={4}>4+ failure value</option>
                    <option value={5}>5 failure value</option>
                  </select>
                  <select value={p.dateFilter} onChange={e => p.setDateFilter(e.target.value)}
                    style={{ padding: '6px 8px', fontSize: 12, border: `1px solid ${colors.gray300}`, borderRadius: 6, background: colors.white }}>
                    <option value="">Any date</option>
                    <option value="7d">Last 7 days</option>
                    <option value="30d">Last 30 days</option>
                    <option value="90d">Last 90 days</option>
                  </select>
                </div>
                <div style={{ maxHeight: '36vh', overflowY: 'auto', border: `1px solid ${colors.gray200}`, borderRadius: 6, background: colors.white }}>
                  {available.length === 0 ? (
                    <div style={{ padding: 14, textAlign: 'center', color: colors.gray400, fontSize: 13 }}>
                      {allSessions.length === p.queuedSessions.length ? 'All available traces are already in the queue.' : 'No sessions match your filters.'}
                    </div>
                  ) : available.map((s, i) => (
                    <div key={s.session_id} style={{
                      display: 'grid', gridTemplateColumns: '1fr auto', gap: 10,
                      alignItems: 'center', padding: '8px 12px',
                      borderBottom: i < available.length - 1 ? `1px solid ${colors.gray100}` : 'none',
                    }}>
                      <div style={{ minWidth: 0 }}>
                        <div style={{
                          fontSize: 13, color: colors.gray900, fontWeight: 500,
                          whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
                        }}>
                          {s.display_title || 'Untitled'}
                        </div>
                        <div style={{ fontSize: 11, color: colors.gray500, marginTop: 2, display: 'flex', gap: 8, alignItems: 'center' }}>
                          <SourceBadge s={s} />
                          <span>{s.project}</span>
                          <span style={{ opacity: 0.5 }}>&middot;</span>
                          <span>{formatTokens(sessionTotalTokens(s))} tokens</span>
                          {s.review_status && s.review_status !== 'approved' && (<>
                            <span style={{ opacity: 0.5 }}>&middot;</span>
                            <span style={{ color: colors.gray400, fontStyle: 'italic' }}>{s.review_status}</span>
                          </>)}
                        </div>
                      </div>
                      <button onClick={() => p.onAdd(s.session_id)} style={btnSecondary}>
                        <Icon name="plus" size={12} />
                        Add
                      </button>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>

          {/* Note */}
          <div style={{ marginBottom: 18 }}>
            <label style={{ fontSize: 12, fontWeight: 500, color: colors.gray600 }}>Note (optional)</label>
            <input
              type="text" value={p.note} onChange={e => p.setNote(e.target.value)}
              placeholder="e.g. Week 14 patent translation traces"
              style={{
                display: 'block', width: '100%', padding: '7px 10px', marginTop: 4,
                border: `1px solid ${colors.gray300}`, borderRadius: 6, fontSize: 13,
                boxSizing: 'border-box', background: colors.white,
              }}
            />
          </div>

          {/* Footer */}
          <div style={{
            position: 'sticky', bottom: 0, marginTop: 8, paddingTop: 14,
            background: `linear-gradient(to top, ${colors.gray50} 40%, rgba(250,248,245,0.95) 80%, transparent)`,
          }}>
            <div style={{
              display: 'flex', alignItems: 'center', gap: 14,
              padding: '12px 14px', background: colors.white,
              border: `1px solid ${colors.gray200}`, borderRadius: 8,
              boxShadow: '0 1px 2px rgba(0,0,0,0.04)',
            }}>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
                <div style={{ fontSize: 13, color: colors.gray900 }}>
                  {p.queuedSessions.length} trace{p.queuedSessions.length === 1 ? '' : 's'} selected
                </div>
                <div style={{ fontSize: 11.5, color: colors.gray500, fontVariantNumeric: 'tabular-nums' }}>
                  Next: we&rsquo;ll redact secrets and identifiers on your device
                </div>
              </div>
              <div style={{ marginLeft: 'auto', display: 'flex', gap: 8 }}>
                <button
                  onClick={p.onContinue}
                  disabled={p.queuedSessions.length === 0}
                  style={{ ...btnPrimary, opacity: p.queuedSessions.length === 0 ? 0.4 : 1, cursor: p.queuedSessions.length === 0 ? 'not-allowed' : 'pointer' }}
                >
                  Redact &amp; review
                  <Icon name="sparkle" size={13} />
                </button>
              </div>
            </div>
          </div>
        </>
      )}

      {historyShares.length > 0 && p.queuedSessions.length > 0 && (
        <div style={{ marginTop: 40 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
            <h3 style={{ margin: 0, fontSize: 14, fontWeight: 600, color: colors.gray900 }}>Recent sharing history</h3>
            <span style={{ fontSize: 12, color: colors.gray500 }}>
              {historyShares.filter(b => b.status === 'shared').length} shared total
            </span>
          </div>
          {historyShares.slice(0, 4).map(share => (
            <div key={share.share_id} style={{
              background: colors.white, border: `1px solid ${colors.gray200}`, borderRadius: 8,
              padding: '12px 14px', marginBottom: 8,
              display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start',
            }}>
              <div>
                <div style={{ fontSize: 13, fontWeight: 500, color: colors.gray900 }}>{autoDescription(share)}</div>
                <div style={{ fontSize: 12, color: colors.gray500, marginTop: 2 }}>
                  {formatDate(share.created_at)} &middot; {share.session_count} sessions &middot;{' '}
                  {share.status === 'shared'
                    ? <span style={{ color: colors.green500 }}>Shared &#x2713;</span>
                    : <span style={{ color: colors.gray500 }}>Saved locally</span>}
                </div>
              </div>
              <button
                onClick={async () => {
                  try {
                    await api.shares.download(share.share_id);
                    p.toast('Download started', 'success');
                  } catch (err: unknown) {
                    p.toast(err instanceof Error ? err.message : 'Download failed', 'error');
                  }
                }}
                style={btnSecondary}
              >
                <Icon name="download" size={12} />
                Download
              </button>
            </div>
          ))}
        </div>
      )}

      <SessionDrawer
        sessionId={p.drawerSessionId}
        onClose={() => p.setDrawerSessionId(null)}
      />
      {p.showHelp && <HelpModal onClose={() => p.setShowHelp(false)} />}
    </div>
  );
}

// ============================================================
// Step 2: Redact component
// ============================================================

interface RedactStepProps {
  stepperHeader: React.ReactNode;
  queuedSessions: ReadySession[];
  redactedSessions: Record<string, RedactedSessionData>;
  allDone: boolean;
  onBack: () => void;
  onContinue: () => void;
  globalStyles: React.ReactNode;
  showHelp: boolean;
  setShowHelp: (b: boolean) => void;
}

function RedactStep(p: RedactStepProps) {
  const totals = p.queuedSessions.reduce((acc, s) => {
    const d = p.redactedSessions[s.session_id];
    if (!d || d.loading || !d.buckets) return acc;
    acc.tokens += d.buckets.tokens;
    acc.emails += d.buckets.emails;
    acc.paths += d.buckets.paths;
    acc.timestamps += d.buckets.timestamps;
    acc.urls += d.buckets.urls;
    acc.other += d.buckets.other;
    if (classify(d) === 'review') acc.flagged += 1;
    acc.thHits += d.trufflehogHits || 0;
    return acc;
  }, { ...emptyBuckets(), flagged: 0, thHits: 0 });

  const doneCount = p.queuedSessions.filter((s) => {
    const d = p.redactedSessions[s.session_id];
    return d && !d.loading;
  }).length;
  const overallPct = p.queuedSessions.length === 0 ? 0 : Math.round((doneCount / p.queuedSessions.length) * 100);

  // progress bar helper for category rows
  const categoryRow = (label: string, count: number, max: number, color: string = colors.primary500) => (
    <div style={{ display: 'flex', alignItems: 'center', gap: 12, fontSize: 13, marginBottom: 8 }}>
      <span style={{ color: colors.gray700, flex: 1 }}>{label}</span>
      <div style={{ flex: 2, maxWidth: 200, height: 6, background: colors.gray100, borderRadius: 3, overflow: 'hidden' }}>
        <div style={{
          height: '100%', width: `${Math.min(100, (count / Math.max(max, 1)) * 100)}%`,
          background: color, transition: 'width 400ms ease',
        }} />
      </div>
      <span style={{
        color: colors.gray500, fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
        fontSize: 12, fontVariantNumeric: 'tabular-nums', minWidth: 80, textAlign: 'right' as const,
      }}>
        {count} removed
      </span>
    </div>
  );

  return (
    <div style={{ padding: '32px 24px 48px', maxWidth: SHARE_SHELL_WIDTH, margin: '0 auto' }}>
      {p.globalStyles}
      {p.stepperHeader}
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 6 }}>
        <button onClick={p.onBack} style={btnGhost}>&larr; Back to queue</button>
      </div>
      <h1 style={{ margin: '0 0 6px', fontSize: 22, fontWeight: 600, color: colors.gray900 }}>
        Redacting your traces
      </h1>
      <p style={{ margin: '0 0 20px', fontSize: 14, color: colors.gray500, maxWidth: '60ch', lineHeight: 1.55 }}>
        Before anything leaves your device, we strip out secrets and personal identifiers.
        Watch it happen &mdash; nothing is hidden.
      </p>

      <UsageDisclosure onLearnMore={() => p.setShowHelp(true)} />

      <div style={{
        display: 'grid', gridTemplateColumns: '1fr auto', gap: 16,
        alignItems: 'center', marginBottom: 16,
      }}>
        <div>
          <div style={{ fontSize: 16, color: colors.gray900, fontWeight: 500, marginBottom: 3 }}>
            Scrubbing {p.queuedSessions.length} trace{p.queuedSessions.length === 1 ? '' : 's'}
          </div>
          <div style={{ fontSize: 13, color: colors.gray500 }}>
            Deterministic rules &rarr; Policy rules &rarr; AI review. All on-device.
          </div>
        </div>
        <div style={{
          fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
          fontSize: 13, color: colors.gray900, fontVariantNumeric: 'tabular-nums',
          padding: '8px 14px', background: colors.white,
          border: `1px solid ${colors.gray200}`, borderRadius: 6,
        }}>
          {overallPct}%
        </div>
      </div>

      <div style={{
        padding: '16px 18px', marginBottom: 20,
        background: colors.white, border: `1px solid ${colors.gray200}`, borderRadius: 8,
      }}>
        {categoryRow(
          `Secrets & credentials${totals.thHits > 0 ? ` (incl. ${totals.thHits} via TruffleHog)` : ''}`,
          totals.tokens, Math.max(totals.tokens, 4),
        )}
        {categoryRow('Email addresses', totals.emails, Math.max(totals.emails, 4))}
        {categoryRow('File paths & usernames', totals.paths, Math.max(totals.paths, 8))}
        {categoryRow('Timestamps coarsened', totals.timestamps, Math.max(totals.timestamps, 20))}
        {categoryRow('URLs', totals.urls, Math.max(totals.urls, 4), colors.blue500)}
        {categoryRow('AI-flagged for your review', totals.flagged, Math.max(totals.flagged, 2), colors.yellow400)}
      </div>

      <div style={{
        fontSize: 11, textTransform: 'uppercase', letterSpacing: '0.08em',
        color: colors.gray400, margin: '0 0 10px', fontWeight: 600,
      }}>Per-trace progress</div>

      {p.queuedSessions.map((s) => {
        const d = p.redactedSessions[s.session_id];
        const finished = !!d && !d.loading;
        const flagged = finished && classify(d) === 'review';
        const chips: string[] = [];
        if (finished && d.buckets) {
          if (d.buckets.emails) chips.push(`${d.buckets.emails} email${d.buckets.emails === 1 ? '' : 's'}`);
          if (d.buckets.tokens) chips.push(`${d.buckets.tokens} secret${d.buckets.tokens === 1 ? '' : 's'}`);
          if (d.buckets.paths) chips.push(`${d.buckets.paths} path${d.buckets.paths === 1 ? '' : 's'}`);
          if (d.buckets.timestamps) chips.push(`${d.buckets.timestamps} timestamps`);
          if (d.buckets.urls) chips.push(`${d.buckets.urls} URL${d.buckets.urls === 1 ? '' : 's'}`);
        }
        return (
          <div key={s.session_id} style={{
            display: 'grid', gridTemplateColumns: '26px 1fr auto', gap: 14,
            alignItems: 'center', padding: '12px 14px',
            background: colors.white, border: `1px solid ${colors.gray200}`,
            borderRadius: 8, marginBottom: 6,
          }}>
            <div style={{
              width: 22, height: 22, borderRadius: '50%',
              background: finished ? colors.green100 : colors.gray100,
              color: finished ? colors.green500 : colors.gray500,
              display: 'grid', placeItems: 'center', flexShrink: 0,
            }}>
              {finished ? <Icon name="check" size={12} /> : (
                <span style={{
                  display: 'inline-block', width: 12, height: 12, borderRadius: '50%',
                  border: `1.5px solid ${colors.gray400}`, borderTopColor: 'transparent',
                  animation: 'clawSpin 800ms linear infinite',
                }} />
              )}
            </div>
            <div style={{ minWidth: 0 }}>
              <div style={{
                fontSize: 13.5, color: colors.gray900,
                whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
              }}>
                {s.display_title || 'Untitled'}
              </div>
              <div style={{ fontSize: 11.5, color: colors.gray500, marginTop: 2, display: 'flex', gap: 10, alignItems: 'center' }}>
                <SourceBadge s={s} />
                <span>{s.project}</span>
                <span style={{ opacity: 0.5 }}>&middot;</span>
                <span>{formatTokens(sessionTotalTokens(s))} tokens</span>
              </div>
            </div>
            <div style={{ display: 'flex', gap: 5, alignItems: 'center', flexWrap: 'wrap', justifyContent: 'flex-end' }}>
              {chips.map((c, j) => (
                <span key={c} style={{
                  fontSize: 11, color: colors.gray600, padding: '2px 7px',
                  background: colors.gray100, borderRadius: 10,
                  fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
                  fontVariantNumeric: 'tabular-nums',
                  opacity: 0, animation: `clawChipPop 420ms cubic-bezier(.2,1.4,.3,1) forwards`,
                  animationDelay: `${j * 60}ms`,
                }}>
                  {c}
                </span>
              ))}
              {flagged && (
                <span style={{
                  fontSize: 11, color: colors.yellow700, padding: '2px 7px',
                  background: colors.yellow50, border: `1px solid ${colors.yellow200}`,
                  borderRadius: 10, fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
                }}>
                  needs review
                </span>
              )}
            </div>
          </div>
        );
      })}

      <div style={{
        position: 'sticky', bottom: 0, marginTop: 14, paddingTop: 14,
        background: `linear-gradient(to top, ${colors.gray50} 40%, rgba(250,248,245,0.95) 80%, transparent)`,
      }}>
        <div style={{
          display: 'flex', alignItems: 'center', gap: 14,
          padding: '12px 14px', background: colors.white,
          border: `1px solid ${colors.gray200}`, borderRadius: 8,
          boxShadow: '0 1px 2px rgba(0,0,0,0.04)',
        }}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
            <div style={{ fontSize: 13, color: colors.gray900 }}>
              {p.allDone ? 'Redaction complete' : 'Redacting...'}
            </div>
            <div style={{ fontSize: 11.5, color: colors.gray500, fontVariantNumeric: 'tabular-nums' }}>
              {p.allDone
                ? (totals.flagged > 0
                  ? `${totals.flagged} item${totals.flagged === 1 ? '' : 's'} need your review next`
                  : 'Everything cleared automatically')
                : 'Running on your device'}
            </div>
          </div>
          <div style={{ marginLeft: 'auto' }}>
            <button
              onClick={p.onContinue}
              disabled={!p.allDone}
              style={{
                ...btnPrimary,
                opacity: p.allDone ? 1 : 0.4,
                cursor: p.allDone ? 'pointer' : 'not-allowed',
              }}
            >
              {p.allDone ? (<>Review what I&rsquo;m sharing<Icon name="check" size={13} /></>) : (<>
                <span style={{
                  display: 'inline-block', width: 12, height: 12, borderRadius: '50%',
                  border: `1.5px solid currentColor`, borderTopColor: 'transparent',
                  animation: 'clawSpin 750ms linear infinite',
                }} />
                Redacting...
              </>)}
            </button>
          </div>
        </div>
      </div>
      {p.showHelp && <HelpModal onClose={() => p.setShowHelp(false)} />}
    </div>
  );
}

// ============================================================
// Step 3: Review component
// ============================================================

interface ReviewStepProps {
  stepperHeader: React.ReactNode;
  queuedSessions: ReadySession[];
  redactedSessions: Record<string, RedactedSessionData>;
  approvedIds: Set<string>;
  expandedIds: Set<string>;
  onToggleExpand: (id: string) => void;
  onApprove: (id: string) => void;
  onApproveAllClean: () => void;
  onRemove: (id: string) => void;
  onRetryAi: (id: string) => void;
  onBack: () => void;
  onPackage: () => void;
  onHelp: () => void;
  globalStyles: React.ReactNode;
  showHelp: boolean;
  setShowHelp: (b: boolean) => void;
}

function ReviewStep(p: ReviewStepProps) {
  const sorted = [...p.queuedSessions].sort((a, b) => {
    const sa = classify(p.redactedSessions[a.session_id]);
    const sb = classify(p.redactedSessions[b.session_id]);
    const order = { review: 0, checking: 1, clear: 2 };
    return order[sa] - order[sb];
  });

  const approvedCount = p.queuedSessions.filter((s) => p.approvedIds.has(s.session_id)).length;
  const allApproved = p.queuedSessions.length > 0 && approvedCount === p.queuedSessions.length;
  const cleanUnapprovedCount = p.queuedSessions.filter((s) => (
    classify(p.redactedSessions[s.session_id]) === 'clear' && !p.approvedIds.has(s.session_id)
  )).length;

  return (
    <div style={{ padding: '32px 24px 48px', maxWidth: SHARE_SHELL_WIDTH, margin: '0 auto' }}>
      {p.globalStyles}
      {p.stepperHeader}
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 6 }}>
        <button onClick={p.onBack} style={btnGhost}>&larr; Back to redaction</button>
      </div>
      <h1 style={{ margin: '0 0 6px', fontSize: 22, fontWeight: 600, color: colors.gray900 }}>
        Review what you&rsquo;re sharing
      </h1>
      <p style={{ margin: '0 0 20px', fontSize: 14, color: colors.gray500, maxWidth: '60ch', lineHeight: 1.55 }}>
        You&rsquo;re the last checkpoint before packaging. Include each trace &mdash; or drop it
        &mdash; so you know exactly what&rsquo;s in the zip.
      </p>

      <UsageDisclosure onLearnMore={() => p.setShowHelp(true)} />

      {/* Bulk progress bar */}
      <div style={{
        position: 'sticky', top: 0, zIndex: 6,
        background: allApproved
          ? `linear-gradient(90deg, rgba(250,248,245,0.95), ${hexAlpha('#558745', 0.12)}, rgba(250,248,245,0.95))`
          : 'rgba(250,248,245,0.95)',
        backdropFilter: 'blur(6px)',
        padding: '10px 14px', marginBottom: 14,
        border: `1px solid ${colors.gray200}`, borderRadius: 8,
        display: 'flex', alignItems: 'center', gap: 12, fontSize: 13,
      }}>
        <span style={{
          fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
          fontVariantNumeric: 'tabular-nums', color: colors.gray900,
        }}>
          <strong style={{ color: allApproved ? colors.green500 : colors.gray900 }}>{approvedCount}</strong>
          <span style={{ color: colors.gray500 }}> / {p.queuedSessions.length} included</span>
        </span>
        <span style={{ color: colors.gray500, fontSize: 12, marginRight: 'auto' }}>
          {allApproved
            ? 'All traces included. Ready to package.'
            : 'Tap each card to inspect. You can include one-by-one or all clean ones at once.'}
        </span>
        <button
          onClick={p.onApproveAllClean}
          disabled={cleanUnapprovedCount === 0}
          style={{
            ...btnSecondary, padding: '6px 12px', fontSize: 12.5,
            opacity: cleanUnapprovedCount === 0 ? 0.4 : 1,
            cursor: cleanUnapprovedCount === 0 ? 'not-allowed' : 'pointer',
          }}
        >
          Include all clean ({cleanUnapprovedCount})
        </button>
      </div>

      <div>
        {sorted.map((s) => (
          <ReviewRow
            key={s.session_id}
            session={s}
            data={p.redactedSessions[s.session_id]}
            approved={p.approvedIds.has(s.session_id)}
            expanded={p.expandedIds.has(s.session_id)}
            onToggle={() => p.onToggleExpand(s.session_id)}
            onApprove={() => p.onApprove(s.session_id)}
            onRemove={() => p.onRemove(s.session_id)}
            onRetryAi={() => p.onRetryAi(s.session_id)}
          />
        ))}
      </div>

      <div style={{
        position: 'sticky', bottom: 0, marginTop: 14, paddingTop: 14,
        background: `linear-gradient(to top, ${colors.gray50} 40%, rgba(250,248,245,0.95) 80%, transparent)`,
      }}>
        <div style={{
          display: 'flex', alignItems: 'center', gap: 14,
          padding: '12px 14px', background: colors.white,
          border: `1px solid ${colors.gray200}`, borderRadius: 8,
          boxShadow: '0 1px 2px rgba(0,0,0,0.04)',
        }}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
            <div style={{ fontSize: 13, color: colors.gray900 }}>
              {allApproved ? 'All included — ready to package' : `${p.queuedSessions.length - approvedCount} still waiting on you`}
            </div>
            <div style={{ fontSize: 11.5, color: colors.gray500, fontVariantNumeric: 'tabular-nums' }}>
              Included traces will be packaged into draft-bundle.zip
            </div>
          </div>
          <div style={{ marginLeft: 'auto' }}>
            <button
              onClick={p.onPackage}
              disabled={!allApproved}
              style={{ ...btnPrimary, opacity: allApproved ? 1 : 0.4, cursor: allApproved ? 'pointer' : 'not-allowed' }}
            >
              <Icon name="check" size={14} />
              Package bundle
            </button>
          </div>
        </div>
      </div>
      {p.showHelp && <HelpModal onClose={() => p.setShowHelp(false)} />}
    </div>
  );
}

interface RedactionCategory {
  label: string;
  count: number;
  source: 'rules' | 'ai';
}

function aggregateCategories(data: RedactedSessionData | undefined): RedactionCategory[] {
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

function ReviewRow({
  session, data, approved, expanded, onToggle, onApprove, onRemove, onRetryAi,
}: {
  session: ReadySession;
  data: RedactedSessionData | undefined;
  approved: boolean;
  expanded: boolean;
  onToggle: () => void;
  onApprove: () => void;
  onRemove: () => void;
  onRetryAi: () => void;
}) {
  const status = classify(data);
  const buckets = data?.buckets;
  const categories = aggregateCategories(data);
  const totalItems = categories.reduce((s, c) => s + c.count, 0);
  const borderColor = approved ? colors.green200 : status === 'review' ? colors.yellow200 : colors.gray200;
  const aiUnavailable = data?.aiCoverage === 'rules_only';

  // Meta label under the title — no raw finding counts, just a neutral phrase.
  const metaPhrase: string | null = status === 'review'
    ? (aiUnavailable ? 'needs review · rules-only' : 'needs review')
    : null;

  return (
    <div style={{
      background: colors.white, border: `1px solid ${borderColor}`,
      borderRadius: 8, marginBottom: 8, overflow: 'hidden',
      transition: 'border-color 140ms',
    }}>
      <div
        onClick={onToggle}
        style={{
          display: 'grid', gridTemplateColumns: '22px 1fr auto auto',
          gap: 14, alignItems: 'center', padding: '12px 14px', cursor: 'pointer',
        }}
      >
        <StatusDot status={status} />
        <div style={{ minWidth: 0 }}>
          <div style={{
            fontSize: 13.5, color: colors.gray900,
            whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
          }}>
            {session.display_title || 'Untitled'}
          </div>
          <div style={{ fontSize: 11.5, color: colors.gray500, marginTop: 2, display: 'flex', gap: 10, alignItems: 'center' }}>
            <SourceBadge s={session} />
            <span>{session.project}</span>
            <span style={{ opacity: 0.5 }}>&middot;</span>
            <span>{formatTokens(sessionTotalTokens(session))} tokens</span>
            {metaPhrase && (<>
              <span style={{ opacity: 0.5 }}>&middot;</span>
              <span style={{ color: colors.yellow700 }}>{metaPhrase}</span>
            </>)}
          </div>
          {status === 'clear' && buckets && totalItems > 0 && (
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 5, marginTop: 4 }}>
              {buckets.tokens > 0 && <span style={autoChipStyle}>{buckets.tokens} secret{buckets.tokens === 1 ? '' : 's'}</span>}
              {buckets.emails > 0 && <span style={autoChipStyle}>{buckets.emails} email{buckets.emails === 1 ? '' : 's'}</span>}
              {buckets.paths > 0 && <span style={autoChipStyle}>{buckets.paths} path{buckets.paths === 1 ? '' : 's'}</span>}
              {buckets.timestamps > 0 && <span style={autoChipStyle}>{buckets.timestamps} ts</span>}
              {buckets.urls > 0 && <span style={autoChipStyle}>{buckets.urls} URL{buckets.urls === 1 ? '' : 's'}</span>}
            </div>
          )}
        </div>
        <div style={{ fontSize: 12, color: colors.gray400 }}>
          {expanded ? 'Collapse' : 'Inspect'}
        </div>
        <div>
          {approved ? (
            <span style={{
              display: 'inline-flex', alignItems: 'center', gap: 6,
              fontSize: 12, color: colors.green500, fontWeight: 500,
            }}>
              <span style={{
                width: 16, height: 16, borderRadius: '50%',
                background: colors.green500, color: colors.white,
                display: 'grid', placeItems: 'center',
              }}>
                <Icon name="check" size={10} />
              </span>
              Included
            </span>
          ) : (
            <span style={{
              display: 'inline-flex', alignItems: 'center', gap: 6,
              fontSize: 12, color: colors.gray500,
            }}>
              <span style={{
                width: 16, height: 16, borderRadius: '50%',
                border: `1.5px dashed ${colors.gray400}`,
              }} />
              {status === 'review' ? 'Needs your eyes' : 'Awaiting you'}
            </span>
          )}
        </div>
      </div>

      {expanded && (
        <div style={{
          borderTop: `1px solid ${colors.gray200}`,
          padding: '16px 18px 18px', background: colors.gray50,
        }}>
          <p style={{ fontSize: 13, color: colors.gray700, margin: '0 0 14px', lineHeight: 1.55 }}>
            {status === 'clear'
              ? <>This trace cleared automatically. Here&rsquo;s the redacted version that will ship &mdash; scan it if you&rsquo;d like extra peace of mind.</>
              : <>Here&rsquo;s the redacted trace. Scan it &mdash; if anything looks off, <strong style={{ color: colors.gray900 }}>remove it</strong>. Otherwise include it in the bundle.</>}
          </p>

          {aiUnavailable && (
            <div style={{
              display: 'flex', alignItems: 'center', gap: 10,
              padding: '10px 12px', marginBottom: 14,
              background: colors.yellow50, border: `1px solid ${colors.yellow200}`,
              borderRadius: 6, fontSize: 12.5, color: colors.gray900,
            }}>
              <Icon name="alert" size={14} />
              <span style={{ color: colors.gray600, marginRight: 'auto' }}>
                AI review was unavailable &mdash; only deterministic + policy rules ran on this trace.
              </span>
              {!approved && (
                <button
                  onClick={onRetryAi}
                  style={{
                    ...btnGhost, color: colors.primary500, fontSize: 12.5,
                    border: `1px solid ${colors.primary200}`, padding: '4px 8px', background: colors.white,
                  }}
                >
                  <Icon name="retry" size={12} /> Retry AI
                </button>
              )}
            </div>
          )}

          {data?.loading ? (
            <div style={{ color: colors.gray500, fontSize: 13 }}>Still analyzing this trace...</div>
          ) : (
            <div style={{ display: 'grid', gridTemplateColumns: '220px 1fr', gap: 14 }}>
              {/* What was redacted summary (compact, category totals) */}
              <div style={{
                padding: '12px 14px', background: colors.white,
                border: `1px solid ${colors.gray200}`, borderRadius: 6,
                alignSelf: 'start',
              }}>
                <h4 style={reviewBoxTitle}>What was redacted</h4>
                {categories.length === 0 ? (
                  <div style={{ fontSize: 12.5, color: colors.gray500 }}>
                    Nothing matched the deterministic rules.
                    {aiUnavailable && <div style={{ marginTop: 4 }}>AI review unavailable.</div>}
                  </div>
                ) : (
                  <>
                    {categories.map((c, i) => (
                      <div key={i} style={rsItemStyle}>
                        <span>{c.label}</span>
                        <span
                          style={c.source === 'ai' ? {
                            fontSize: 10, padding: '0 5px', borderRadius: 3,
                            background: colors.primary100, color: colors.primary500,
                            fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
                            fontWeight: 600, letterSpacing: '0.02em',
                          } : rsItemV}
                          title={c.source === 'ai' ? 'Flagged by AI' : 'Matched by rules'}
                        >
                          {c.source === 'ai' ? 'AI' : '\u2713'}
                        </span>
                      </div>
                    ))}
                    <div style={{
                      ...rsItemStyle, marginTop: 6, paddingTop: 8,
                      borderTop: `1px solid ${colors.gray200}`, borderBottom: 'none',
                    }}>
                      <span>Total</span>
                      <span style={rsItemV}>{totalItems}</span>
                    </div>
                  </>
                )}
              </div>

              {/* Full redacted preview — scrollable, all messages */}
              <div>
                <div style={reviewBoxTitle}>Redacted preview</div>
                <div style={{
                  background: colors.white, border: `1px solid ${colors.gray200}`,
                  borderRadius: 6, padding: '12px 14px',
                  fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
                  fontSize: 11.5, color: colors.gray700, lineHeight: 1.55,
                  maxHeight: 420, overflow: 'auto',
                }}>
                  {data && data.messages.length === 0 ? (
                    <div style={{ color: colors.gray400 }}>(no message content)</div>
                  ) : data && data.messages.map((m, i) => (
                    <div key={i} style={{
                      marginBottom: 12, paddingBottom: 10,
                      borderBottom: i < data.messages.length - 1 ? `1px dashed ${colors.gray200}` : 'none',
                    }}>
                      <div style={{
                        color: m.role === 'user' ? colors.blue500 : colors.primary500,
                        fontWeight: 600, fontSize: 10.5, textTransform: 'uppercase',
                        marginBottom: 4,
                      }}>
                        {m.role} #{i + 1}
                      </div>
                      <div style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                        <RedactedText text={m.content || ''} />
                      </div>
                      {m.thinking && <ThinkingBlock text={m.thinking} />}
                      {m.tool_uses && m.tool_uses.length > 0 && (
                        <div style={{ marginTop: 6 }}>
                          {m.tool_uses.map((t, ti) => (
                            <ToolUseCard key={ti} tu={t} />
                          ))}
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            </div>
          )}

          <div style={{
            display: 'flex', alignItems: 'center', gap: 10,
            paddingTop: 14, marginTop: 14,
            borderTop: `1px dashed ${colors.gray200}`,
          }}>
            <span style={{ fontSize: 12, color: colors.gray500, marginRight: 'auto' }}>
              {approved
                ? 'Included with the redactions shown above.'
                : 'Include if the redacted version looks good. Remove if not.'}
            </span>
            <button onClick={onRemove} style={btnSecondary}>
              Remove from bundle
            </button>
            {!approved && (
              <button onClick={onApprove} style={btnPrimary}>
                <Icon name="check" size={13} />
                Include in bundle
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

const autoChipStyle: React.CSSProperties = {
  fontSize: 10.5, padding: '1px 7px', borderRadius: 10,
  background: colors.gray100, color: colors.gray600,
  fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
  fontVariantNumeric: 'tabular-nums',
};

const reviewBoxTitle: React.CSSProperties = {
  margin: '0 0 8px', fontSize: 11, textTransform: 'uppercase' as const,
  letterSpacing: '0.08em', color: colors.gray400, fontWeight: 600,
};

const rsItemStyle: React.CSSProperties = {
  display: 'flex', justifyContent: 'space-between',
  fontSize: 12.5, padding: '4px 0',
  color: colors.gray600, borderBottom: `1px dashed ${colors.gray200}`,
};

const rsItemV: React.CSSProperties = {
  color: colors.green500,
  fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
  fontVariantNumeric: 'tabular-nums',
};

// ============================================================
// Step 4: Package component
// ============================================================

interface PackageStepProps {
  stepperHeader: React.ReactNode;
  approvedCount: number;
  approvedList: ReadySession[];
  progress: number;
  log: string;
  failed: string | null;
  onRetry: () => void;
  onBack: () => void;
  globalStyles: React.ReactNode;
}

function PackageStep(p: PackageStepProps) {
  const [flying, setFlying] = useState<{ id: string; title: string }[]>([]);
  const [thump, setThump] = useState(0);

  // animate trace labels flying in over the course of progress
  useEffect(() => {
    if (p.failed) return;
    const timers: number[] = [];
    p.approvedList.forEach((s, i) => {
      timers.push(window.setTimeout(() => {
        setFlying((prev) => [...prev, { id: `${s.session_id}-${Date.now()}-${i}`, title: `${s.session_id.slice(0, 10)}.jsonl` }]);
      }, 400 + i * 220));
      timers.push(window.setTimeout(() => setThump((n) => n + 1), 400 + i * 220 + 620));
    });
    return () => timers.forEach((t) => window.clearTimeout(t));
  }, [p.approvedList, p.failed]);

  useEffect(() => {
    if (flying.length === 0) return;
    const t = window.setTimeout(() => {
      setFlying((prev) => prev.slice(1));
    }, 900);
    return () => window.clearTimeout(t);
  }, [flying]);

  return (
    <div style={{ padding: '32px 24px 48px', maxWidth: SHARE_SHELL_WIDTH, margin: '0 auto' }}>
      {p.globalStyles}
      {p.stepperHeader}
      <div style={{
        padding: '56px 24px 24px', maxWidth: 680, margin: '0 auto', textAlign: 'center',
      }}>
        <div style={{ width: 180, height: 240, margin: '0 auto 24px', position: 'relative' }}>
          {flying.map((f) => (
            <div key={f.id} style={{
              position: 'absolute', left: '50%', top: 0, width: 140,
              transform: 'translateX(-50%)',
              padding: '6px 10px', background: colors.primary50,
              border: `1px solid ${colors.primary400}`, borderRadius: 4,
              fontSize: 10.5, fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
              color: colors.primary500, textAlign: 'left' as const,
              whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
              boxShadow: '0 8px 24px rgba(180,125,8,0.25)',
              animation: 'clawPkgDrop 820ms cubic-bezier(.45,.05,.6,1) forwards',
              zIndex: 2,
            }}>{f.title}</div>
          ))}
          <div
            key={thump}
            style={{
              position: 'absolute', inset: '40px 10px 0 10px',
              background: `linear-gradient(180deg, ${colors.gray200} 0%, ${colors.gray100} 100%)`,
              border: `1px solid ${colors.gray300}`, borderRadius: 6,
              boxShadow: '0 30px 60px -25px rgba(0,0,0,0.3)',
              overflow: 'hidden',
              animation: thump > 0 ? 'clawThump 240ms ease-out' : undefined,
            }}
          >
            <div style={{ position: 'absolute', inset: '60px 0 auto 0', display: 'grid', placeItems: 'center', color: colors.gray500 }}>
              <Icon name="lock" size={40} />
            </div>
            <div style={{
              position: 'absolute', bottom: 20, left: 0, right: 0, textAlign: 'center',
              fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
              fontSize: 11.5, color: colors.gray600,
            }}>
              draft-bundle.zip
            </div>
          </div>
        </div>
        <h2 style={{ fontSize: 20, fontWeight: 500, letterSpacing: '-0.01em', margin: '0 0 6px', color: colors.gray900 }}>
          {p.failed ? 'Packaging failed' : 'Packaging your bundle'}
        </h2>
        <p style={{ color: colors.gray500, fontSize: 13.5, margin: '0 0 20px' }}>
          {p.failed
            ? p.failed
            : <>Compressing {p.approvedCount} approved trace{p.approvedCount === 1 ? '' : 's'} into a single redacted zip.</>}
        </p>
        <div style={{
          width: 260, margin: '0 auto 16px', height: 4,
          background: colors.gray200, borderRadius: 2, overflow: 'hidden',
        }}>
          <div style={{
            height: '100%', width: `${p.progress}%`,
            background: `linear-gradient(90deg, ${colors.primary500}, ${colors.green500})`,
            transition: 'width 300ms ease',
          }} />
        </div>
        <div style={{
          fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
          fontSize: 11, color: colors.gray500, height: 14,
          fontVariantNumeric: 'tabular-nums',
        }}>
          {p.log}
        </div>
        {p.failed && (
          <div style={{ marginTop: 20, display: 'flex', gap: 10, justifyContent: 'center' }}>
            <button onClick={p.onBack} style={btnSecondary}>Back to review</button>
            <button onClick={p.onRetry} style={btnPrimary}>
              <Icon name="retry" size={13} /> Retry
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

// ============================================================
// Step 5: Done component
// ============================================================

interface DoneStepProps {
  stepperHeader: React.ReactNode;
  bundle: { traces: number; created: string; approxSize: string } | null;
  onDownloadAgain: () => void;
  onNew: () => void;
  globalStyles: React.ReactNode;
  error: string | null;
}

function DoneStep(p: DoneStepProps) {
  const [peek, setPeek] = useState(false);
  const [confettiPieces] = useState(() => {
    const palette = ['#b47d08', '#558745', '#5f7191', '#c4890a', '#a09a8f'];
    return Array.from({ length: 28 }, (_, i) => ({
      id: i,
      left: 20 + Math.random() * 60,
      dx: (Math.random() - 0.5) * 500,
      dy: -50 - Math.random() * 360,
      r: Math.random() * 720 - 360,
      color: palette[i % palette.length],
      delay: Math.random() * 300,
    }));
  });

  const traces = p.bundle?.traces ?? 0;

  return (
    <div style={{ padding: '32px 24px 48px', maxWidth: SHARE_SHELL_WIDTH, margin: '0 auto' }}>
      {p.globalStyles}
      {p.stepperHeader}
      <div style={{ position: 'relative', padding: '56px 24px 24px', maxWidth: 680, margin: '0 auto', textAlign: 'center' }}>
        <div style={{ position: 'absolute', inset: 0, pointerEvents: 'none', overflow: 'hidden' }}>
          {confettiPieces.map((c) => (
            <span key={c.id} style={{
              position: 'absolute', top: '40%', left: `${c.left}%`,
              width: 6, height: 10, borderRadius: 1,
              background: c.color, opacity: 0,
              ['--cdx' as string]: `${c.dx}px`,
              ['--cdy' as string]: `${c.dy}px`,
              ['--cr' as string]: `${c.r}deg`,
              animation: 'clawConfetti 1800ms ease-out forwards',
              animationDelay: `${c.delay}ms`,
            } as React.CSSProperties} />
          ))}
        </div>

        <div style={{
          width: 72, height: 72, margin: '0 auto 24px',
          borderRadius: '50%', background: colors.green100, color: colors.green500,
          display: 'grid', placeItems: 'center', position: 'relative',
        }}>
          <span style={{
            position: 'absolute', inset: -10, borderRadius: '50%',
            border: `1px solid ${colors.green500}`, opacity: 0.4,
            animation: 'clawRingOut 1.6s ease-out forwards',
          }} />
          <span style={{
            position: 'absolute', inset: -20, borderRadius: '50%',
            border: `1px solid ${colors.green500}`, opacity: 0.2,
            animation: 'clawRingOut 2s ease-out 0.2s forwards',
          }} />
          <svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2.5} strokeLinecap="round" strokeLinejoin="round" style={{ animation: 'clawCheckIn 500ms cubic-bezier(.2,1.4,.3,1) both' }}>
            <path d="M5 12l4 4 10-10" />
          </svg>
        </div>

        <div style={{
          display: 'inline-flex', alignItems: 'center', gap: 6,
          padding: '4px 10px', borderRadius: 12,
          background: colors.white, border: `1px solid ${colors.gray200}`,
          fontSize: 11.5, color: colors.gray500,
          fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
          marginBottom: 18,
        }}>
          <span style={{ width: 6, height: 6, borderRadius: '50%', background: colors.green500 }} />
          local &middot; redacted &middot; not uploaded
        </div>

        <h2 style={{ fontSize: 22, fontWeight: 500, letterSpacing: '-0.02em', margin: '0 0 8px', color: colors.gray900 }}>
          Your bundle is saving to downloads
        </h2>
        <p style={{ color: colors.gray500, margin: '0 0 24px', fontSize: 14 }}>
          If your browser didn&rsquo;t catch the save, you can download the same zip again.
        </p>

        {p.bundle && (
          <div style={{
            background: colors.white, border: `1px solid ${colors.gray200}`, borderRadius: 8,
            padding: '16px 18px', display: 'grid', gridTemplateColumns: '1fr 1fr 1fr',
            gap: 20, textAlign: 'left' as const, marginBottom: 22,
          }}>
            <div>
              <div style={statLabelStyle}>Traces</div>
              <div style={statValueStyle}>{p.bundle.traces}</div>
            </div>
            <div>
              <div style={statLabelStyle}>File size</div>
              <div style={statValueStyle}>~{p.bundle.approxSize}</div>
            </div>
            <div>
              <div style={statLabelStyle}>Created</div>
              <div style={statValueStyle}>{p.bundle.created}</div>
            </div>
          </div>
        )}

        <div style={{
          margin: '0 auto 22px',
          display: 'flex', gap: 10, justifyContent: 'center', flexWrap: 'wrap',
        }}>
          <button
            onClick={p.onDownloadAgain}
            style={{
              display: 'inline-flex', alignItems: 'center', gap: 8,
              padding: '11px 20px', background: colors.white, color: colors.gray900,
              border: `1px solid ${colors.gray300}`, borderRadius: 8,
              fontSize: 14, fontWeight: 600, cursor: 'pointer',
            }}
          >
            <Icon name="download" size={15} /> Download zip
          </button>
          <a
            href="https://data.rayward.ai"
            target="_blank"
            rel="noopener noreferrer"
            style={{
              display: 'inline-flex', alignItems: 'center', gap: 8,
              padding: '11px 20px', background: colors.primary500, color: colors.white,
              borderRadius: 8, fontSize: 14, fontWeight: 600,
              textDecoration: 'none',
            }}
          >
            Share with Rayward &rarr;
          </a>
        </div>
        <div style={{ fontSize: 12, color: colors.gray500, marginBottom: 8 }}>
          Upload your bundle at <span style={{ fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace' }}>data.rayward.ai</span> to contribute to research.
        </div>

        <div style={{
          margin: '20px auto', maxWidth: 480, textAlign: 'left' as const,
          padding: '14px 16px', background: colors.white,
          border: `1px solid ${colors.gray200}`, borderRadius: 8,
          fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
          fontSize: 11.5, color: colors.gray600, lineHeight: 1.8, overflow: 'hidden',
        }}>
          <div style={{
            display: 'flex', alignItems: 'center', justifyContent: 'space-between',
            fontFamily: 'Inter, system-ui', fontSize: 11, color: colors.gray400,
            textTransform: 'uppercase' as const, letterSpacing: '0.08em',
            marginBottom: 8, fontWeight: 600,
            paddingBottom: 8, borderBottom: `1px solid ${colors.gray200}`,
          }}>
            <span>Inside the zip</span>
            <button
              onClick={() => setPeek(!peek)}
              style={{
                fontFamily: 'Inter, system-ui', color: colors.primary500,
                fontSize: 11, letterSpacing: 0, textTransform: 'none' as const,
                background: 'transparent', border: 'none', cursor: 'pointer',
              }}
            >
              {peek ? 'Hide' : 'Show file list'}
            </button>
          </div>
          <div style={manifestRowStyle}><span>&rsaquo; manifest.json</span><span style={{ color: colors.gray900 }}>0.3 KB</span></div>
          <div style={manifestRowStyle}><span>&rsaquo; redaction-audit.json</span><span style={{ color: colors.gray900 }}>2.1 KB</span></div>
          {peek && traces > 0 && Array.from({ length: traces }).map((_, i) => (
            <div key={i} style={manifestRowStyle}>
              <span>&rsaquo; traces/t_{(i + 1).toString().padStart(3, '0')}.jsonl</span>
              <span style={{ color: colors.gray900 }}>{(50 + i * 17) % 120 + 20} KB</span>
            </div>
          ))}
          {!peek && traces > 0 && (
            <div style={manifestRowStyle}>
              <span>&rsaquo; traces/ ({traces} files)</span>
              <span style={{ color: colors.gray900 }}>~{p.bundle?.approxSize}</span>
            </div>
          )}
        </div>

        <div style={{ display: 'flex', justifyContent: 'center', gap: 10, color: colors.gray500, fontSize: 13 }}>
          <button onClick={p.onNew} style={{ ...btnGhost, color: colors.primary500, fontSize: 13, padding: '6px 10px' }}>
            Start a new bundle
          </button>
        </div>

        <div style={{
          margin: '28px auto 0', padding: '14px 16px', maxWidth: 480,
          background: colors.white, border: `1px solid ${colors.gray200}`, borderRadius: 8,
          textAlign: 'left' as const, fontSize: 12.5, color: colors.gray600, lineHeight: 1.55,
        }}>
          <strong style={{ color: colors.gray900, fontWeight: 500 }}>What happens next.</strong>{' '}
          If you choose to share, your bundle will be used{' '}
          <strong style={{ color: colors.gray900, fontWeight: 500 }}>only for model evaluation and model training</strong>.
          No advertising. No resale. No profile building.
          <div style={doneMiniRow}>
            <span style={{ color: colors.green500 }}>&#x2713;</span>
            <span>Original trace never left your device</span>
          </div>
          <div style={doneMiniRow}>
            <span style={{ color: colors.green500 }}>&#x2713;</span>
            <span>Zip contains the redacted copy only</span>
          </div>
          <div style={doneMiniRow}>
            <span style={{ color: colors.green500 }}>&#x2713;</span>
            <span>You approved every trace before packaging</span>
          </div>
        </div>
      </div>
    </div>
  );
}

const statLabelStyle: React.CSSProperties = {
  fontSize: 11, textTransform: 'uppercase' as const, letterSpacing: '0.08em',
  color: colors.gray400, marginBottom: 6, fontWeight: 600,
};
const statValueStyle: React.CSSProperties = {
  fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
  fontSize: 15, color: colors.gray900, fontVariantNumeric: 'tabular-nums',
};
const manifestRowStyle: React.CSSProperties = {
  display: 'flex', justifyContent: 'space-between', gap: 10,
};
const doneMiniRow: React.CSSProperties = {
  display: 'flex', gap: 8, alignItems: 'center', marginTop: 6,
  fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace', fontSize: 11.5,
};
