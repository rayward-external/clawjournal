import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { api } from '../api.ts';
import {
  BUG_REPORT_CONTEXT_TIMEOUT_MS,
  BUG_REPORT_FILENAME,
  BUG_REPORT_URL,
  MAX_BUG_REPORT_DRAFT_LENGTH,
  buildBugReportDraft,
  deriveBrowserHost,
  projectSupportContext,
  safeRouteTemplate,
} from '../bugReportDraft.ts';
import type { BugReportLocation, BugReportSurface } from '../bugReportDraft.ts';
import type { SupportContext } from '../types.ts';
import { btnPrimary, btnSecondary, colors, fontFamily, inputStyle, labelStyle } from '../theme.ts';

interface BugReportDialogProps {
  open: boolean;
  onClose: () => void;
  surface: BugReportSurface;
  location?: BugReportLocation;
}

type DiagnosticsState = 'loading' | 'ready' | 'unavailable';
type DialogStage = 'compose' | 'review';

const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'textarea:not([disabled])',
  'select:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

export function BugReportDialog({ open, onClose, surface, location }: BugReportDialogProps) {
  const [stage, setStage] = useState<DialogStage>('compose');
  const [summary, setSummary] = useState('');
  const [whatHappened, setWhatHappened] = useState('');
  const [expectedBehavior, setExpectedBehavior] = useState('');
  const [diagnosticsState, setDiagnosticsState] = useState<DiagnosticsState>('loading');
  const [supportContext, setSupportContext] = useState<SupportContext | null>(null);
  const [draft, setDraft] = useState('');
  const [actionStatus, setActionStatus] = useState('');
  const dialogRef = useRef<HTMLDivElement>(null);
  const summaryRef = useRef<HTMLInputElement>(null);
  const draftRef = useRef<HTMLTextAreaElement>(null);
  const downloadUrlRef = useRef<string | null>(null);
  const downloadTimerRef = useRef<number | null>(null);
  const titleId = useId();
  const privacyId = useId();
  const diagnosticsId = useId();

  const releaseDownloadUrl = useCallback(() => {
    if (downloadTimerRef.current !== null) {
      window.clearTimeout(downloadTimerRef.current);
      downloadTimerRef.current = null;
    }
    if (downloadUrlRef.current !== null) {
      URL.revokeObjectURL(downloadUrlRef.current);
      downloadUrlRef.current = null;
    }
  }, []);

  useEffect(() => {
    if (!open) {
      releaseDownloadUrl();
      return;
    }
    return releaseDownloadUrl;
  }, [open, releaseDownloadUrl]);

  useEffect(() => {
    if (!open) return;
    const previousFocus = document.activeElement as HTMLElement | null;
    return () => previousFocus?.focus?.();
  }, [open]);

  useEffect(() => {
    if (!open) return;
    if (stage === 'review') draftRef.current?.focus();
    else summaryRef.current?.focus();
  }, [open, stage]);

  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    let active = true;
    setDiagnosticsState('loading');
    setSupportContext(null);
    const timeout = window.setTimeout(
      () => controller.abort(),
      BUG_REPORT_CONTEXT_TIMEOUT_MS,
    );

    void api.support.context(controller.signal)
      .then(raw => {
        if (!active) return;
        const projected = projectSupportContext(raw);
        setSupportContext(projected);
        setDiagnosticsState(projected ? 'ready' : 'unavailable');
      })
      .catch(() => {
        if (!active) return;
        setSupportContext(null);
        setDiagnosticsState('unavailable');
      })
      .finally(() => window.clearTimeout(timeout));

    return () => {
      active = false;
      controller.abort();
      window.clearTimeout(timeout);
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        onClose();
        return;
      }
      if (event.key !== 'Tab') {
        // Keep route-level shortcuts from acting behind the modal. In
        // particular, Enter remains untouched so textareas keep newlines.
        event.stopPropagation();
        return;
      }

      const focusables = Array.from(
        dialogRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR) ?? [],
      ).filter(element => element.getAttribute('aria-hidden') !== 'true');
      if (focusables.length === 0) {
        event.preventDefault();
        event.stopPropagation();
        return;
      }
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      if (!dialogRef.current?.contains(document.activeElement)) {
        event.preventDefault();
        first.focus();
      } else if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
      event.stopPropagation();
    };
    window.addEventListener('keydown', handleKeyDown, true);
    return () => window.removeEventListener('keydown', handleKeyDown, true);
  }, [open, onClose]);

  if (!open) return null;

  const reviewDraft = () => {
    const currentLocation = location ?? {
      pathname: window.location.pathname,
      search: window.location.search,
    };
    setDraft(buildBugReportDraft({
      summary,
      whatHappened,
      expectedBehavior,
      routeTemplate: safeRouteTemplate(currentLocation),
      surface,
      browserHost: deriveBrowserHost({
        userAgent: navigator.userAgent,
        innerWidth: window.innerWidth,
        innerHeight: window.innerHeight,
        devicePixelRatio: window.devicePixelRatio,
      }),
      supportContext: diagnosticsState === 'ready' ? supportContext : null,
    }));
    setActionStatus('');
    setStage('review');
  };

  const copyDraft = async () => {
    try {
      if (!navigator.clipboard?.writeText) throw new Error('clipboard unavailable');
      await navigator.clipboard.writeText(draft);
      setActionStatus('Draft copied to the clipboard.');
    } catch {
      draftRef.current?.focus();
      draftRef.current?.select();
      setActionStatus('Clipboard access was unavailable. The draft is selected; press Ctrl+C or Command+C.');
    }
  };

  const downloadDraft = () => {
    releaseDownloadUrl();
    const blob = new Blob([draft], { type: 'text/markdown;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    downloadUrlRef.current = url;
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = BUG_REPORT_FILENAME;
    anchor.rel = 'noopener';
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    downloadTimerRef.current = window.setTimeout(() => {
      if (downloadUrlRef.current === url) {
        URL.revokeObjectURL(url);
        downloadUrlRef.current = null;
      }
      downloadTimerRef.current = null;
    }, 60_000);
    setActionStatus(`Downloaded ${BUG_REPORT_FILENAME}.`);
  };

  const canReview = summary.trim().length > 0 && whatHappened.trim().length > 0;

  return (
    <div style={{
      position: 'fixed',
      inset: 0,
      zIndex: 10_000,
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      padding: 20,
      background: 'rgba(27, 26, 23, 0.42)',
      fontFamily,
    }}>
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={`${privacyId} ${diagnosticsId}`}
        style={{
          width: 'min(680px, 100%)',
          maxHeight: '90vh',
          overflow: 'auto',
          padding: '24px 26px',
          borderRadius: 12,
          border: `1px solid ${colors.gray200}`,
          background: colors.white,
          boxShadow: '0 18px 60px rgba(27, 26, 23, 0.24)',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: 16 }}>
          <div style={{ flex: 1 }}>
            <h2 id={titleId} style={{ margin: '0 0 6px', color: colors.gray900, fontSize: 20 }}>
              Report a problem
            </h2>
            <p id={privacyId} style={{ margin: 0, color: colors.gray600, fontSize: 13.5, lineHeight: 1.55 }}>
              This draft stays in this browser tab. ClawJournal does not capture a screenshot, upload the draft, or submit an issue. Review and remove sensitive information before sharing it.
            </p>
          </div>
          <button type="button" onClick={onClose} aria-label="Close bug report" style={{
            ...btnSecondary,
            flexShrink: 0,
            padding: '5px 10px',
          }}>
            Close
          </button>
        </div>

        <div id={diagnosticsId} role="status" aria-live="polite" style={{
          marginTop: 14,
          padding: '8px 10px',
          borderRadius: 7,
          background: diagnosticsState === 'unavailable' ? colors.yellow50 : colors.gray50,
          color: diagnosticsState === 'unavailable' ? colors.yellow700 : colors.gray500,
          fontSize: 12.5,
        }}>
          {diagnosticsState === 'loading' && 'Checking privacy-bounded local diagnostics… You can continue without waiting.'}
          {diagnosticsState === 'ready' && 'Privacy-bounded local diagnostics are ready for review.'}
          {diagnosticsState === 'unavailable' && 'Local diagnostics are unavailable. You can still create, copy, and download the report.'}
        </div>

        {stage === 'compose' ? (
          <form onSubmit={event => { event.preventDefault(); if (canReview) reviewDraft(); }} style={{ marginTop: 16 }}>
            <label htmlFor={`${titleId}-summary`} style={labelStyle}>
              Summary <span aria-hidden="true">*</span>
            </label>
            <input
              ref={summaryRef}
              id={`${titleId}-summary`}
              required
              maxLength={120}
              value={summary}
              onChange={event => setSummary(event.target.value)}
              style={inputStyle}
            />
            <div style={{ textAlign: 'right', color: colors.gray400, fontSize: 11.5 }}>{summary.length}/120</div>

            <label htmlFor={`${titleId}-happened`} style={labelStyle}>
              What happened / how to reproduce <span aria-hidden="true">*</span>
            </label>
            <textarea
              id={`${titleId}-happened`}
              required
              maxLength={4_000}
              rows={7}
              value={whatHappened}
              onChange={event => setWhatHappened(event.target.value)}
              style={{ ...inputStyle, resize: 'vertical', lineHeight: 1.45 }}
            />
            <div style={{ textAlign: 'right', color: colors.gray400, fontSize: 11.5 }}>{whatHappened.length}/4000</div>

            <label htmlFor={`${titleId}-expected`} style={labelStyle}>Expected behavior (optional)</label>
            <textarea
              id={`${titleId}-expected`}
              maxLength={2_000}
              rows={4}
              value={expectedBehavior}
              onChange={event => setExpectedBehavior(event.target.value)}
              style={{ ...inputStyle, resize: 'vertical', lineHeight: 1.45 }}
            />
            <div style={{ textAlign: 'right', color: colors.gray400, fontSize: 11.5 }}>{expectedBehavior.length}/2000</div>

            <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 18 }}>
              <button
                type="submit"
                disabled={!canReview}
                style={{
                  ...btnPrimary,
                  opacity: canReview ? 1 : 0.55,
                  cursor: canReview ? 'pointer' : 'not-allowed',
                }}
              >
                Review draft
              </button>
            </div>
          </form>
        ) : (
          <div style={{ marginTop: 16 }}>
            <label htmlFor={`${titleId}-draft`} style={{ ...labelStyle, marginTop: 0 }}>
              Review and edit the exact Markdown that you may choose to share
            </label>
            <textarea
              ref={draftRef}
              id={`${titleId}-draft`}
              maxLength={MAX_BUG_REPORT_DRAFT_LENGTH}
              rows={18}
              value={draft}
              onChange={event => {
                setDraft(event.target.value.slice(0, MAX_BUG_REPORT_DRAFT_LENGTH));
                setActionStatus('');
              }}
              style={{
                ...inputStyle,
                resize: 'vertical',
                lineHeight: 1.45,
                fontFamily: "ui-monospace, SFMono-Regular, Consolas, monospace",
                fontSize: 12.5,
              }}
            />
            <p style={{ margin: '8px 0 0', color: colors.gray500, fontSize: 12.5, lineHeight: 1.5 }}>
              Copy or download only after reviewing this text. Opening GitHub sends none of it automatically.
            </p>
            <div role="status" aria-live="polite" style={{ minHeight: 20, marginTop: 6, color: colors.blue700, fontSize: 12.5 }}>
              {actionStatus}
            </div>
            <div style={{
              display: 'flex',
              flexWrap: 'wrap',
              justifyContent: 'flex-end',
              gap: 8,
              marginTop: 10,
            }}>
              <button type="button" onClick={() => { setStage('compose'); setActionStatus(''); }} style={btnSecondary}>
                Back
              </button>
              <button type="button" onClick={() => void copyDraft()} style={btnSecondary}>
                Copy draft
              </button>
              <button type="button" onClick={downloadDraft} style={btnSecondary}>
                Download .md
              </button>
              <a
                href={BUG_REPORT_URL}
                target="_blank"
                rel="noopener noreferrer"
                style={{ ...btnPrimary, display: 'inline-block', textDecoration: 'none' }}
              >
                Open blank GitHub issue
              </a>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
