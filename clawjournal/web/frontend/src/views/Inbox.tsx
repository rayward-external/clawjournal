import { useState, useEffect, useCallback, useRef } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import type { Session, Stats } from '../types.ts';
import { api } from '../api.ts';
import { useToast } from '../components/Toast.tsx';
import { ConfirmDialog } from '../components/ConfirmDialog.tsx';
import { Spinner } from '../components/Spinner.tsx';
import { LABELS } from '../components/BadgeChip.tsx';
import { GettingStartedGuide } from '../components/GettingStartedGuide.tsx';
import { ZeroState } from '../components/ZeroState.tsx';
import { colors, selectStyle, btnPrimary, btnDanger, btnSecondary } from '../theme.ts';

// Show only the top few task-type chips by default; the long tail collapses
// behind a "More (N)" toggle. Keeps the toolbar to one calm row instead of ~17
// chips wrapping across the viewport.
const TYPE_CHIP_PREVIEW_LIMIT = 6;
const GETTING_STARTED_DISMISSED_KEY = 'cj.gettingStartedGuideV2Dismissed';

function failureBadge(score: number | null): string {
  if (score == null) return '\u2014';
  // Noun-first with an explicit /5 denominator so it reads as a score, not a
  // count of failures. "Value" signals this rates the trace's worth as a
  // failure example, not how badly the session failed.
  return `Failure value ${score}/5`;
}

function outcomeText(badge: string | null): string {
  if (!badge) return '';
  const b = badge.toLowerCase();
  // New resolution labels
  if (b === 'resolved') return '\u2713 resolved';
  if (b === 'partial') return '~ partial';
  if (b === 'interrupted') return '~ interrupted';
  if (b === 'inconclusive') return '\u2014 inconclusive';
  if (b === 'failed') return '\u2717 failed';
  if (b === 'abandoned') return '\u2717 abandoned';
  if (b === 'exploratory') return '\u2014 exploratory';
  if (b === 'trivial') return '\u2014 trivial';
  if (b === 'unscored') return '\u2014 unscored';
  if (b === 'unknown') return '\u2014 unknown';
  // Legacy outcome labels
  if (b.includes('pass')) return '\u2713 passed';
  if (b.includes('fail')) return '\u2717 failed';
  if (b.includes('analysis')) return '\u2014 analysis';
  if (b.includes('completed')) return '\u2014 completed';
  if (b.includes('errored')) return '\u2717 errored';
  return '';
}

// Plain-English gloss for each outcome badge, shown on hover so the
// similar-looking states (notably failed vs errored) are distinguishable.
function outcomeTooltip(badge: string | null): string {
  if (!badge) return '';
  const b = badge.toLowerCase();
  if (b === 'resolved') return 'Outcome: the task was resolved successfully.';
  // 'partial' here is the AI-judge label (useful progress, not fully complete) — the heuristic
  // "user spoke last" case normalizes to 'interrupted' before reaching the Inbox.
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

function riskFlags(session: Session): string[] {
  const flags: string[] = [];
  const risks = session.risk_level;
  if (!risks) return flags;
  try {
    const arr: string[] = typeof risks === 'string' ? JSON.parse(risks) : risks;
    for (const r of arr) {
      if (r.includes('secret')) flags.push('secrets');
      else if (r.includes('name')) flags.push('names');
      else if (r.includes('url')) flags.push('private URLs');
      else if (r.includes('review')) flags.push('review needed');
    }
  } catch { /* ignore */ }
  return flags;
}

/* ------------------------------------------------------------------ */
/*  Type chip colors — derived from the same palette as BadgeChip     */
/* ------------------------------------------------------------------ */

const TYPE_COLORS: Record<string, string> = {
  feature: '#7c3aed',
  refactor: '#2563eb',
  analysis: '#0891b2',
  testing: '#16a34a',
  documentation: '#6b7280',
  exploration: '#d97706',
  review: '#0d9488',
  configuration: '#6366f1',
  migration: '#c026d3',
  trivial: '#9ca3af',
  debugging: '#dc2626',
};

const HASH_HEX_PALETTE = ['#7c3aed', '#2563eb', '#0891b2', '#16a34a', '#d97706', '#0d9488', '#6366f1', '#c026d3', '#dc2626', '#059669'];

function typeColor(t: string): string {
  if (TYPE_COLORS[t]) return TYPE_COLORS[t];
  let hash = 0;
  for (let i = 0; i < t.length; i++) hash = t.charCodeAt(i) + ((hash << 5) - hash);
  return HASH_HEX_PALETTE[Math.abs(hash) % HASH_HEX_PALETTE.length];
}

function sourceInfo(s: Session): { label: string; color: string } {
  if (s.source === 'codex') {
    return s.client_origin === 'desktop'
      ? { label: 'Codex Desktop', color: '#0891b2' }
      : { label: 'Codex', color: '#16a34a' };
  }
  if (s.source === 'claude') {
    if (s.client_origin === 'desktop' || s.runtime_channel === 'local-agent')
      return { label: 'Claude Desktop', color: '#7c3aed' };
    return { label: 'Claude Code', color: '#d97706' };
  }
  if (s.source === 'claude-science')
    return { label: 'Claude Science', color: '#9333ea' };
  if (s.source === 'openclaw')
    return { label: 'OpenClaw', color: '#6b7280' };
  if (s.source === 'workbuddy')
    return { label: 'WorkBuddy', color: '#0f766e' };
  return { label: s.source, color: '#6b7280' };
}

/** Convert a hex color like #7c3aed to rgba with given alpha (0-1). */
function hexAlpha(hex: string, alpha: number): string {
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return `rgba(${r},${g},${b},${alpha})`;
}

export function Inbox() {
  const { toast } = useToast();
  const navigate = useNavigate();
  const [searchBox, setSearchBox] = useState('');
  const [sessions, setSessions] = useState<Session[]>([]);
  const [stats, setStats] = useState<Stats>({ total: 0, by_status: {}, by_source: {}, by_project: {}, by_task_type: {} });
  const [showGettingStartedGuide, setShowGettingStartedGuide] = useState(() => {
    try {
      return localStorage.getItem(GETTING_STARTED_DISMISSED_KEY) !== '1';
    } catch {
      return true;
    }
  });
  const [loading, setLoading] = useState(false);
  // Distinguishes "not yet loaded" from "genuinely empty" so the zero-state /
  // empty-state don't flash for a frame before the first fetch resolves.
  const [loaded, setLoaded] = useState(false);
  const [offset, setOffset] = useState(0);
  const [pageSize, setPageSize] = useState(10);
  const [hasMore, setHasMore] = useState(false);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [expandedMessages, setExpandedMessages] = useState<Record<string, Array<{ role: string; content: string; tool_uses?: Array<{ tool: string }> }>>>({});
  const [sort, setSort] = useState('ai_failure_value_score:desc');
  const [showFilters, setShowFilters] = useState(false);
  const [sourceFilter, setSourceFilter] = useState<string | null>(null);
  const [projectFilter, setProjectFilter] = useState<string | null>(null);
  const [recoveryFilter, setRecoveryFilter] = useState<string | null>(null);
  const [attributionFilter, setAttributionFilter] = useState<string | null>(null);
  const [modeFilter, setModeFilter] = useState<string | null>(null);
  const [showAllTaskTypes, setShowAllTaskTypes] = useState(false);

  // Agent-classified type filter (dynamic, not hardcoded)
  const [typeFilter, setTypeFilter] = useState<string | null>(null);

  // Bulk selection
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());

  // Keyboard navigation
  const [focusIndex, setFocusIndex] = useState(-1);
  const listRef = useRef<HTMLDivElement>(null);

  // Refs for stable keyboard handler access (avoids stale closures)
  const sessionsRef = useRef(sessions);
  sessionsRef.current = sessions;
  const focusRef = useRef(focusIndex);
  focusRef.current = focusIndex;
  const expandedIdRef = useRef(expandedId);
  expandedIdRef.current = expandedId;
  const expandedMsgsRef = useRef(expandedMessages);
  expandedMsgsRef.current = expandedMessages;
  const loadRequestSeqRef = useRef(0);
  // Session ids with an in-flight redacted-content fetch (single-flight guard).
  const loadingIdsRef = useRef<Set<string>>(new Set());

  // Confirm dialog
  const [confirm, setConfirm] = useState<{ action: string; ids: string[] } | null>(null);

  const loadStats = useCallback(async () => {
    try {
      const s = await api.stats();
      setStats(s);
    } catch { /* ignore */ }
  }, []);

  const loadSessions = useCallback(async (currentOffset: number, append: boolean) => {
    const requestSeq = loadRequestSeqRef.current + 1;
    loadRequestSeqRef.current = requestSeq;
    setLoading(true);
    const [sortField, sortOrder] = sort.split(':');
    try {
      const data = await api.sessions.list({
        status: ['new', 'shortlisted'],
        source: sourceFilter,
        project: projectFilter,
        task_type: typeFilter,
        recovery_label: recoveryFilter,
        failure_attribution: attributionFilter,
        failure_mode: modeFilter,
        sort: sortField,
        order: sortOrder,
        limit: pageSize + 1,
        offset: currentOffset,
      });
      if (requestSeq !== loadRequestSeqRef.current) return;
      const visibleRows = data.slice(0, pageSize);
      setSessions(prev => append ? [...prev, ...visibleRows] : visibleRows);
      // Sessions are an opt-out bulk-selection surface: select every row as it
      // becomes visible, while preserving any choices already made on rows
      // from earlier pages.
      setSelectedIds(prev => {
        if (!append) return new Set(visibleRows.map(s => s.session_id));
        const next = new Set(prev);
        visibleRows.forEach(s => next.add(s.session_id));
        return next;
      });
      setOffset(currentOffset);
      setHasMore(data.length > pageSize);
    } catch (e) {
      if (requestSeq !== loadRequestSeqRef.current) return;
      toast(e instanceof Error ? e.message : 'Failed to load sessions', 'error');
    }
    finally {
      if (requestSeq === loadRequestSeqRef.current) {
        setLoading(false);
        setLoaded(true);
      }
    }
  }, [sort, sourceFilter, projectFilter, typeFilter, recoveryFilter, attributionFilter, modeFilter, pageSize, toast]);

  useEffect(() => {
    setOffset(0);
    setHasMore(false);
    setSelectedIds(new Set());
    setFocusIndex(-1);
    loadSessions(0, false);
    loadStats();
  }, [sort, sourceFilter, projectFilter, typeFilter, recoveryFilter, attributionFilter, modeFilter, pageSize, loadSessions, loadStats]);

  // Keyboard navigation — uses refs to avoid stale closures
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.target as HTMLElement).tagName === 'INPUT' || (e.target as HTMLElement).tagName === 'TEXTAREA' || (e.target as HTMLElement).tagName === 'SELECT') return;
      const cur = sessionsRef.current;
      const fi = focusRef.current;

      if (e.key === 'j' || e.key === 'ArrowDown') {
        e.preventDefault();
        setFocusIndex(prev => Math.min(prev + 1, cur.length - 1));
      } else if (e.key === 'k' || e.key === 'ArrowUp') {
        e.preventDefault();
        setFocusIndex(prev => Math.max(prev - 1, 0));
      } else if (e.key === 'Enter' && fi >= 0 && fi < cur.length) {
        e.preventDefault();
        handleExpand(cur[fi].session_id);
      }
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, []); // stable — reads from refs

  useEffect(() => {
    if (focusIndex >= 0 && listRef.current) {
      const el = listRef.current.children[focusIndex] as HTMLElement | undefined;
      el?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    }
  }, [focusIndex]);

  const handleAction = async (sessionId: string, action: 'approved' | 'blocked') => {
    try {
      await api.sessions.update(sessionId, { status: action });
      setSessions(prev => {
        const next = prev.filter(s => s.session_id !== sessionId);
        // Clamp focusIndex so it doesn't go out of bounds
        setFocusIndex(fi => fi >= next.length ? Math.max(next.length - 1, 0) : fi);
        return next;
      });
      setSelectedIds(prev => { const next = new Set(prev); next.delete(sessionId); return next; });
      loadStats();
      toast(action === 'approved' ? 'Session approved' : 'Session skipped', 'success');
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Action failed', 'error');
    }
  };

  const handleBulkAction = async (action: 'approved' | 'blocked') => {
    const ids = [...selectedIds];
    const verb = action === 'approved' ? 'approved' : 'skipped';
    // Per-item settle so one failed update doesn't strand the rest: remove only
    // the ones that actually succeeded and keep the failures selected.
    const results = await Promise.allSettled(ids.map(id => api.sessions.update(id, { status: action })));
    const okIds = new Set(ids.filter((_, i) => results[i].status === 'fulfilled'));
    const failed = ids.length - okIds.size;
    setSessions(prev => prev.filter(s => !okIds.has(s.session_id)));
    setSelectedIds(prev => new Set([...prev].filter(id => !okIds.has(id))));
    loadStats();
    setConfirm(null);
    if (okIds.size > 0) {
      toast(`${okIds.size} session${okIds.size > 1 ? 's' : ''} ${verb}`, 'success');
    }
    if (failed > 0) {
      toast(`${failed} session${failed > 1 ? 's' : ''} failed to update`, 'error');
    }
  };

  const handleExpand = async (sessionId: string) => {
    if (expandedIdRef.current === sessionId) {
      setExpandedId(null);
      return;
    }
    // Open the row immediately so the loading placeholder renders while the
    // redacted-content fetch runs. The fetch can take many seconds for large
    // sessions; without this optimistic open the click feels dead (nothing
    // visibly happens until the request resolves).
    setExpandedId(sessionId);
    // Single-flight per session: if a fetch is already in flight, a quick
    // collapse/reopen must not start a second request. Two overlapping
    // promises would race on the same cache entry, and a failing retry could
    // clobber an already-successful response with "(unable to load)".
    if (!expandedMsgsRef.current[sessionId] && !loadingIdsRef.current.has(sessionId)) {
      loadingIdsRef.current.add(sessionId);
      try {
        const detail = await api.sessions.redacted(sessionId);
        const msgs = (detail.messages || []).map((m: { role: string; content: string; tool_uses?: Array<{ tool: string }> }) => ({
          role: m.role,
          content: m.content || '',
          tool_uses: m.tool_uses,
        }));
        setExpandedMessages(prev => ({ ...prev, [sessionId]: msgs }));
      } catch {
        setExpandedMessages(prev => ({ ...prev, [sessionId]: [{ role: 'system', content: '(unable to load)' }] }));
      } finally {
        loadingIdsRef.current.delete(sessionId);
      }
    }
  };

  const toggleSelect = (id: string) => {
    setSelectedIds(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };

  const toggleSelectAll = () => {
    if (selectedIds.size === sessions.length) {
      setSelectedIds(new Set());
    } else {
      setSelectedIds(new Set(sessions.map(s => s.session_id)));
    }
  };

  // Derive agent-classified types from stats (dynamic, not hardcoded)
  const taskTypes = Object.entries(stats.by_task_type ?? {})
    .sort(([, a], [, b]) => b - a);
  const visibleTaskTypes = showAllTaskTypes ? taskTypes : (() => {
    const top = taskTypes.slice(0, TYPE_CHIP_PREVIEW_LIMIT);
    if (typeFilter && !top.some(([type]) => type === typeFilter)) {
      const active = taskTypes.find(([type]) => type === typeFilter);
      return active ? [...top, active] : top;
    }
    return top;
  })();
  const hiddenTaskTypeCount = Math.max(0, taskTypes.length - TYPE_CHIP_PREVIEW_LIMIT);

  const dismissGettingStartedGuide = () => {
    setShowGettingStartedGuide(false);
    try {
      localStorage.setItem(GETTING_STARTED_DISMISSED_KEY, '1');
    } catch { /* ignore */ }
  };

  const hasActiveFilter = !!(typeFilter || sourceFilter || projectFilter
    || recoveryFilter || attributionFilter || modeFilter);
  const clearAllFilters = () => {
    setTypeFilter(null);
    setSourceFilter(null);
    setProjectFilter(null);
    setRecoveryFilter(null);
    setAttributionFilter(null);
    setModeFilter(null);
  };

  return (
    <div style={{ padding: '14px 20px' }}>
      {/* Header */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <h2 style={{ margin: 0, fontSize: '20px', fontWeight: 600, color: colors.gray900 }}>Sessions</h2>
          <span style={{ fontSize: 13, color: colors.gray400 }}>{stats.total} sessions</span>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <input
            type="search"
            value={searchBox}
            onChange={e => setSearchBox(e.target.value)}
            onKeyDown={e => {
              // Full-text search lives in the dedicated results view; the toolbar
              // box is just the entry point and hands the query off via ?q=.
              if (e.key === 'Enter' && searchBox.trim()) {
                navigate(`/search?q=${encodeURIComponent(searchBox.trim())}`);
              }
            }}
            placeholder="Search…"
            aria-label="Search sessions"
            style={{
              ...selectStyle,
              width: 150,
              cursor: 'text',
            }}
          />
          <select value={sort} onChange={e => setSort(e.target.value)} style={selectStyle}>
            <option value="ai_failure_value_score:desc">Most instructive failures</option>
            <option value="ai_quality_score:desc">Highest productivity</option>
            <option value="start_time:desc">Newest first</option>
            <option value="start_time:asc">Oldest first</option>
          </select>
          <select
            value={pageSize}
            onChange={e => setPageSize(Number(e.target.value))}
            aria-label="Sessions per page"
            style={selectStyle}
          >
            <option value={10}>10 / page</option>
            <option value={20}>20 / page</option>
            <option value={50}>50 / page</option>
            <option value={100}>100 / page</option>
          </select>
          <button
            onClick={() => setShowFilters(!showFilters)}
            style={{ background: 'none', border: 'none', color: colors.primary500, fontSize: '13px', cursor: 'pointer', padding: 0 }}
          >
            {showFilters ? 'Hide filters' : 'Filters'}
          </button>
        </div>
      </div>

      {loaded && stats.total === 0 && sessions.length === 0 && !typeFilter && (
        <ZeroState />
      )}

      {showGettingStartedGuide && stats.total > 0 && sessions.length > 0 && !typeFilter && (
        <GettingStartedGuide stats={stats} onDismiss={dismissGettingStartedGuide} />
      )}

      {selectedIds.size > 0 && (
        <div style={{
          display: 'flex', alignItems: 'center', gap: 10,
          padding: '8px 12px', marginBottom: 8,
          background: colors.primary50, border: `1px solid ${colors.primary200}`, borderRadius: 8,
        }}>
          <span style={{ fontSize: 13, fontWeight: 600, color: colors.gray800 }}>
            {selectedIds.size} selected
          </span>
          <button onClick={toggleSelectAll} style={{ ...btnSecondary, padding: '4px 10px', fontSize: 12 }}>
            {selectedIds.size === sessions.length ? 'Clear' : 'Select all'}
          </button>
          <div style={{ flex: 1 }} />
          <button
            onClick={() => setConfirm({ action: 'approved', ids: [...selectedIds] })}
            style={{ ...btnPrimary, padding: '4px 12px', fontSize: 12, fontWeight: 600 }}
          >
            Approve
          </button>
          <button
            onClick={() => setConfirm({ action: 'blocked', ids: [...selectedIds] })}
            style={{ ...btnDanger, padding: '4px 12px', fontSize: 12, fontWeight: 600 }}
          >
            Skip
          </button>
        </div>
      )}

      {showFilters && (
        <div style={{ display: 'flex', gap: '6px', marginBottom: '8px' }}>
          <select
            value={sourceFilter || ''}
            onChange={e => setSourceFilter(e.target.value || null)}
            style={selectStyle}
          >
            <option value="">All sources</option>
            {Object.keys(stats.by_source).sort().map(src => (
              <option key={src} value={src}>{src} ({stats.by_source[src]})</option>
            ))}
          </select>
          <select
            value={recoveryFilter || ''}
            onChange={e => setRecoveryFilter(e.target.value || null)}
            style={selectStyle}
          >
            <option value="">All recovery</option>
            <option value="user_corrected_recovery">User-corrected</option>
            <option value="self_recovered">Self-recovered</option>
            <option value="unrecovered">Unrecovered</option>
            <option value="blocked">Blocked</option>
          </select>
          <select
            value={attributionFilter || ''}
            onChange={e => setAttributionFilter(e.target.value || null)}
            style={selectStyle}
          >
            <option value="">All attribution</option>
            <option value="agent_caused">Agent-caused</option>
            <option value="environment">Environment</option>
            <option value="preexisting_problem">Preexisting</option>
            <option value="user_redirect">User redirect</option>
            <option value="unclear">Unclear</option>
          </select>
          <select
            value={modeFilter || ''}
            onChange={e => setModeFilter(e.target.value || null)}
            style={selectStyle}
          >
            <option value="">All modes</option>
            <option value="task_framing">Task framing</option>
            <option value="method_selection">Method selection</option>
            <option value="context_handling">Context handling</option>
            <option value="execution_error">Execution error</option>
            <option value="reasoning_fabrication">Reasoning / fabrication</option>
            <option value="revision_failure">Revision failure</option>
            <option value="verification_skipped">Verification skipped</option>
            <option value="deliverable_defect">Deliverable defect</option>
            <option value="communication_error">Communication error</option>
            <option value="collaboration_error">Collaboration error</option>
            <option value="safety_security">Safety / security</option>
            <option value="efficiency_waste">Efficiency / waste</option>
          </select>
          <select
            value={projectFilter || ''}
            onChange={e => setProjectFilter(e.target.value || null)}
            style={selectStyle}
          >
            <option value="">All projects</option>
            {Object.keys(stats.by_project).sort().map(proj => (
              <option key={proj} value={proj}>{proj} ({stats.by_project[proj]})</option>
            ))}
          </select>
        </div>
      )}

      {/* Agent-classified type chips — dynamic from backend data */}
      {taskTypes.length > 0 && (
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: 10 }}>
          <button
            onClick={() => setTypeFilter(null)}
            style={{
              padding: '4px 12px',
              borderRadius: 9999,
              fontSize: 13,
              fontWeight: typeFilter === null ? 700 : 500,
              cursor: 'pointer',
              border: typeFilter === null ? `2px solid ${colors.primary500}` : `1px solid ${colors.gray300}`,
              background: typeFilter === null ? colors.primary50 : colors.white,
              color: typeFilter === null ? colors.primary500 : colors.gray500,
            }}
          >
            All ({stats.total})
          </button>
          {visibleTaskTypes.map(([type, count]) => {
            const active = typeFilter === type;
            const c = typeColor(type);
            return (
              <button
                key={type}
                onClick={() => setTypeFilter(active ? null : type)}
                style={{
                  padding: '4px 12px',
                  borderRadius: 9999,
                  fontSize: 13,
                  fontWeight: active ? 700 : 500,
                  cursor: 'pointer',
                  border: active ? `2px solid ${c}` : `1px solid ${colors.gray300}`,
                  background: active ? hexAlpha(c, 0.1) : colors.white,
                  color: active ? c : colors.gray600,
                  transition: 'all 0.15s',
                }}
              >
                <span style={{
                  display: 'inline-block',
                  width: 8,
                  height: 8,
                  borderRadius: '50%',
                  background: c,
                  marginRight: 6,
                  verticalAlign: 'middle',
                }} />
                {LABELS[type] ?? type.replace(/_/g, ' ')} ({count})
              </button>
            );
          })}
          {hiddenTaskTypeCount > 0 && (
            <button
              onClick={() => setShowAllTaskTypes(prev => !prev)}
              style={{
                padding: '4px 12px',
                borderRadius: 9999,
                fontSize: 13,
                fontWeight: 600,
                cursor: 'pointer',
                border: `1px solid ${colors.gray300}`,
                background: colors.gray50,
                color: colors.gray600,
              }}
            >
              {showAllTaskTypes ? 'Show fewer' : `More (${hiddenTaskTypeCount})`}
            </button>
          )}
        </div>
      )}

      {/* spacer */}

      {/* spacer */}

      {/* All-reviewed / filtered-empty state. The brand-new-install case
          (stats.total === 0) is handled by <ZeroState /> above. */}
      {loaded && sessions.length === 0 && !loading && stats.total > 0 && (
        <div style={{
          background: colors.white, border: `1px solid ${colors.gray200}`, borderRadius: '8px',
          padding: '24px 20px', textAlign: 'center', marginTop: '12px',
        }}>
          <h3 style={{ margin: '0 0 4px', fontSize: '15px', fontWeight: 600, color: hasActiveFilter ? colors.gray700 : colors.green500 }}>
            {hasActiveFilter
              ? (typeFilter ? `No "${LABELS[typeFilter] ?? typeFilter}" sessions found` : 'No sessions match your filters')
              : 'You’re all caught up'}
          </h3>
          <p style={{ margin: '0 0 10px', fontSize: '13px', color: colors.gray500 }}>
            {hasActiveFilter
              ? 'Try adjusting or clearing your filters.'
              : 'Every session has been reviewed. Open Share to package approved traces, or run clawjournal scan to pick up new ones.'}
          </p>
          {hasActiveFilter && (
            <button
              onClick={clearAllFilters}
              style={{
                display: 'inline-block', padding: '7px 18px', background: colors.primary500, color: colors.white,
                borderRadius: '6px', fontSize: '13px', fontWeight: 600, border: 'none', cursor: 'pointer',
              }}
            >
              Clear filters
            </button>
          )}
          {!hasActiveFilter && (stats.by_status['approved'] ?? 0) > 0 && (
            <Link to="/share" style={{
              display: 'inline-block', padding: '7px 18px', background: colors.primary500, color: colors.white,
              borderRadius: '6px', fontSize: '13px', fontWeight: 600, textDecoration: 'none',
            }}>
              Go to Share
            </Link>
          )}
        </div>
      )}

      {/* Active filter indicator */}
      {typeFilter && sessions.length > 0 && (
        <div style={{ fontSize: 12, color: typeColor(typeFilter), fontWeight: 600, marginBottom: 4 }}>
          Showing: {LABELS[typeFilter] ?? typeFilter.replace(/_/g, ' ')}
        </div>
      )}

      {/* Session cards */}
      <div ref={listRef}>
        {sessions.map((s, idx) => {
          const expanded = expandedId === s.session_id;
          const msgs = expandedMessages[s.session_id];
          const flags = riskFlags(s);
          const isToReview = true;
          const isFocused = idx === focusIndex;
          const isSelected = selectedIds.has(s.session_id);

          return (
            <div key={s.session_id} style={{
              background: expanded ? colors.primary50 : isFocused ? '#f0f4ff' : colors.white,
              border: `1px solid ${isFocused ? colors.blue400 : colors.gray200}`,
              borderRadius: '6px',
              marginBottom: '3px',
              overflow: 'hidden',
              transition: 'border-color 0.1s',
            }}>
              {/* Card header */}
              <div style={{
                display: 'flex', alignItems: 'center', gap: '8px',
                padding: '7px 10px', cursor: 'pointer',
              }} onClick={() => handleExpand(s.session_id)}>
                <input
                  type="checkbox"
                  checked={isSelected}
                  aria-label={`Select session: ${s.display_title || 'Untitled'}`}
                  onClick={e => e.stopPropagation()}
                  onChange={() => toggleSelect(s.session_id)}
                  style={{ flexShrink: 0, cursor: 'pointer' }}
                />
                {/* Failure + productivity scores */}
                <div style={{
                  display: 'flex', flexDirection: 'column', gap: 1,
                  fontSize: '12px', whiteSpace: 'nowrap', minWidth: '104px',
                }}>
                  <span
                    title="Failure value (1–5): how useful this trace is for studying agent failure behavior — not how badly the session failed"
                    style={{ color: colors.yellow700, fontWeight: 700 }}
                  >
                    {failureBadge(s.ai_failure_value_score)}
                  </span>
                  {s.ai_quality_score != null && (
                    <span
                      title="Productivity (1–5): how much useful work this session accomplished"
                      style={{ color: colors.gray400, fontSize: 11, fontWeight: 600 }}
                    >
                      Productivity {s.ai_quality_score}/5
                    </span>
                  )}
                </div>

                {/* Title + meta */}
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div style={{
                    display: 'flex', alignItems: 'center', gap: 6,
                    fontSize: '14px', fontWeight: 500, color: colors.gray900,
                    overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                  }}>
                    <span>{expanded ? '\u25BC' : '\u25B6'} {s.display_title || 'Untitled'}</span>
                    {/* Agent-classified type badge */}
                    {s.task_type && !typeFilter && (
                      <span
                        style={{
                          fontSize: 11,
                          padding: '1px 8px',
                          borderRadius: 9999,
                          background: hexAlpha(typeColor(s.task_type), 0.1),
                          color: typeColor(s.task_type),
                          fontWeight: 600,
                          flexShrink: 0,
                          cursor: 'pointer',
                        }}
                        onClick={(e) => { e.stopPropagation(); setTypeFilter(s.task_type); }}
                        title={`Filter by ${LABELS[s.task_type] ?? s.task_type}`}
                      >
                        {LABELS[s.task_type] ?? s.task_type.replace(/_/g, ' ')}
                      </span>
                    )}
                  </div>
                  <div style={{ fontSize: '12px', color: colors.gray400, lineHeight: '1.3', display: 'flex', alignItems: 'center', gap: 4 }}>
                    <span style={{
                      fontSize: 10, fontWeight: 600, padding: '1px 6px', borderRadius: 4,
                      background: hexAlpha(sourceInfo(s).color, 0.10),
                      color: sourceInfo(s).color,
                      flexShrink: 0,
                      whiteSpace: 'nowrap',
                    }}>{sourceInfo(s).label}</span>
                    <span>
                      {s.project} &middot; {s.user_messages + s.assistant_messages} msgs
                      {s.outcome_label ? <> &middot; <span title={outcomeTooltip(s.outcome_label)}>{outcomeText(s.outcome_label)}</span></> : ''}
                      {s.ai_failure_attribution ? ` · ${s.ai_failure_attribution.replace(/_/g, ' ')}` : ''}
                    </span>
                  </div>
                </div>

                {/* Actions */}
                <div style={{ display: 'flex', gap: '3px', alignItems: 'center' }} onClick={e => e.stopPropagation()}>
                  <Link to={`/session/${s.session_id}`} style={{
                    padding: '3px 8px', fontSize: '13px', color: colors.primary500, textDecoration: 'none',
                    fontWeight: 500,
                  }}>Details</Link>
                </div>
              </div>

              {/* Expanded: full redacted conversation */}
              {expanded && (
                <div style={{
                  padding: '0 10px 8px 56px',
                  borderTop: `1px solid ${colors.gray100}`,
                  maxHeight: '350px', overflowY: 'auto',
                }}>
                  {!msgs ? (
                    <Spinner text="Loading redacted content…" />
                  ) : msgs.length === 0 ? (
                    <div style={{ padding: '8px 0', fontSize: '13px', color: colors.gray400 }}>No message content available. Try running <code>clawjournal scan</code> to re-index.</div>
                  ) : msgs.map((m, i) => (
                    <div key={i} style={{ padding: '3px 0', borderBottom: `1px solid ${colors.gray50}` }}>
                      <div style={{
                        fontWeight: 600, fontSize: '11px', textTransform: 'uppercase',
                        color: m.role === 'user' ? colors.blue500 : colors.primary500, marginBottom: '1px',
                      }}>{m.role}</div>
                      <div style={{
                        whiteSpace: 'pre-wrap', wordBreak: 'break-word',
                        fontSize: '13px', lineHeight: '1.4', color: colors.gray700,
                        maxHeight: '100px', overflow: 'hidden',
                      }}>
                        {m.content.slice(0, 400)}{m.content.length > 400 ? '...' : ''}
                      </div>
                      {m.tool_uses && m.tool_uses.length > 0 && (
                        <div style={{ fontSize: '11px', color: colors.gray400, marginTop: '1px' }}>
                          Tools: {m.tool_uses.map(t => t.tool).join(', ')}
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>
          );
        })}
      </div>

      {/* Load more */}
      {hasMore && sessions.length > 0 && (
        <div style={{ textAlign: 'center', marginTop: '8px' }}>
          <button
            onClick={() => { void loadSessions(offset + pageSize, true); }}
            disabled={loading}
            style={{
              padding: '5px 18px', background: colors.gray100, color: colors.gray700, border: `1px solid ${colors.gray300}`,
              borderRadius: '4px', fontSize: '13px', cursor: 'pointer',
            }}
          >
            {loading ? 'Loading...' : 'Load more'}
          </button>
        </div>
      )}

      {loading && sessions.length === 0 && <Spinner text="Loading sessions..." />}

      {/* Confirm dialog for bulk actions */}
      <ConfirmDialog
        open={confirm !== null}
        title={confirm?.action === 'approved' ? 'Approve sessions?' : 'Skip sessions?'}
        message={`This will ${confirm?.action === 'approved' ? 'approve' : 'skip'} ${confirm?.ids.length ?? 0} session${(confirm?.ids.length ?? 0) > 1 ? 's' : ''}.`}
        confirmLabel={confirm?.action === 'approved' ? 'Approve' : 'Skip'}
        variant={confirm?.action === 'blocked' ? 'danger' : 'primary'}
        onConfirm={() => confirm && handleBulkAction(confirm.action as 'approved' | 'blocked')}
        onCancel={() => setConfirm(null)}
      />
    </div>
  );
}

const kbdStyle: React.CSSProperties = {
  display: 'inline-block',
  padding: '0 4px',
  background: colors.gray100,
  border: `1px solid ${colors.gray300}`,
  borderRadius: 3,
  fontSize: 10,
  fontFamily: 'monospace',
  lineHeight: '16px',
};
