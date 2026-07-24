import { useState } from 'react';
import { colors } from '../../theme.ts';
import type { ShareDestination } from './types.ts';
import { formatShareDestination } from './helpers.ts';
import { SHARE_SHELL_WIDTH, btnGhost } from './styles.tsx';
import { Icon } from './shared.tsx';
import { AutoUploadOffer } from '../../components/AutoUploadControls.tsx';

export interface DoneStepProps {
  stepperHeader: React.ReactNode;
  bundle: { traces: number; created: string; approxSize: string } | null;
  receiptId: string | null;
  hostedStatus: string | null;
  supportContact: string | null;
  onDownloadAgain: () => void;
  onNew: () => void;
  globalStyles: React.ReactNode;
  shareDestination: ShareDestination | null;
  destinationLoading: boolean;
  destinationFailed: boolean;
  onRetryDestination: () => void;
}

export function DoneStep(p: DoneStepProps) {
  const [confettiPieces] = useState(() => {
    const palette = ['#D4AB73', '#45392C', '#725E47', '#E9DEC9', '#EDE2CC'];
    const piecesPerWave = 48;
    return Array.from({ length: piecesPerWave * SUCCESS_WAVE_DELAYS.length }, (_, i) => ({
      id: i,
      wave: Math.floor(i / piecesPerWave),
      left: 3 + Math.random() * 94,
      dx: (Math.random() - 0.5) * 180,
      dy: 360 + Math.random() * 300,
      staticY: 28 + Math.random() * 150,
      r: (Math.random() > 0.5 ? 1 : -1) * (540 + Math.random() * 720),
      width: 5 + Math.random() * 5,
      height: 8 + Math.random() * 9,
      color: palette[i % palette.length],
      delay: SUCCESS_WAVE_DELAYS[Math.floor(i / piecesPerWave)] + Math.random() * 520,
      duration: 1800 + Math.random() * 700,
    }));
  });

  const hostedShareUrl = p.shareDestination?.configured ? p.shareDestination.share_page_url : null;
  const hostedShareLabel = hostedShareUrl ? formatShareDestination(hostedShareUrl) : null;
  const submitted = !!p.receiptId;
  // The flow routes here (instead of Submit) when hosted submissions are
  // closed. Surface that explicitly so a participant doesn't think the
  // missing Submit button means their workbench is broken.
  const submissionsClosed = !!p.shareDestination?.configured && !p.shareDestination?.submissions_open;
  // `bundle.approxSize` is the whole zip's compressed size (from the seal
  // response); it doesn't belong on the sessions.jsonl row, which the user
  // would read as "this JSONL is that big". Keep the row generic — the zip
  // size already shows in the stats grid as "File size".
  const zipFiles = [
    { name: 'manifest.json', detail: 'metadata' },
    { name: 'sessions.jsonl', detail: p.bundle ? `${p.bundle.traces} redacted traces` : 'redacted traces' },
    { name: 'trufflehog.json', detail: 'secret scan' },
    { name: 'trufflehog.post-pii.json', detail: 'final scan' },
  ];

  return (
    <div
      data-testid="share-done-shell"
      style={{ position: 'relative', padding: '32px 24px 48px', maxWidth: SHARE_SHELL_WIDTH, margin: '0 auto' }}
    >
      {p.globalStyles}
      {p.stepperHeader}
      {submitted && (
        <>
          {SUCCESS_WAVE_DELAYS.map((delay) => (
            <div
              key={delay}
              aria-hidden="true"
              className="claw-success-flash"
              style={{
                position: 'absolute', top: -36, left: '5%', right: '5%', height: 96,
                zIndex: 1, pointerEvents: 'none', borderRadius: 48,
                background: 'rgba(212, 171, 115, .18)', filter: 'blur(18px)',
                opacity: 0,
                ['--flash-delay' as string]: `${delay}ms`,
              } as React.CSSProperties}
            />
          ))}
          <div
            aria-hidden="true"
            className="claw-success-confetti"
            data-testid="success-confetti"
            style={{ position: 'absolute', inset: 0, zIndex: 2, pointerEvents: 'none', overflow: 'hidden' }}
          >
            {confettiPieces.map((c) => (
              <span
                key={c.id}
                className={c.wave > 0 ? 'claw-confetti-later' : undefined}
                style={{
                  position: 'absolute', top: -20, left: `${c.left}%`,
                  width: c.width, height: c.height, borderRadius: c.id % 4 === 0 ? '50%' : 1,
                  background: c.color, opacity: 0,
                  ['--cdx' as string]: `${c.dx}px`,
                  ['--cdy' as string]: `${c.dy}px`,
                  ['--cstatic-y' as string]: `${c.staticY}px`,
                  ['--cr' as string]: `${c.r}deg`,
                  ['--cduration' as string]: `${c.duration}ms`,
                  ['--cdelay' as string]: `${c.delay}ms`,
                } as React.CSSProperties}
              />
            ))}
          </div>
        </>
      )}
      <div style={{ position: 'relative', padding: '56px 24px 24px', maxWidth: 680, margin: '0 auto', textAlign: 'center' }}>

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
          {submitted ? 'submitted · redacted · receipt saved' : 'local · redacted · not uploaded'}
        </div>

        <h2 style={{ fontSize: 22, fontWeight: 500, letterSpacing: '-0.02em', margin: '0 0 8px', color: colors.gray900 }}>
          {submitted ? 'Submitted' : 'Your bundle is ready'}
        </h2>
        <p style={{ color: colors.gray500, margin: '0 0 24px', fontSize: 14 }}>
          {submitted ? `Receipt ${p.receiptId}` : 'Download the finalized zip, then upload it through the hosted submission page.'}
        </p>

        {!submitted && submissionsClosed && (
          <div style={{
            margin: '0 auto 22px', maxWidth: 520, padding: '12px 14px',
            background: colors.yellow50, border: `1px solid ${colors.yellow200}`,
            borderRadius: 8, fontSize: 13, color: colors.yellow700, textAlign: 'left' as const,
          }}>
            {p.shareDestination?.message
              || 'Hosted research submissions are currently closed, so there is no Submit step right now — this is expected, not a problem. Download the zip below and upload it once submissions reopen.'}
          </div>
        )}

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
              <div style={statValueStyle}>{p.bundle.approxSize}</div>
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
          {!submitted && p.destinationLoading && (
            <span style={{
              display: 'inline-flex', alignItems: 'center', gap: 8,
              padding: '11px 20px', background: colors.white, color: colors.gray400,
              border: `1px solid ${colors.gray200}`, borderRadius: 8,
              fontSize: 14, fontWeight: 600,
            }}>
              <span style={{
                width: 13, height: 13, borderRadius: '50%',
                border: `2px solid ${colors.gray200}`, borderTopColor: colors.gray400,
                animation: 'clawSpin 0.7s linear infinite',
              }} />
              Checking submission options…
            </span>
          )}
          {!submitted && !p.destinationLoading && p.destinationFailed && (
            <button
              onClick={p.onRetryDestination}
              style={{
                display: 'inline-flex', alignItems: 'center', gap: 8,
                padding: '11px 20px', background: colors.white, color: colors.gray900,
                border: `1px solid ${colors.gray300}`, borderRadius: 8,
                fontSize: 14, fontWeight: 600, cursor: 'pointer',
              }}
            >
              <Icon name="retry" size={14} /> Retry submission check
            </button>
          )}
          {!submitted && !p.destinationLoading && !p.destinationFailed && hostedShareUrl && (
            <a
              href={hostedShareUrl}
              target="_blank"
              rel="noopener noreferrer"
              style={{
                display: 'inline-flex', alignItems: 'center', gap: 8,
                padding: '11px 20px', background: colors.primary500, color: colors.white,
                borderRadius: 8, fontSize: 14, fontWeight: 600,
                textDecoration: 'none',
              }}
            >
              Submit to ClawJournal Research &rarr;
            </a>
          )}
        </div>
        <div style={{ fontSize: 12, color: colors.gray500, marginBottom: 8 }}>
          {submitted ? (
            <>
              {p.hostedStatus ? `Status: ${p.hostedStatus}. ` : null}
              {p.supportContact ? `For deletion or withdrawal, contact ${p.supportContact} with the receipt ID.` : null}
            </>
          ) : p.destinationLoading ? (
            <>Checking whether hosted submission is available…</>
          ) : p.destinationFailed ? (
            <>Couldn&rsquo;t reach the submission service. Your redacted zip is saved locally — retry above to check for the Submit option.</>
          ) : hostedShareLabel ? (
            <>
              Upload this zip at <span style={{ fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace' }}>{hostedShareLabel}</span> to contribute to research.
            </>
          ) : (
            <>Hosted submission is not configured for this install. The redacted zip stays on this computer.</>
          )}
        </div>

        <AutoUploadOffer manualReceiptId={p.receiptId} />

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
          </div>
          {zipFiles.map((entry) => (
            <div key={entry.name} style={manifestRowStyle}>
              <span style={manifestNameStyle}>&rsaquo; {entry.name}</span>
              <span style={{ color: colors.gray900 }}>{entry.detail}</span>
            </div>
          ))}
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
const manifestNameStyle: React.CSSProperties = {
  minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis',
};
const doneMiniRow: React.CSSProperties = {
  display: 'flex', gap: 8, alignItems: 'center', marginTop: 6,
  fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace', fontSize: 11.5,
};

const SUCCESS_WAVE_DELAYS = [0, 900, 1800];
