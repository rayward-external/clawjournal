import { useState } from 'react';
import { Link } from 'react-router-dom';
import type { Session, Share as ShareType } from '../../types.ts';
import { api } from '../../api.ts';
import { colors } from '../../theme.ts';
import { ConfirmDialog } from '../../components/ConfirmDialog.tsx';
import { SessionDrawer } from '../../components/SessionDrawer.tsx';
import { TraceCard } from '../../components/TraceCard.tsx';
import { LARGE_BUNDLE_CONFIRM_THRESHOLD } from './types.ts';
import type { ReadySession, ShareReadyStats } from './types.ts';
import { autoDescription, formatDate, formatTokens, outcomeBadge, outcomeTooltip, sessionTotalTokens, sourceFullLabel } from './helpers.ts';
import { SHARE_SHELL_WIDTH, btnGhost, btnPrimary, btnSecondary } from './styles.tsx';
import { CheckboxRow, HelpModal, Icon, SourceBadge, UsageDisclosure } from './shared.tsx';

const TRACE_RENDER_BATCH = 50;

export interface QueueStepProps {
  stepperHeader: React.ReactNode;
  readyStats: ShareReadyStats | null;
  shares: ShareType[];
  candidates: Session[];
  scoringBackend: { backend: string | null; display_name: string | null } | null;
  queueOrder: string[];
  queuedSessions: ReadySession[];
  note: string;
  setNote: (s: string) => void;
  aiPiiEnabled: boolean;
  setAiPiiEnabled: (enabled: boolean) => void;
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

function UpdatedSinceShareBadge({ session }: { session: ReadySession }) {
  if (!session.updated_since_last_share) return null;

  return (
    <span
      title="This trace has new content since its most recent successful share."
      style={{
        display: 'inline-flex', alignItems: 'center', flexShrink: 0,
        padding: '1px 6px', borderRadius: 999,
        background: colors.primary50, border: `1px solid ${colors.primary200}`,
        color: colors.primary700, fontSize: 10.5, fontWeight: 600,
        lineHeight: '16px', whiteSpace: 'nowrap',
      }}
    >
      Updated since last share
    </span>
  );
}

export function QueueStep(p: QueueStepProps) {
  const [dragId, setDragId] = useState<string | null>(null);
  const [pickerRenderLimit, setPickerRenderLimit] = useState(TRACE_RENDER_BATCH);
  const [queueRenderLimit, setQueueRenderLimit] = useState(TRACE_RENDER_BATCH);
  const [confirmLargeBundle, setConfirmLargeBundle] = useState(false);

  const allSessions = p.readyStats?.sessions || [];
  const selectedIds = new Set(p.queueOrder);
  // `total_approved` counts every approved session; `allSessions` only holds the
  // ones actually eligible to share. When approved > 0 but eligible == 0, the
  // sessions exist but are all held/embargoed/excluded/already-shared — say so
  // instead of telling the user to "approve traces" they already approved.
  const totalApproved = p.readyStats?.total_approved ?? 0;
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

  // The expanded picker is also the fastest way to trim the default queue, so
  // keep selected and unselected traces together under the same filters.
  const sources = [...new Set(allSessions.map(s => s.source).filter(Boolean))].sort();
  const projects = [...new Set(allSessions.map(s => s.project).filter(Boolean))].sort();
  // eslint-disable-next-line react-hooks/purity
  const dateCutoffMs = p.dateFilter ? (Date.now() - ((p.dateFilter === '7d' ? 7 : p.dateFilter === '30d' ? 30 : 90) * 86_400_000)) : null;

  const filteredSessions = allSessions.filter(s => {
    if (p.searchQuery && !(s.display_title || '').toLowerCase().includes(p.searchQuery.toLowerCase())
      && !(s.project || '').toLowerCase().includes(p.searchQuery.toLowerCase())) return false;
    if (p.sourceFilter && s.source !== p.sourceFilter) return false;
    if (p.projectFilter && s.project !== p.projectFilter) return false;
    if (p.scoreFilter > 0 && (s.ai_failure_value_score == null || s.ai_failure_value_score < p.scoreFilter)) return false;
    if (dateCutoffMs && (!s.start_time || new Date(s.start_time).getTime() < dateCutoffMs)) return false;
    return true;
  });
  const available = filteredSessions.filter((s) => !selectedIds.has(s.session_id));
  const visiblePickerSessions = filteredSessions.slice(0, pickerRenderLimit);
  const visibleQueuedSessions = p.queuedSessions.slice(0, queueRenderLimit);

  const historyShares = p.shares.filter(b => b.status === 'shared' || b.status === 'exported');

  const renderAddTracesPicker = () => (
    <div id="share-trace-picker" style={{
      marginTop: 10, padding: 12,
      background: colors.gray50, border: `1px solid ${colors.gray200}`, borderRadius: 8,
    }}>
      <div style={{ display: 'flex', gap: 8, marginBottom: 10, flexWrap: 'wrap' }}>
        <input
          type="text"
          value={p.searchQuery}
          onChange={e => p.setSearchQuery(e.target.value)}
          placeholder="Search by title or project..."
          aria-label="Search traces by title or project"
          style={{
            flex: 1, minWidth: 200, padding: '6px 10px', fontSize: 13,
            border: `1px solid ${colors.gray300}`, borderRadius: 6,
            outline: 'none', background: colors.white,
          }}
        />
        {sources.length > 1 && (
          <select value={p.sourceFilter} onChange={e => p.setSourceFilter(e.target.value)} aria-label="Filter by source"
            style={{ padding: '6px 8px', fontSize: 12, border: `1px solid ${colors.gray300}`, borderRadius: 6, background: colors.white }}>
            <option value="">All sources</option>
            {sources.map(src => <option key={src} value={src}>{sourceFullLabel({ source: src }).label}</option>)}
          </select>
        )}
        {projects.length > 1 && (
          <select value={p.projectFilter} onChange={e => p.setProjectFilter(e.target.value)} aria-label="Filter by project"
            style={{ padding: '6px 8px', fontSize: 12, border: `1px solid ${colors.gray300}`, borderRadius: 6, background: colors.white, maxWidth: 180 }}>
            <option value="">All projects</option>
            {projects.map(pr => <option key={pr} value={pr}>{pr}</option>)}
          </select>
        )}
        <select value={p.scoreFilter} onChange={e => p.setScoreFilter(Number(e.target.value))} aria-label="Filter by failure value"
          style={{ padding: '6px 8px', fontSize: 12, border: `1px solid ${colors.gray300}`, borderRadius: 6, background: colors.white }}>
          <option value={0}>Any failure value</option>
          <option value={3}>3+ failure value</option>
          <option value={4}>4+ failure value</option>
          <option value={5}>5 failure value</option>
        </select>
        <select value={p.dateFilter} onChange={e => p.setDateFilter(e.target.value)} aria-label="Filter by date"
          style={{ padding: '6px 8px', fontSize: 12, border: `1px solid ${colors.gray300}`, borderRadius: 6, background: colors.white }}>
          <option value="">Any date</option>
          <option value="7d">Last 7 days</option>
          <option value="30d">Last 30 days</option>
          <option value="90d">Last 90 days</option>
        </select>
      </div>
      <div style={{ maxHeight: '36vh', overflowY: 'auto', border: `1px solid ${colors.gray200}`, borderRadius: 6, background: colors.white }}>
        {filteredSessions.length === 0 ? (
          <div style={{ padding: 14, textAlign: 'center', color: colors.gray400, fontSize: 13 }}>
            No sessions match your filters.
          </div>
        ) : visiblePickerSessions.map((s, i) => (
          <label key={s.session_id} style={{
            display: 'grid', gridTemplateColumns: 'auto 1fr', gap: 10,
            alignItems: 'center', padding: '8px 12px',
            borderBottom: i < visiblePickerSessions.length - 1 ? `1px solid ${colors.gray100}` : 'none',
            cursor: 'pointer',
          }}>
            <input
              type="checkbox"
              checked={selectedIds.has(s.session_id)}
              aria-label={`Include trace: ${s.display_title || 'Untitled'}`}
              onChange={(e) => e.target.checked ? p.onAdd(s.session_id) : p.onRemove(s.session_id)}
              style={{ width: 15, height: 15, accentColor: colors.gray900 }}
            />
            <div style={{ minWidth: 0 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 7, minWidth: 0 }}>
                <span style={{
                  minWidth: 0, fontSize: 13, color: colors.gray900, fontWeight: 500,
                  whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
                }}>
                  {s.display_title || 'Untitled'}
                </span>
                <UpdatedSinceShareBadge session={s} />
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
          </label>
        ))}
        {filteredSessions.length > visiblePickerSessions.length && (
          <button
            onClick={() => setPickerRenderLimit((current) => current + TRACE_RENDER_BATCH)}
            style={{
              ...btnGhost, width: '100%', justifyContent: 'center', padding: '10px 12px',
              borderTop: `1px solid ${colors.gray200}`, borderRadius: 0,
            }}
          >
            Show {Math.min(TRACE_RENDER_BATCH, filteredSessions.length - visiblePickerSessions.length)} more
            <span style={{ color: colors.gray400, fontWeight: 400 }}>
              ({filteredSessions.length - visiblePickerSessions.length} remaining)
            </span>
          </button>
        )}
      </div>
      {filteredSessions.length > TRACE_RENDER_BATCH && (
        <div style={{ marginTop: 7, fontSize: 11.5, color: colors.gray500 }}>
          Showing {visiblePickerSessions.length} of {filteredSessions.length} matching traces. All eligible traces remain selected unless you uncheck them.
        </div>
      )}
    </div>
  );

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
            {totalApproved > 0 ? (
              <p style={{ margin: '0 auto 20px', maxWidth: '46ch', fontSize: 13 }}>
                You have {totalApproved} approved session{totalApproved === 1 ? '' : 's'}, but none are eligible to
                share right now — they may be on hold, under an active embargo, in an excluded project, or already
                shared. Release a hold or check your excluded projects (<code>clawjournal config --exclude</code>),
                then come back.
              </p>
            ) : (
              <p style={{ margin: '0 auto 20px', maxWidth: '38ch', fontSize: 13 }}>
                Scan traces, release holds, or check excluded projects to make sessions available.
              </p>
            )}
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
            <div>
              <div style={{ marginBottom: 10 }}>
                <h3 style={{ margin: '0 0 4px', fontSize: 16, fontWeight: 600, color: colors.gray900 }}>
                  Build your bundle
                </h3>
                <p style={{ margin: 0, fontSize: 13, color: colors.gray500, maxWidth: '60ch' }}>
                  Nothing is preselected. Pick the traces you want to share below, approved or not. You&rsquo;ll redact and review each one in the next steps.
                </p>
              </div>
              {renderAddTracesPicker()}
            </div>
          )}
        </>
      ) : (
        <>
          <h1 style={{ margin: '0 0 12px', fontSize: 22, fontWeight: 600, color: colors.gray900 }}>
            What would you like to share?
          </h1>
          <UsageDisclosure onLearnMore={p.onHelp} aiPiiEnabled={p.aiPiiEnabled} />

          {/* Bundle summary */}
          <div style={{
            background: colors.gray50,
            border: `1px solid ${colors.gray200}`, borderRadius: 8, marginBottom: 10,
          }}>
            <div style={{
              display: 'flex', alignItems: 'center', justifyContent: 'space-between',
              padding: '12px 14px 8px',
            }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
              <div style={{ fontSize: 13, color: colors.gray900, fontWeight: 500 }}>draft-bundle</div>
              <div style={{ fontSize: 12, color: colors.gray500, fontVariantNumeric: 'tabular-nums' }}>
                {p.queuedSessions.length} trace{p.queuedSessions.length === 1 ? '' : 's'} &middot; ~{formatTokens(totalTokens)} tokens
                {uniqueProjects.length > 0 && ` · ${uniqueProjects.length} project${uniqueProjects.length !== 1 ? 's' : ''}`}
              </div>
            </div>
            {p.showAddTraces ? (
              <span style={{ color: colors.gray500, fontSize: 11.5 }}>
                Use the checkboxes below to deselect traces
              </span>
            ) : (
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
            )}
            </div>
            <div style={{
              padding: '7px 14px 10px',
              fontSize: 11, color: colors.gray400,
              borderTop: `1px solid ${colors.gray200}`,
              display: 'flex', flexDirection: 'column', gap: 3,
            }}>
              <div><span style={{ fontWeight: 600 }}>Failure value N/5</span>{' — how instructive this trace is for AI training (not how badly the session went)'}</div>
              <div><span style={{ fontWeight: 600 }}>Productivity N/5</span>{' — how much useful work the session accomplished'}</div>
              <div><span style={{ fontWeight: 600 }}>Who caused it</span>{': agent caused = the AI made a mistake · environment = external tool/infra problem · preexisting problem = bug existed before · user redirect = user changed direction · unclear = ambiguous'}</div>
              <div><span style={{ fontWeight: 600 }}>Outcome</span>{': ✓ resolved/passed · ✗ failed (test or build said no) · ✗ errored (runtime exception near the end) · ~ partial/interrupted · — inconclusive (no decisive signal)'}</div>
            </div>
          </div>

          {/* Trace list */}
          {!p.showAddTraces && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginBottom: 18 }} onDragEnd={onDragEnd}>
            {visibleQueuedSessions.map((s) => {
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
                    <div style={{ display: 'flex', alignItems: 'center', gap: 7, minWidth: 0 }}>
                      <span style={{
                        minWidth: 0, fontSize: 13.5, color: colors.gray900, fontWeight: 500,
                        whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
                      }}>
                        {s.display_title || 'Untitled'}
                      </span>
                      <UpdatedSinceShareBadge session={s} />
                    </div>
                    {/* Row 1: source · project · tokens · tools */}
                    <div style={{ fontSize: 11.5, color: colors.gray500, display: 'flex', gap: 8, alignItems: 'center', marginTop: 2, flexWrap: 'nowrap', overflow: 'hidden' }}>
                      <SourceBadge s={s} />
                      <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0, flexShrink: 1 }}>{s.project}</span>
                      <span style={{ opacity: 0.5, flexShrink: 0 }}>&middot;</span>
                      <span style={{ flexShrink: 0, whiteSpace: 'nowrap' }}>{formatTokens(sessionTotalTokens(s))} tokens</span>
                      {s.tool_uses > 0 && (<>
                        <span style={{ opacity: 0.5, flexShrink: 0 }}>&middot;</span>
                        <span style={{ flexShrink: 0, whiteSpace: 'nowrap' }}>{s.tool_uses} tools</span>
                      </>)}
                    </div>
                    {/* Row 2: scores · attribution · outcome */}
                    <div style={{ fontSize: 11.5, color: colors.gray500, display: 'flex', gap: 8, alignItems: 'center', marginTop: 2, flexWrap: 'wrap', rowGap: 2 }}>
                      {s.ai_failure_value_score != null ? (
                        <span
                          style={{ color: colors.yellow700, fontWeight: 700, whiteSpace: 'nowrap' }}
                          title="Failure value (1–5): how useful this trace is for studying agent failure behavior — not how badly the session failed"
                        >
                          Failure value {s.ai_failure_value_score}/5
                        </span>
                      ) : (
                        <span
                          style={{ color: colors.gray500, fontStyle: 'italic', whiteSpace: 'nowrap' }}
                          title={s.ai_quality_score == null
                            ? "This session hasn't been scored yet. Click Preview → Score with AI, or run `clawjournal score` from a terminal."
                            : "This legacy-scored session is missing failure value. Re-score it with AI before sharing."}
                        >
                          {s.ai_quality_score == null ? 'unscored' : 'failure value missing'}
                        </span>
                      )}
                      {s.ai_quality_score != null && (<>
                        <span style={{ opacity: 0.5 }}>&middot;</span>
                        <span
                          style={{ whiteSpace: 'nowrap' }}
                          title="Productivity (1–5): how much useful work this session accomplished"
                        >
                          Productivity {s.ai_quality_score}/5
                        </span>
                      </>)}
                      {s.ai_failure_attribution && (<>
                        <span style={{ opacity: 0.5 }}>&middot;</span>
                        <span style={{ whiteSpace: 'nowrap' }}>{s.ai_failure_attribution.replace(/_/g, ' ')}</span>
                      </>)}
                      {s.outcome_badge && (<>
                        <span style={{ opacity: 0.5 }}>&middot;</span>
                        <span style={{ whiteSpace: 'nowrap' }} title={outcomeTooltip(s.outcome_badge)}>{outcomeBadge(s.outcome_badge)}</span>
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
            {p.queuedSessions.length > visibleQueuedSessions.length && (
              <button
                onClick={() => setQueueRenderLimit((current) => current + TRACE_RENDER_BATCH)}
                style={{ ...btnSecondary, alignSelf: 'center', marginTop: 4 }}
              >
                Show {Math.min(TRACE_RENDER_BATCH, p.queuedSessions.length - visibleQueuedSessions.length)} more selected traces
              </button>
            )}
          </div>
          )}

          {/* Add more traces */}
          <div style={{ marginBottom: 18 }}>
            <button
              onClick={() => p.setShowAddTraces(!p.showAddTraces)}
              aria-expanded={p.showAddTraces}
              aria-controls="share-trace-picker"
              style={{
                ...btnGhost, color: colors.gray600, fontSize: 13,
                padding: '6px 10px', border: `1px solid ${colors.gray300}`,
                borderRadius: 6, background: colors.white,
              }}
            >
              <Icon name={p.showAddTraces ? 'chevron' : 'plus'} size={12} />
              {p.showAddTraces ? 'Hide traces' : 'Add more traces'}
              {!p.showAddTraces && available.length > 0 && (
                <span style={{ color: colors.gray400, fontWeight: 400, marginLeft: 4 }}>({available.length} available)</span>
              )}
            </button>

            {p.showAddTraces && renderAddTracesPicker()}
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

          <div style={{
            marginBottom: 18, padding: '10px 12px',
            background: colors.white, border: `1px solid ${colors.gray200}`,
            borderRadius: 8,
          }}>
            <CheckboxRow checked={p.aiPiiEnabled} onChange={p.setAiPiiEnabled}>
              Use AI-assisted PII review for this bundle
              <span style={{ display: 'block', marginTop: 2, color: colors.gray500 }}>
                Sends already-redacted trace text to your configured AI backend to flag contextual identifiers.
              </span>
            </CheckboxRow>
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
                  {p.aiPiiEnabled ? ' with AI review enabled' : ' with AI review off'}
                </div>
              </div>
              <div style={{ marginLeft: 'auto', display: 'flex', gap: 8 }}>
                <button
                  onClick={() => {
                    if (p.queuedSessions.length > LARGE_BUNDLE_CONFIRM_THRESHOLD) {
                      setConfirmLargeBundle(true);
                    } else {
                      p.onContinue();
                    }
                  }}
                  disabled={p.queuedSessions.length === 0}
                  style={{ ...btnPrimary, opacity: p.queuedSessions.length === 0 ? 0.4 : 1, cursor: p.queuedSessions.length === 0 ? 'not-allowed' : 'pointer' }}
                >
                  Redact &amp; review
                  <Icon name="sparkle" size={13} />
                </button>
              </div>
            </div>
          </div>

          <ConfirmDialog
            open={confirmLargeBundle}
            title="Review large bundle?"
            message={`You selected ${p.queuedSessions.length} traces. Redaction and optional AI-assisted PII review will run for every selected trace. Continue only if you intend to review this full bundle.`}
            confirmLabel={`Redact ${p.queuedSessions.length} traces`}
            onConfirm={() => {
              setConfirmLargeBundle(false);
              p.onContinue();
            }}
            onCancel={() => setConfirmLargeBundle(false)}
          />
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
      {p.showHelp && <HelpModal onClose={() => p.setShowHelp(false)} aiPiiEnabled={p.aiPiiEnabled} />}
    </div>
  );
}
