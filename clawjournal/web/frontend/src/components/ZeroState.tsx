import { colors } from '../theme.ts';
import { STEPS } from '../views/Share/types.ts';

/**
 * First-run walkthrough shown in the Sessions view when no sessions have been
 * scanned yet (stats.total === 0). It orients the user, restates the local-first
 * promise, gives the exact `clawjournal scan` command, and previews the Share
 * roadmap derived from the canonical STEPS so labels never drift. It carries no
 * dismiss control — it disappears on its own once sessions exist.
 */
export function ZeroState() {
  const roadmap = STEPS.filter(s => s.key !== 'done').map(s => s.label).join(' → ');
  return (
    <div role="note" aria-label="Getting started" style={{
      margin: '8px 0 12px',
      padding: '20px 22px',
      border: `1px solid ${colors.primary200}`,
      borderRadius: 10,
      background: colors.primary50,
    }}>
      <h3 style={{ margin: '0 0 6px', fontSize: 17, fontWeight: 700, color: colors.gray900 }}>
        Welcome to ClawJournal
      </h3>
      <p style={{ margin: '0 0 14px', fontSize: 13.5, color: colors.gray600, lineHeight: 1.5, maxWidth: 620 }}>
        ClawJournal scans your coding-agent session logs (Claude Code, Codex, and more),
        scores them, and helps you redact and share traces. Everything stays on your machine by
        default — AI scoring/review (when enabled) sends an anonymized, redacted trace to your
        configured AI backend; nothing is uploaded for sharing until you explicitly approve it.
      </p>
      <div style={{ marginBottom: 14 }}>
        <div style={{ fontSize: 12, fontWeight: 600, color: colors.gray500, marginBottom: 4 }}>
          1. Scan your sessions
        </div>
        <code style={{
          display: 'inline-block',
          padding: '8px 12px',
          borderRadius: 6,
          background: colors.gray800,
          color: colors.gray50,
          fontSize: 13,
          fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
          userSelect: 'all',
        }}>clawjournal scan</code>
        <span style={{ marginLeft: 10, fontSize: 12, color: colors.gray500 }}>
          Run this in your terminal, then refresh this page.
        </span>
      </div>
      <div style={{ fontSize: 12, fontWeight: 600, color: colors.gray500, marginBottom: 6 }}>
        2. Then you’ll work through:
      </div>
      <ol style={{ margin: 0, paddingLeft: 18, fontSize: 12.5, color: colors.gray600, lineHeight: 1.7 }}>
        <li><strong>Sessions</strong> — review what was captured, right here.</li>
        <li><strong>Share</strong> — {roadmap} to package and (optionally) submit traces.</li>
      </ol>
    </div>
  );
}
