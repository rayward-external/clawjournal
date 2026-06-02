import { Link } from 'react-router-dom';
import type { Stats } from '../types.ts';
import { colors } from '../theme.ts';
import { LOCAL_FIRST_CAVEAT } from './onboardingCopy.ts';

interface GettingStartedGuideProps {
  stats: Stats;
  onDismiss: () => void;
}

export function GettingStartedGuide({ stats, onDismiss }: GettingStartedGuideProps) {
  const toReview = (stats.by_status['new'] ?? 0) + (stats.by_status['shortlisted'] ?? 0);
  // Labels for the in-Share phases come from the canonical stepper so they can
  // never drift from the real wizard (Share/types.ts STEPS).
  const steps = [
    { label: 'Review', detail: toReview > 0 ? `${toReview} waiting` : 'Scan sessions' },
    { label: 'Queue', detail: 'In Share' },
    { label: 'Redact', detail: 'Local review' },
    { label: 'Package', detail: 'Submit or zip' },
  ];

  return (
    <div role="note" aria-label="Getting started" style={{
      display: 'flex',
      flexWrap: 'wrap',
      gap: 14,
      alignItems: 'center',
      margin: '2px 0 12px',
      padding: '12px 14px',
      border: `1px solid ${colors.primary200}`,
      borderRadius: 8,
      background: colors.primary50,
    }}>
      <div style={{ minWidth: 220, flex: '1 1 240px' }}>
        <div style={{ fontSize: 13.5, fontWeight: 700, color: colors.gray900, marginBottom: 2 }}>
          New here? Turn your sessions into shareable traces.
        </div>
        <div style={{ fontSize: 12, color: colors.gray600, lineHeight: 1.45 }}>
          Inspect captured sessions here, then open Share to pick traces, redact, and review locally. {LOCAL_FIRST_CAVEAT}
        </div>
      </div>

      <div role="list" style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fit, minmax(132px, 1fr))',
        gap: 6,
        flex: '999 1 440px',
        minWidth: 280,
      }}>
        {steps.map((step, idx) => (
          <div
            key={step.label}
            role="listitem"
            style={{
              display: 'flex',
              gap: 7,
              alignItems: 'center',
              minWidth: 0,
              padding: '7px 8px',
              border: `1px solid ${colors.gray200}`,
              borderRadius: 6,
              background: colors.white,
            }}
          >
            <span aria-hidden="true" style={{
              width: 20,
              height: 20,
              borderRadius: '50%',
              display: 'inline-grid',
              placeItems: 'center',
              flexShrink: 0,
              background: colors.gray800,
              color: colors.white,
              fontSize: 11,
              fontWeight: 700,
              fontVariantNumeric: 'tabular-nums',
            }}>
              {idx + 1}
            </span>
            <span style={{ minWidth: 0 }}>
              <span style={{ display: 'block', fontSize: 12, fontWeight: 600, color: colors.gray800, whiteSpace: 'nowrap' }}>
                {step.label}
              </span>
              <span style={{ display: 'block', fontSize: 11, color: colors.gray500, lineHeight: 1.25 }}>
                {step.detail}
              </span>
            </span>
          </div>
        ))}
      </div>

      <div style={{ display: 'flex', gap: 8, alignItems: 'center', justifyContent: 'flex-end', marginLeft: 'auto' }}>
        <Link to="/share" onClick={onDismiss} style={{
          display: 'inline-flex',
          alignItems: 'center',
          padding: '7px 12px',
          background: colors.gray900,
          color: colors.white,
          borderRadius: 6,
          fontSize: 12.5,
          fontWeight: 600,
          textDecoration: 'none',
          whiteSpace: 'nowrap',
        }}>
          Open Share
        </Link>
        <button
          type="button"
          onClick={onDismiss}
          aria-label="Dismiss getting-started guide"
          title="Dismiss getting-started guide"
          style={{
            width: 28,
            height: 28,
            display: 'grid',
            placeItems: 'center',
            border: `1px solid ${colors.primary200}`,
            borderRadius: 6,
            background: 'transparent',
            color: colors.gray500,
            cursor: 'pointer',
            fontSize: 15,
            lineHeight: 1,
          }}
        >
          <span aria-hidden="true">✕</span>
        </button>
      </div>
    </div>
  );
}
