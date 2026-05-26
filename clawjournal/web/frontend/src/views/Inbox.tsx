import { useState, useEffect, useCallback, useRef } from 'react';
import { Link } from 'react-router-dom';
import type { Session, Stats } from '../types.ts';
import { api } from '../api.ts';
import { useToast } from '../components/Toast.tsx';
import { ConfirmDialog } from '../components/ConfirmDialog.tsx';
import { Spinner } from '../components/Spinner.tsx';
import { LABELS } from '../components/BadgeChip.tsx';
import { colors, selectStyle } from '../theme.ts';

const PAGE_SIZE = 10;

function scoreBadge(score: number | null): string {
  if (score == null) return '\u2014';
  return '\u2605'.repeat(Math.min(score, 5));
}

function failureBadge(score: number | null): string {
  if (score == null) return '\u2014';
  return `${score} failure`;
}

function outcomeText(badge: string | null): string {
  if (!badge) return '';
  const b = badge.toLowerCase();
  // New resolution labels
  if (b === 'resolved') return '\u2713 resolved';
  if (b === 'partial') return '~ partial';
  if (b === 'failed') return '\u2717 failed';
  if (b === 'abandoned') return '\u2717 abandoned';
  if (b === 'exploratory') return '\u2014 exploratory';
  if (b === 'trivial') return '\u2014 trivial';
  // Legacy outcome labels
  if (b.includes('pass')) return '\u2713 passed';
  if (b.includes('fail')) return '\u2717 failed';
  if (b.includes('analysis')) return '\u2014 analysis';
  if (b.includes('completed')) return '\u2713 completed';
  if (b.includes('errored')) return '\u2717 errored';
  return '';
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
  if (s.source === 'openclaw')
    return { label: 'OpenClaw', color: '#6b7280' };
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
  const [sessions, setSessions] = useState<Session[]>([]);
  const [stats, setStats] = useState<Stats>({ total: 0, by_status: {}, by_source: {}, by_project: {}, by_task_type: {} });
  const [loading, setLoading] = useState(false);
  const [offset, setOffset] = useState(0);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [expandedMessages, setExpandedMessages] = useState<Record<string, Array<{ role: string; content: string; tool_uses?: Array<{ tool: string }> }>>>({});
  const [sort, setSort] = useState('ai_failure_value_score:desc');
  const [showFilters, setShowFilters] = useState(false);
  const [sourceFilter, setSourceFilter] = useState<string | null>(null);
  const [projectFilter, setProjectFilter] = useState<string | null>(null);
  const [recoveryFilter, setRecoveryFilter] = useState<string | null>(null);
  const [attributionFilter, setAttributionFilter] = useState<string | null>(null);
  const [modeFilter, setModeFilter] = useState<string | null>(null);

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

  // Confirm dialog
  const [confirm, setConfirm] = useState<{ action: string; ids: string[] } | null>(null);

  const loadStats = useCallback(async () => {
    try {
      const s = await api.stats();
      setStats(s);
    } catch { /* ignore */ }
  }, []);

  const loadSessions = useCallback(async (currentOffset: number, append: boolean) => {
    setLoading(true);
    const [sortField, sortOrder] = sort.split(':');
    try {
      const data = await api.sessions.list({
        status: null,
        source: sourceFilter,
        project: projectFilter,
        task_type: typeFilter,
        recovery_label: recoveryFilter,
        failure_attribution: attributionFilter,
        failure_mode: modeFilter,
        sort: sortField,
        order: sortOrder,
        limit: PAGE_SIZE,
        offset: currentOffset,
      });
      setSessions(prev => append ? [...prev, ...data] : data);
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Failed to load sessions', 'error');
    }
    finally { setLoading(false); }
  }, [sort, sourceFilter, projectFilter, typeFilter, recoveryFilter, attributionFilter, modeFilter, toast]);

  useEffect(() => {
    setOffset(0);
    setSelectedIds(new Set());
    setFocusIndex(-1);
    loadSessions(0, false);
    loadStats();
  }, [sort, sourceFilter, projectFilter, typeFilter, recoveryFilter, attributionFilter, modeFilter, loadSessions, loadStats]);

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
    try {
      await Promise.all(ids.map(id => api.sessions.update(id, { status: action })));
      setSessions(prev => prev.filter(s => !selectedIds.has(s.session_id)));
      setSelectedIds(new Set());
      loadStats();
      toast(`${ids.length} session${ids.length > 1 ? 's' : ''} ${action === 'approved' ? 'approved' : 'skipped'}`, 'success');
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Bulk action failed', 'error');
    }
    setConfirm(null);
  };

  const handleExpand = async (sessionId: string) => {
    if (expandedIdRef.current === sessionId) {
      setExpandedId(null);
      return;
    }
    if (!expandedMsgsRef.current[sessionId]) {
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
      }
    }
    setExpandedId(sessionId);
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

  return (
    <div style={{ padding: '14px 20px' }}>
      {/* Header */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <h2 style={{ margin: 0, fontSize: '20px', fontWeight: 600, color: colors.gray900 }}>Sessions</h2>
          <span style={{ fontSize: 13, color: colors.gray400 }}>{stats.total} sessions</span>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <select value={sort} onChange={e => setSort(e.target.value)} style={selectStyle}>
            <option value="ai_failure_value_score:desc">Top failures</option>
            <option value="ai_quality_score:desc">Highest productivity</option>
            <option value="start_time:desc">Newest first</option>
            <option value="start_time:asc">Oldest first</option>
          </select>
          <button
            onClick={() => setShowFilters(!showFilters)}
            style={{ background: 'none', border: 'none', color: colors.primary500, fontSize: '13px', cursor: 'pointer', padding: 0 }}
          >
            {showFilters ? 'Hide filters' : 'Filters'}
          </button>
        </div>
      </div>

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
          {taskTypes.map(([type, count]) => {
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
        </div>
      )}

      {/* spacer */}

      {/* spacer */}

      {/* All reviewed state */}
      {sessions.length === 0 && !loading && (
        <div style={{
          background: colors.white, border: `1px solid ${colors.gray200}`, borderRadius: '8px',
          padding: '24px 20px', textAlign: 'center', marginTop: '12px',
        }}>
          <h3 style={{ margin: '0 0 4px', fontSize: '15px', fontWeight: 600, color: colors.green500 }}>
            {typeFilter ? `No "${LABELS[typeFilter] ?? typeFilter}" sessions found` : 'No sessions yet'}
          </h3>
          <p style={{ margin: '0 0 10px', fontSize: '13px', color: colors.gray500 }}>
            {typeFilter
              ? 'Try selecting a different type or clear the filter.'
              : 'Run clawjournal scan to discover sessions.'}
          </p>
          {typeFilter && (
            <button
              onClick={() => setTypeFilter(null)}
              style={{
                display: 'inline-block', padding: '7px 18px', background: colors.primary500, color: colors.white,
                borderRadius: '6px', fontSize: '13px', fontWeight: 600, border: 'none', cursor: 'pointer',
              }}
            >
              Show all sessions
            </button>
          )}
          {!typeFilter && (stats.by_status['approved'] ?? 0) > 0 && (
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
                {/* Failure + productivity scores */}
                <div style={{
                  fontSize: '12px', color: colors.red500, whiteSpace: 'nowrap', minWidth: '74px',
                  fontWeight: 700,
                }}>
                  {failureBadge(s.ai_failure_value_score)}
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
                      {s.outcome_label ? ` · ${outcomeText(s.outcome_label)}` : ''}
                      {s.ai_quality_score != null ? ` · productivity ${s.ai_quality_score}/5` : ''}
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
                    <div style={{ padding: '8px 0', fontSize: '13px', color: colors.gray400 }}>Loading redacted content...</div>
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
      {sessions.length >= PAGE_SIZE && sessions.length > 0 && (
        <div style={{ textAlign: 'center', marginTop: '8px' }}>
          <button
            onClick={() => { const n = offset + PAGE_SIZE; setOffset(n); loadSessions(n, true); }}
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
