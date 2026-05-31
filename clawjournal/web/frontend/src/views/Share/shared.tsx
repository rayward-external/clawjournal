import { useState } from 'react';
import { Link } from 'react-router-dom';
import { colors } from '../../theme.ts';
import { hexAlpha, sourceFullLabel } from './helpers.ts';

export function SourceBadge({ s }: { s: { source: string; client_origin?: string | null; runtime_channel?: string | null } }) {
  const { label, color } = sourceFullLabel(s);
  return <span style={{ fontSize: 10, fontWeight: 600, padding: '1px 6px', borderRadius: 4, background: hexAlpha(color, 0.10), color, marginRight: 3 }}>{label}</span>;
}

export function ThinkingBlock({ text }: { text: string }) {
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

export function Icon({ name, size = 16 }: { name: string; size?: number }) {
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

export function UsageDisclosure({ onLearnMore, aiPiiEnabled = false }: { onLearnMore?: () => void; aiPiiEnabled?: boolean }) {
  return (
    <div style={{
      display: 'flex', alignItems: 'stretch', gap: 8,
      padding: 10, marginBottom: 18,
      background: colors.primary50, border: `1px solid ${colors.primary200}`,
      borderRadius: 10, flexWrap: 'wrap',
    }}>
      <TrustChip icon="shield" title="Local only" subtitle="Redaction runs on your device" />
      <TrustChip
        icon="sparkle"
        title={aiPiiEnabled ? 'Rules + AI redact' : 'Rules-only redact'}
        subtitle={aiPiiEnabled ? 'Deterministic rules, then AI' : 'AI review is off for this bundle'}
      />
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

export function HelpModal({ onClose, aiPiiEnabled = false }: { onClose: () => void; aiPiiEnabled?: boolean }) {
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
      sub: aiPiiEnabled ? 'Opted in' : 'Off unless you opt in',
      desc: aiPiiEnabled
        ? 'Names, orgs, private project names, and contextual identifiers are flagged. If AI is unavailable, the trace falls back to rules-only and you’ll see a labeled reason.'
        : 'The bundle uses deterministic and policy rules only. You can opt in before redaction if you want AI to flag contextual identifiers.',
      accent: colors.primary500,
      accentBg: colors.primary100,
    },
    {
      num: 4,
      name: 'Your review',
      sub: aiPiiEnabled ? 'Only when flagged' : 'Required when AI is off',
      desc: aiPiiEnabled
        ? 'Only traces the AI wasn’t confident about reach you. Everything else clears automatically.'
        : 'Review each rules-only preview before packaging so the final zip contains only traces you inspected.',
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

export function StatusDot({ status }: { status: 'checking' | 'clear' | 'review' }) {
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

export function CheckboxRow({ checked, onChange, children }: { checked: boolean; onChange: (checked: boolean) => void; children: React.ReactNode }) {
  return (
    <label style={{ display: 'flex', alignItems: 'flex-start', gap: 9, fontSize: 12.5, color: colors.gray700, lineHeight: 1.4, cursor: 'pointer' }}>
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        style={{ marginTop: 2, width: 15, height: 15, accentColor: colors.gray900, flexShrink: 0 }}
      />
      <span>{children}</span>
    </label>
  );
}
