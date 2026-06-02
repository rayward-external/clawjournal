import { colors } from '../../theme.ts';
import type { ReadySession, RedactedSessionData } from './types.ts';
import { classify, emptyBuckets, formatTokens, sessionTotalTokens } from './helpers.ts';
import { SHARE_SHELL_WIDTH, btnGhost, btnPrimary } from './styles.tsx';
import { HelpModal, Icon, SourceBadge, UsageDisclosure } from './shared.tsx';

export interface RedactStepProps {
  stepperHeader: React.ReactNode;
  queuedSessions: ReadySession[];
  redactedSessions: Record<string, RedactedSessionData>;
  allDone: boolean;
  aiPiiEnabled: boolean;
  onBack: () => void;
  onContinue: () => void;
  globalStyles: React.ReactNode;
  showHelp: boolean;
  setShowHelp: (b: boolean) => void;
}

export function RedactStep(p: RedactStepProps) {
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
  const categoryRow = (
    label: string,
    count: number,
    max: number,
    color: string = colors.primary500,
    unit: string = 'removed',
  ) => (
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
        {count} {unit}
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

      <UsageDisclosure onLearnMore={() => p.setShowHelp(true)} aiPiiEnabled={p.aiPiiEnabled} />

      <div style={{
        display: 'grid', gridTemplateColumns: '1fr auto', gap: 16,
        alignItems: 'center', marginBottom: 16,
      }}>
        <div>
          <div style={{ fontSize: 16, color: colors.gray900, fontWeight: 500, marginBottom: 3 }}>
            Scrubbing {p.queuedSessions.length} trace{p.queuedSessions.length === 1 ? '' : 's'}
          </div>
          <div style={{ fontSize: 13, color: colors.gray500 }}>
            {p.aiPiiEnabled
              ? 'Deterministic + policy rules run on your device. AI review sends the already-redacted text to your configured AI backend.'
              : 'Deterministic rules \u2192 Policy rules. AI review is off for this bundle.'}
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
        {categoryRow(
          p.aiPiiEnabled ? 'AI-flagged for your review' : 'Needs your manual review',
          totals.flagged,
          Math.max(totals.flagged, 2),
          colors.yellow400,
          p.aiPiiEnabled ? 'flagged' : 'to review',
        )}
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
                : (p.aiPiiEnabled
                  ? 'Redacting on your device; AI review uses your backend'
                  : 'Running on your device')}
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
      {p.showHelp && <HelpModal onClose={() => p.setShowHelp(false)} aiPiiEnabled={p.aiPiiEnabled} />}
    </div>
  );
}
