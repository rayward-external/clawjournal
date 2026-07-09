import { useEffect, useState, useRef, useCallback } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { api } from '../api.ts';
import type { SessionDetail as SessionDetailType, Message, ToolUse } from '../types.ts';
import { BadgeChip } from '../components/BadgeChip.tsx';
import { Spinner } from '../components/Spinner.tsx';
import { useToast } from '../components/Toast.tsx';
import { RedactedText } from '../components/RedactedText.tsx';
import { ToolUseCard } from '../components/ToolUseCard.tsx';
import { colors, btnGhost } from '../theme.ts';

/* ------------------------------------------------------------------ */
/*  Helpers                                                           */
/* ------------------------------------------------------------------ */

function hexAlpha(hex: string, alpha: number): string {
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return `rgba(${r},${g},${b},${alpha})`;
}

function sourceLabel(s: { source: string; client_origin?: string | null; runtime_channel?: string | null }): { label: string; color: string } {
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
  if (s.source === 'claude-science')
    return { label: 'Claude Science', color: '#9333ea' };
  if (s.source === 'openclaw')
    return { label: 'OpenClaw', color: '#6b7280' };
  if (s.source === 'workbuddy')
    return { label: 'WorkBuddy', color: '#0f766e' };
  return { label: s.source, color: '#6b7280' };
}

function formatDuration(seconds: number | null): string {
  if (seconds == null) return '--';
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return s ? `${m}m ${s}s` : `${m}m`;
}

function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

function formatTime(ts: string | null | undefined): string {
  if (!ts) return '--';
  try {
    return new Date(ts).toLocaleString();
  } catch {
    return ts;
  }
}

function truncate(text: string, max: number): string {
  if (text.length <= max) return text;
  return text.slice(0, max) + '...';
}

function failureValueLabel(score: number): string {
  return score === 5 ? 'Canonical'
    : score === 4 ? 'Strong pattern'
    : score === 3 ? 'Usable signal'
    : score === 2 ? 'Weak signal'
    : 'No signal';
}

function failureEvidenceText(detail: string | null | undefined): string {
  if (!detail) return '';
  try {
    const parsed = JSON.parse(detail);
    if (!Array.isArray(parsed.ai_failure_evidence)) return '';
    return parsed.ai_failure_evidence
      .filter((item: unknown): item is string => typeof item === 'string' && item.trim().length > 0)
      .join('\n');
  } catch {
    return '';
  }
}

function splitEvidence(text: string): string[] {
  return text
    .split('\n')
    .map(item => item.trim())
    .filter(Boolean);
}

/** Render text with [REDACTED] spans highlighted. */
/* ------------------------------------------------------------------ */
/*  Sub-components                                                    */
/* ------------------------------------------------------------------ */

function ThinkingBlock({ text }: { text: string }) {
  const [open, setOpen] = useState(true);
  return (
    <div style={{ margin: '6px 0' }}>
      <button
        onClick={() => setOpen(!open)}
        style={{
          background: 'none',
          border: 'none',
          color: colors.gray500,
          cursor: 'pointer',
          fontSize: 13,
          padding: 0,
          textDecoration: 'underline',
        }}
      >
        {open ? 'Hide thinking' : 'Show thinking'}
      </button>
      {open && (
        <pre
          style={{
            background: colors.yellow50,
            border: `1px solid ${colors.yellow200}`,
            borderRadius: 6,
            padding: 10,
            fontSize: 13,
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
            marginTop: 4,
            maxHeight: 300,
            overflow: 'auto',
          }}
        >
          {text}
        </pre>
      )}
    </div>
  );
}

function MessageCard({
  msg,
  index,
  refCallback,
}: {
  msg: Message;
  index: number;
  refCallback: (el: HTMLDivElement | null) => void;
}) {
  const isUser = msg.role === 'user';
  return (
    <div
      ref={refCallback}
      data-msg-index={index}
      style={{
        borderLeft: `3px solid ${isUser ? '#93c5fd' : colors.gray300}`,
        padding: '10px 14px',
        marginBottom: 12,
        background: colors.white,
        borderRadius: '0 6px 6px 0',
      }}
    >
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          marginBottom: 6,
          fontSize: 13,
          color: colors.gray500,
        }}
      >
        <span style={{ fontWeight: 600, color: isUser ? colors.blue500 : colors.gray700 }}>
          {isUser ? 'User' : 'Assistant'}
        </span>
        <span>#{index}</span>
        {msg.timestamp && <span>{formatTime(msg.timestamp)}</span>}
      </div>

      {msg.thinking && <ThinkingBlock text={msg.thinking} />}

      <div
        style={{
          fontSize: 14,
          lineHeight: 1.6,
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-word',
        }}
      >
        <RedactedText text={msg.content} />
      </div>

      {msg.tool_uses && msg.tool_uses.length > 0 && (
        <div style={{ marginTop: 8 }}>
          {msg.tool_uses.map((tu, i) => (
            <ToolUseCard key={i} tu={tu} />
          ))}
        </div>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Review statuses                                                   */
/* ------------------------------------------------------------------ */

/* ------------------------------------------------------------------ */
/*  Main component                                                    */
/* ------------------------------------------------------------------ */

export default function SessionDetail() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const { toast } = useToast();

  const [session, setSession] = useState<SessionDetailType | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [failureValueOverride, setFailureValueOverride] = useState<number | null>(null);
  const [failureValueEvidence, setFailureValueEvidence] = useState('');
  const [scoring, setScoring] = useState(false);

  // Refs for scroll targets
  const msgRefs = useRef<Map<number, HTMLDivElement>>(new Map());

  const setMsgRef = useCallback(
    (index: number) => (el: HTMLDivElement | null) => {
      if (el) {
        msgRefs.current.set(index, el);
      } else {
        msgRefs.current.delete(index);
      }
    },
    [],
  );

  useEffect(() => {
    if (!id) return;
    setLoading(true);
    api.sessions
      .get(id)
      .then((data) => {
        setSession(data);
        setFailureValueOverride(data.ai_failure_value_score);
        setFailureValueEvidence(failureEvidenceText(data.ai_scoring_detail));
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, [id]);

  const scrollToMessage = (index: number) => {
    const el = msgRefs.current.get(index);
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'start' });
      el.style.background = colors.yellow100;
      setTimeout(() => {
        el.style.background = colors.white;
      }, 800);
    }
  };

  /* Loading / error states */
  if (loading) {
    return <Spinner text="Loading session..." />;
  }
  if (error) {
    return (
      <div style={{ padding: 40, textAlign: 'center' }}>
        <div style={{ color: colors.red700, marginBottom: 12 }}>Error: {error}</div>
        <button onClick={() => navigate('/')} style={btnGhost}>
          Back
        </button>
      </div>
    );
  }
  if (!session) {
    return (
      <div style={{ padding: 40, textAlign: 'center', color: colors.gray500 }}>Session not found.</div>
    );
  }

  const totalToolUses = session.messages.reduce(
    (sum, m) => sum + (m.tool_uses?.length ?? 0),
    0,
  );
  const userMsgCount = session.messages.filter((m) => m.role === 'user').length;
  const firstUserMsg = session.messages.find((m) => m.role === 'user');
  const firstUserPreview = firstUserMsg
    ? truncate((firstUserMsg.content ?? '').trim(), 200)
    : null;

  /* ---------------------------------------------------------------- */
  /*  Render                                                          */
  /* ---------------------------------------------------------------- */

  return (
    <div style={{ display: 'flex', height: '100vh', fontFamily: 'system-ui, sans-serif' }}>
      {/* ---- Left pane: Summary + Metadata ---- */}
      <div
        style={{
          width: 300,
          minWidth: 300,
          borderRight: `1px solid ${colors.gray200}`,
          overflowY: 'auto',
          background: '#fafafa',
          padding: '10px 0',
          fontSize: 13,
        }}
      >
        <div style={{ padding: '0 10px 8px', borderBottom: `1px solid ${colors.gray200}` }}>
          <button onClick={() => navigate('/')} style={btnGhost}>
            &larr; Back
          </button>
        </div>

        <div style={{ padding: '8px 10px 4px', fontWeight: 700, fontSize: 12, color: colors.gray400 }}>
          SUMMARY
        </div>
        <div style={{ padding: '4px 10px' }}>
          <SummaryRow label="Messages" value={String(session.messages.length)} />
          <SummaryRow label="User msgs" value={String(userMsgCount)} />
          <SummaryRow label="Tool uses" value={String(totalToolUses)} />
          <SummaryRow label="Tokens" value={formatTokens(session.input_tokens + session.output_tokens)} />
          <SummaryRow label="Duration" value={formatDuration(session.duration_seconds)} />
          {(session.user_interrupts ?? 0) > 0 && (
            <SummaryRow
              label="Interrupts"
              value={String(session.user_interrupts)}
              color={(session.user_interrupts ?? 0) >= 4 ? colors.red400 : (session.user_interrupts ?? 0) >= 2 ? colors.yellow400 : colors.gray500}
            />
          )}
        </div>

        {/* Session Info */}
        <div style={{ padding: '0 10px' }}>
          <div style={{ padding: '12px 0 4px', fontWeight: 700, fontSize: 12, color: colors.gray400 }}>
            SESSION INFO
          </div>
          <MetaRow label="ID" value={session.session_id} mono />
          <MetaRow label="Source" value={
            <span style={{
              fontSize: 11, fontWeight: 600, padding: '1px 6px', borderRadius: 4,
              background: hexAlpha(sourceLabel(session).color, 0.10),
              color: sourceLabel(session).color,
            }}>{sourceLabel(session).label}</span>
          } />
          <MetaRow
            label="Model"
            value={
              session.model
                ? session.model_effort
                  ? `${session.model} @ ${session.model_effort}`
                  : session.model
                : '--'
            }
          />
          <MetaRow label="Branch" value={session.git_branch ?? '--'} />
          <MetaRow label="Task type" value={session.task_type ?? '--'} />
          <MetaRow label="Started" value={formatTime(session.start_time)} />
          <MetaRow
            label="Tokens"
            value={`${formatTokens(session.input_tokens)} in / ${formatTokens(session.output_tokens)} out`}
          />
          <MetaRow
            label="Messages"
            value={`${session.user_messages} user / ${session.assistant_messages} asst`}
          />
          <MetaRow label="Tool uses" value={String(session.tool_uses)} />
          {session.estimated_cost_usd != null && session.estimated_cost_usd > 0 && (
            <MetaRow label="API-equivalent cost" value={`$${session.estimated_cost_usd.toFixed(2)}`} />
          )}
          {session.parent_session_id && (
            <MetaRow label="Parent" value={session.parent_session_id.slice(0, 12) + '...'} mono />
          )}
          {session.share_id && <MetaRow label="Share" value={session.share_id} mono />}

          {/* Failure reasons (AI judge output) */}
          {(() => {
            let scoringDetail: Record<string, unknown> = {};
            try {
              if (session.ai_scoring_detail) {
                scoringDetail = JSON.parse(session.ai_scoring_detail);
              }
            } catch { /* tolerate malformed scoring detail */ }
            const evidence = Array.isArray(scoringDetail.ai_failure_evidence)
              ? scoringDetail.ai_failure_evidence.filter((item): item is string => typeof item === 'string')
              : [];
            const metaLabels = Array.isArray(scoringDetail.ai_meta_labels)
              ? scoringDetail.ai_meta_labels.filter((item): item is string => typeof item === 'string')
              : [];
            const modes = Array.isArray(session.ai_failure_modes) ? session.ai_failure_modes : [];
            const recovery = Array.isArray(session.ai_recovery_labels) ? session.ai_recovery_labels : [];
            const hasFailureBlock =
              session.ai_failure_value_score != null
              || modes.length > 0
              || metaLabels.length > 0
              || evidence.length > 0
              || (session.ai_failure_attribution && session.ai_failure_attribution.length > 0)
              || (session.ai_learning_summary && session.ai_learning_summary.length > 0);
            if (!hasFailureBlock) return null;
            return (
              <>
                <div style={{ padding: '12px 0 4px', fontWeight: 700, fontSize: 12, color: colors.gray400 }}>
                  FAILURE REASONS
                </div>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, padding: '2px 0 6px' }}>
                  {session.ai_failure_value_score != null && (
                    <span
                      title="Failure value (1-5): how useful this trace is for understanding agent failure behavior"
                      style={{
                        display: 'inline-block', padding: '1px 8px', borderRadius: '9999px',
                        fontSize: '12px', fontWeight: 600, lineHeight: '20px',
                        background: '#fef2f2', color: '#b91c1c', whiteSpace: 'nowrap',
                      }}
                    >
                      Failure value {session.ai_failure_value_score}/5
                    </span>
                  )}
                  {session.ai_failure_attribution && (
                    <BadgeChip kind="attribution" value={session.ai_failure_attribution} />
                  )}
                  {modes.map(m => <BadgeChip key={`mode-${m}`} kind="failure_mode" value={m} />)}
                  {metaLabels.map(m => <BadgeChip key={`meta-${m}`} kind="meta_label" value={m} />)}
                  {recovery.map(r => <BadgeChip key={`rec-${r}`} kind="recovery" value={r} />)}
                </div>
                {session.ai_learning_summary && (
                  <div
                    style={{
                      padding: '4px 0 6px', fontSize: 12, fontStyle: 'italic',
                      color: colors.gray700, lineHeight: 1.4,
                    }}
                  >
                    “{session.ai_learning_summary}”
                  </div>
                )}
                {evidence.length > 0 && (
                  <>
                    <div style={{ padding: '8px 0 2px', fontSize: 11, fontWeight: 600, color: colors.gray400 }}>
                      EVIDENCE
                    </div>
                    <ul style={{ margin: 0, padding: '0 0 0 14px', lineHeight: 1.5 }}>
                      {evidence.map((e, i) => (
                        <li key={i} style={{ fontSize: 12, color: colors.gray700, marginBottom: 2 }}>
                          {e}
                        </li>
                      ))}
                    </ul>
                  </>
                )}
              </>
            );
          })()}

          {/* Badges */}
          <div style={{ padding: '12px 0 4px', fontWeight: 700, fontSize: 12, color: colors.gray400 }}>
            BADGES
          </div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, padding: '2px 0 4px' }}>
            <BadgeChip kind="status" value={session.review_status} />
            {session.outcome_label && (
              <BadgeChip kind="outcome" value={session.outcome_label} />
            )}
            {session.value_labels?.map((b) => (
              <BadgeChip key={b} kind="value" value={b} />
            ))}
            {session.risk_level?.map((b) => (
              <BadgeChip key={b} kind="risk" value={b} />
            ))}
          </div>

          {/* Files touched */}
          {session.files_touched?.length > 0 && (
            <>
              <div style={{ padding: '12px 0 4px', fontWeight: 700, fontSize: 12, color: colors.gray400 }}>
                FILES TOUCHED ({session.files_touched.length})
              </div>
              <ul style={{ margin: 0, padding: '0 0 0 14px', lineHeight: 1.8 }}>
                {session.files_touched.map((f, i) => (
                  <li key={i} style={{ fontFamily: 'monospace', fontSize: 12, wordBreak: 'break-all' }}>
                    {f}
                  </li>
                ))}
              </ul>
            </>
          )}

          {/* Commands run */}
          {session.commands_run?.length > 0 && (
            <>
              <div style={{ padding: '12px 0 4px', fontWeight: 700, fontSize: 12, color: colors.gray400 }}>
                COMMANDS RUN ({session.commands_run.length})
              </div>
              <ul style={{ margin: 0, padding: '0 0 0 14px', lineHeight: 1.8 }}>
                {session.commands_run.map((c, i) => (
                  <li key={i} style={{ fontFamily: 'monospace', fontSize: 12, wordBreak: 'break-all' }}>
                    {c}
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>

        {firstUserPreview && (
          <>
            <div style={{ padding: '12px 10px 4px', fontWeight: 700, fontSize: 12, color: colors.gray400 }}>
              PROMPT
            </div>
            <div style={{ padding: '4px 10px', fontSize: 12, color: colors.gray700, lineHeight: 1.5 }}>
              {firstUserPreview}
            </div>
          </>
        )}

        <div style={{ padding: '12px 10px 4px', display: 'flex', gap: 6 }}>
          <button
            onClick={() => scrollToMessage(0)}
            style={{ ...btnGhost, fontSize: 12 }}
          >
            Jump to top
          </button>
          <button
            onClick={() => scrollToMessage(session.messages.length - 1)}
            style={{ ...btnGhost, fontSize: 12 }}
          >
            Jump to bottom
          </button>
        </div>
      </div>

      {/* ---- Center pane: Transcript ---- */}
      <div
        style={{
          flex: 1,
          overflowY: 'auto',
          padding: '16px 20px',
          background: colors.gray100,
        }}
      >
        <h2 style={{ margin: '0 0 4px', fontSize: 20 }}>{session.display_title}</h2>
        <div style={{ fontSize: 13, color: colors.gray500, marginBottom: 16 }}>
          {session.project} &middot; {session.source}
          {session.model && (
            <> &middot; {session.model}
              {session.model_effort && <> @ {session.model_effort}</>}
            </>
          )}
        </div>

        {session.messages.length === 0 && (
          <div style={{ color: colors.gray400, fontStyle: 'italic' }}>No messages in this session.</div>
        )}

        {session.messages.map((msg, i) => (
          <MessageCard key={i} msg={msg} index={i} refCallback={setMsgRef(i)} />
        ))}
      </div>

      {/* ---- Right pane: Review + Score ---- */}
      <div
        style={{
          width: 260,
          minWidth: 260,
          borderLeft: `1px solid ${colors.gray200}`,
          overflowY: 'auto',
          padding: 14,
          fontSize: 13,
          background: colors.white,
        }}
      >
        <Section title="Failure Value">
          {(failureValueOverride ?? session.ai_failure_value_score) != null ? (() => {
            const displayScore = failureValueOverride ?? session.ai_failure_value_score!;
            return (
            <div>
              <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginBottom: 6 }}>
                <span style={{
                  fontSize: 24,
                  fontWeight: 700,
                  color: colors.red500,
                }}>
                  Failure value {displayScore}/5
                </span>
                <span style={{ fontSize: 13, fontWeight: 600, color: colors.gray700 }}>
                  {failureValueLabel(displayScore)}
                </span>
              </div>
              {session.ai_learning_summary && (
                <div style={{ fontSize: 12, color: colors.gray700, lineHeight: 1.5, marginBottom: 6 }}>
                  {session.ai_learning_summary}
                </div>
              )}
              {session.ai_summary && !session.ai_learning_summary && (
                <div style={{ fontSize: 12, color: colors.gray700, lineHeight: 1.5, marginBottom: 6 }}>
                  {session.ai_summary}
                </div>
              )}
              {session.ai_score_reason && (
                <div style={{ fontSize: 12, color: colors.gray500, fontStyle: 'italic', lineHeight: 1.5, marginBottom: 8 }}>
                  {session.ai_score_reason}
                </div>
              )}
              {session.ai_quality_score != null && (
                <div style={{ fontSize: 11, color: colors.gray400, lineHeight: 1.4, marginBottom: 8 }}
                  title="Earlier productivity score">
                  Productivity {session.ai_quality_score}/5
                </div>
              )}
            </div>
            );
          })() : (
            <div style={{ fontSize: 12, color: colors.gray400, marginBottom: 8 }}>Failure value not scored yet</div>
          )}

          <button
            disabled={scoring}
            onClick={async () => {
              if (!id) return;
              setScoring(true);
              try {
                await api.sessions.score(id);
                const fresh = await api.sessions.get(id);
                setSession(fresh);
                setFailureValueOverride(fresh.ai_failure_value_score);
                setFailureValueEvidence(failureEvidenceText(fresh.ai_scoring_detail));
                toast('Scored', 'success');
              } catch (e) {
                toast(e instanceof Error ? e.message : 'Scoring failed', 'error');
              } finally {
                setScoring(false);
              }
            }}
            style={{
              width: '100%',
              padding: '6px 10px',
              marginBottom: 10,
              fontSize: 12,
              fontWeight: 600,
              color: colors.gray50,
              background: scoring ? colors.gray400 : colors.gray700,
              border: 'none',
              borderRadius: 4,
              cursor: scoring ? 'wait' : 'pointer',
            }}
          >
            {scoring ? 'Scoring…' : (session.ai_failure_value_score ? 'Re-score with AI' : 'Score with AI')}
          </button>

          <div style={{ fontSize: 11, color: colors.gray400, lineHeight: 1.45, marginBottom: 10 }}>
            Sends this trace — anonymized (home-dir paths &amp; usernames stripped) and redacted —
            to your configured AI backend (Claude Code / Codex) for a 1–5 score. Runs locally as a
            subprocess; the agent itself may call its provider and incur usage cost.
          </div>

          <div style={{ fontSize: 12, fontWeight: 600, color: colors.gray700, marginBottom: 4 }}>Override failure value</div>
          <textarea
            value={failureValueEvidence}
            onChange={e => setFailureValueEvidence(e.target.value)}
            placeholder="Evidence for 4-5 overrides"
            rows={3}
            style={{
              width: '100%',
              boxSizing: 'border-box',
              resize: 'vertical',
              border: `1px solid ${colors.gray200}`,
              borderRadius: 4,
              padding: '6px 8px',
              marginBottom: 6,
              fontSize: 12,
              lineHeight: 1.4,
              color: colors.gray700,
            }}
          />
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: 4, marginBottom: 4 }}>
            {[1, 2, 3, 4, 5].map((n) => {
              const currentScore = failureValueOverride ?? session.ai_failure_value_score ?? 0;
              const active = n === currentScore;
              return (
                <button
                  key={n}
                  onClick={async () => {
                    const evidence = splitEvidence(failureValueEvidence);
                    if (n >= 4 && evidence.length === 0) {
                      toast('Evidence is required for failure value 4-5', 'error');
                      return;
                    }
                    if (id) {
                      try {
                        await api.sessions.update(id, {
                          ai_failure_value_score: n,
                          ...(evidence.length > 0 ? { ai_failure_evidence: evidence } : {}),
                        });
                        const fresh = await api.sessions.get(id);
                        setSession(fresh);
                        setFailureValueOverride(n);
                        setFailureValueEvidence(failureEvidenceText(fresh.ai_scoring_detail));
                        toast(`Failure value set to ${n}`, 'success');
                      } catch (e) {
                        toast(e instanceof Error ? e.message : 'Failed to save failure value', 'error');
                      }
                    }
                  }}
                  style={{
                    padding: '5px 0',
                    border: `1px solid ${active ? colors.red500 : colors.gray200}`,
                    background: active ? colors.red500 : colors.white,
                    color: active ? colors.white : colors.gray700,
                    borderRadius: 4,
                    fontSize: 12,
                    fontWeight: 700,
                    cursor: 'pointer',
                    lineHeight: 1,
                  }}
                >
                  {n}
                </button>
              );
            })}
          </div>
        </Section>

      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Tiny layout helpers                                               */
/* ------------------------------------------------------------------ */

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 16 }}>
      <div
        style={{
          fontWeight: 700,
          fontSize: 11,
          color: colors.gray400,
          textTransform: 'uppercase',
          marginBottom: 6,
          letterSpacing: '0.04em',
        }}
      >
        {title}
      </div>
      {children}
    </div>
  );
}

function MetaRow({ label, value, mono }: { label: string; value: React.ReactNode; mono?: boolean }) {
  return (
    <div
      style={{
        display: 'flex',
        justifyContent: 'space-between',
        padding: '2px 0',
        gap: 8,
      }}
    >
      <span style={{ color: colors.gray500, flexShrink: 0 }}>{label}</span>
      <span
        style={{
          fontWeight: 500,
          textAlign: 'right',
          wordBreak: 'break-all',
          ...(mono ? { fontFamily: 'monospace', fontSize: 12 } : {}),
        }}
      >
        {value}
      </span>
    </div>
  );
}

function SummaryRow({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', padding: '3px 0' }}>
      <span style={{ color: colors.gray500 }}>{label}</span>
      <span style={{ fontWeight: 600, color: color ?? colors.gray700 }}>{value}</span>
    </div>
  );
}
