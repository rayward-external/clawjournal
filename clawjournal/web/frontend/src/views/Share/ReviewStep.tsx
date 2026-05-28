import { RedactedText } from '../../components/RedactedText.tsx';
import { ToolUseCard } from '../../components/ToolUseCard.tsx';
import { colors } from '../../theme.ts';
import type { ReadySession, RedactedSessionData } from './types.ts';
import { aggregateCategories, classify, formatTokens, hexAlpha, sessionTotalTokens } from './helpers.ts';
import { SHARE_SHELL_WIDTH, btnGhost, btnPrimary, btnSecondary } from './styles.tsx';
import { HelpModal, Icon, SourceBadge, StatusDot, ThinkingBlock, UsageDisclosure } from './shared.tsx';

export interface ReviewStepProps {
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
  globalStyles: React.ReactNode;
  showHelp: boolean;
  setShowHelp: (b: boolean) => void;
}

export function ReviewStep(p: ReviewStepProps) {
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
                          {c.source === 'ai' ? 'AI' : '✓'}
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
