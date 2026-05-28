import { useState } from 'react';
import { Link } from 'react-router-dom';
import type { Session, Share as ShareType } from '../../types.ts';
import { api } from '../../api.ts';
import { colors } from '../../theme.ts';
import { SessionDrawer } from '../../components/SessionDrawer.tsx';
import { TraceCard } from '../../components/TraceCard.tsx';
import type { ReadySession, ShareReadyStats } from './types.ts';
import { autoDescription, formatDate, formatTokens, outcomeBadge, sessionTotalTokens } from './helpers.ts';
import { SHARE_SHELL_WIDTH, btnGhost, btnPrimary, btnSecondary } from './styles.tsx';
import { CheckboxRow, HelpModal, Icon, SourceBadge, UsageDisclosure } from './shared.tsx';

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

export function QueueStep(p: QueueStepProps) {
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
          <UsageDisclosure onLearnMore={p.onHelp} aiPiiEnabled={p.aiPiiEnabled} />

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
      {p.showHelp && <HelpModal onClose={() => p.setShowHelp(false)} aiPiiEnabled={p.aiPiiEnabled} />}
    </div>
  );
}
