import { useEffect, useMemo, useState } from 'react';
import { colors } from '../../theme.ts';
import type { BlockedShareSession, ReadySession } from './types.ts';
import { SHARE_SHELL_WIDTH, btnPrimary, btnSecondary } from './styles.tsx';
import { Icon } from './shared.tsx';

export interface PackageStepProps {
  stepperHeader: React.ReactNode;
  approvedCount: number;
  approvedList: ReadySession[];
  progress: number;
  log: string;
  failed: string | null;
  blockedSessions: BlockedShareSession[];
  onRetry: () => void;
  onRemoveBlockedAndRetry: () => void;
  onBack: () => void;
  globalStyles: React.ReactNode;
}

export function PackageStep(p: PackageStepProps) {
  const [flying, setFlying] = useState<{ id: string; title: string }[]>([]);
  const [thump, setThump] = useState(0);
  const blockedRows = useMemo(() => p.blockedSessions.map((blocked) => ({
    blocked,
    session: p.approvedList.find((s) => s.session_id === blocked.session_id),
  })), [p.approvedList, p.blockedSessions]);
  const blockedCount = blockedRows.length;

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
          {p.failed && blockedCount > 0 ? 'Packaging blocked' : p.failed ? 'Packaging failed' : 'Packaging your bundle'}
        </h2>
        <p style={{ color: colors.gray500, fontSize: 13.5, margin: '0 0 20px' }}>
          {p.failed && blockedCount > 0
            ? `${blockedCount} trace${blockedCount === 1 ? '' : 's'} triggered the final secret scan.`
            : p.failed
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
          <>
            {blockedCount > 0 && (
              <div style={{
                margin: '20px auto 0', maxWidth: 560, textAlign: 'left',
                border: `1px solid ${colors.yellow200}`, background: colors.yellow50,
                borderRadius: 6, padding: '12px 14px',
              }}>
                <div style={{ fontSize: 12, fontWeight: 600, color: colors.gray900, marginBottom: 8 }}>
                  Blocked trace{blockedCount === 1 ? '' : 's'}
                </div>
                <div style={{ display: 'grid', gap: 8 }}>
                  {blockedRows.map(({ blocked, session }) => {
                    const firstFinding = blocked.findings?.[0];
                    return (
                      <div key={blocked.session_id} style={{
                        display: 'grid', gap: 3, paddingBottom: 8,
                        borderBottom: `1px solid ${colors.yellow200}`,
                      }}>
                        <div style={{ fontSize: 12.5, fontWeight: 500, color: colors.gray900 }}>
                          {session?.display_title || blocked.project || blocked.session_id}
                        </div>
                        <div style={{ fontSize: 11.5, color: colors.gray500 }}>
                          {session?.project || blocked.project || 'Unknown project'}
                          {firstFinding?.detector ? ` · ${firstFinding.detector}` : ''}
                          {firstFinding?.masked ? ` · ${firstFinding.masked}` : ''}
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}
            <div style={{ marginTop: 20, display: 'flex', gap: 10, justifyContent: 'center', flexWrap: 'wrap' }}>
              <button onClick={p.onBack} style={btnSecondary}>Back to review</button>
              {blockedCount > 0 && (
                <button onClick={p.onRemoveBlockedAndRetry} style={btnPrimary}>
                  Remove and retry
                </button>
              )}
              <button onClick={p.onRetry} style={blockedCount > 0 ? btnSecondary : btnPrimary}>
                <Icon name="retry" size={13} /> Retry
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
