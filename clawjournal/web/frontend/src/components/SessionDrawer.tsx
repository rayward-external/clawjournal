import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../api.ts';
import type { SessionDetail } from '../types.ts';
import { colors } from '../theme.ts';
import { Spinner } from './Spinner.tsx';
import { ToolUseCard } from './ToolUseCard.tsx';

interface SessionDrawerProps {
  sessionId: string | null;
  onClose: () => void;
}

/**
 * Right-side drawer for inspecting a session without leaving the current
 * view. Used from the Share wizard's Preview step so the user can read a
 * trace before deciding whether to include it, then dismiss back to the
 * selection list with all wizard state intact.
 */
export function SessionDrawer({ sessionId, onClose }: SessionDrawerProps) {
  const [data, setData] = useState<SessionDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!sessionId) {
      setData(null);
      setError(null);
      return;
    }
    setLoading(true);
    setError(null);
    setData(null);
    api.sessions.get(sessionId)
      .then(setData)
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false));
  }, [sessionId]);

  useEffect(() => {
    if (!sessionId) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [sessionId, onClose]);

  if (!sessionId) return null;

  return (
    <>
      <div
        onClick={onClose}
        style={{
          position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.35)', zIndex: 1000,
        }}
      />
      <aside
        style={{
          position: 'fixed', top: 0, right: 0, width: '60vw', maxWidth: 820,
          height: '100vh', background: colors.white, zIndex: 1001,
          boxShadow: '-2px 0 24px rgba(0,0,0,0.18)',
          display: 'flex', flexDirection: 'column',
        }}
      >
        <header style={{
          padding: '12px 18px', borderBottom: `1px solid ${colors.gray200}`,
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          gap: 12, flexShrink: 0,
        }}>
          <div style={{ minWidth: 0, flex: 1 }}>
            <div style={{
              fontSize: 14, fontWeight: 600, color: colors.gray900,
              overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
            }}>
              {data?.display_title || (loading ? 'Loading…' : 'Session')}
            </div>
            {data && (
              <div style={{ fontSize: 11, color: colors.gray500, marginTop: 2 }}>
                {data.project} · {data.user_messages + data.assistant_messages} msgs
                {data.tool_uses ? ` · ${data.tool_uses} tools` : ''}
                {(data.checkpoint_count ?? 0) > 1 ? ` · ${data.checkpoint_count} upload checkpoints` : ''}
              </div>
            )}
          </div>
          <Link
            to={`/session/${sessionId}`}
            style={{
              fontSize: 12, color: colors.primary500, textDecoration: 'none',
              padding: '4px 8px', borderRadius: 4,
            }}
            target="_blank"
            rel="noopener noreferrer"
          >
            Open full ↗
          </Link>
          <button
            onClick={onClose}
            aria-label="Close"
            style={{
              background: 'none', border: 'none', fontSize: 20, cursor: 'pointer',
              color: colors.gray500, lineHeight: 1, padding: '4px 8px',
            }}
          >
            ✕
          </button>
        </header>

        <div style={{ flex: 1, overflowY: 'auto', padding: '16px 20px' }}>
          {loading && <div style={{ padding: 40, textAlign: 'center' }}><Spinner /></div>}
          {error && (
            <div style={{
              color: colors.red700, background: colors.red100, padding: 12,
              borderRadius: 6, fontSize: 13,
            }}>
              Failed to load: {error}
            </div>
          )}
          {data?.logical_incomplete && (
            <div
              role="alert"
              style={{
                color: colors.red700, background: colors.red100,
                border: `1px solid ${colors.red200}`, padding: 12,
                borderRadius: 6, fontSize: 13, marginBottom: 12,
              }}
            >
              This conversation has a missing or overlapping upload checkpoint. Run a successful scan before sharing it.
            </div>
          )}
          {data?.messages?.map((msg, i) => (
            <div key={i} style={{
              padding: '10px 0', borderBottom: `1px solid ${colors.gray100}`,
            }}>
              <div style={{
                fontWeight: 600, fontSize: 11, textTransform: 'uppercase',
                color: msg.role === 'user' ? colors.blue500 : colors.primary500,
                marginBottom: 4,
              }}>
                {msg.role} #{i + 1}
              </div>
              {msg.content && (
                <div style={{
                  whiteSpace: 'pre-wrap', wordBreak: 'break-word',
                  fontSize: 13, lineHeight: 1.55, color: colors.gray700,
                }}>
                  {msg.content}
                </div>
              )}
              {msg.thinking && (
                <pre style={{
                  background: colors.yellow50, border: `1px solid ${colors.yellow200}`,
                  borderRadius: 6, padding: 10, fontSize: 12, marginTop: 6,
                  whiteSpace: 'pre-wrap', wordBreak: 'break-word',
                  maxHeight: 240, overflow: 'auto',
                }}>{msg.thinking}</pre>
              )}
              {msg.tool_uses && msg.tool_uses.length > 0 && (
                <div style={{ marginTop: 8 }}>
                  {msg.tool_uses.map((tu, ti) => <ToolUseCard key={ti} tu={tu} />)}
                </div>
              )}
            </div>
          ))}
          {data && (data.messages?.length ?? 0) === 0 && (
            <div style={{ color: colors.gray400, fontSize: 13 }}>
              No messages in this session.
            </div>
          )}
        </div>
      </aside>
    </>
  );
}
