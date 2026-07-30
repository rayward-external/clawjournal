import { useCallback, useEffect, useId, useRef, useState } from 'react';
import type { CSSProperties } from 'react';
import { Link } from 'react-router-dom';
import { api, ApiError } from '../api.ts';
import {
  createAutoUploadEnableProgressId,
  startAutoUploadEnableProgressPolling,
} from '../autoUploadEnableProgress.ts';
import type {
  AutoUploadAuthorizationChallenge,
  AutoUploadCandidateReport,
  AutoUploadEnableProgress,
  AutoUploadStatus,
} from '../types.ts';
import { colors, btnDanger, btnGhost, btnPrimary, btnSecondary } from '../theme.ts';
import { ConfirmDialog } from './ConfirmDialog.tsx';
import { useToast } from './Toast.tsx';
import { challengeFromError } from './autoUploadChallenge.ts';

const OFFER_DISMISSED_KEY = 'clawjournal.autoUploadOfferDismissed.v1';

const exclusionLabels: Record<string, string> = {
  pre_enrollment: 'Completed before enrollment',
  unsupported_unsettled: 'Not yet safely complete',
  held_or_embargoed: 'Held or embargoed',
  blocked_review_status: 'Needs review',
  changed_revision_needing_approval: 'Updated trace needs fresh approval',
  already_shared: 'Already shared',
  source_excluded: 'Outside enrolled sources',
  project_excluded: 'Outside enrolled projects',
  scope_pair_excluded: 'Outside exact enrolled scope',
  missing_blob: 'Local trace unavailable',
  raw_source_unavailable: 'Original trace unavailable',
  scope_confirmation_changed: 'Scope confirmation changed',
  deferred_by_size: 'Deferred by size',
};

const reviewReasons = new Set([
  'held_or_embargoed',
  'blocked_review_status',
  'changed_revision_needing_approval',
]);

function stringField(record: Record<string, unknown> | null, key: string): string | null {
  const value = record?.[key];
  return typeof value === 'string' && value.trim() ? value : null;
}

function stringList(record: Record<string, unknown> | null, key: string): string[] {
  const value = record?.[key];
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : [];
}

function hasScopeBlocker(error: unknown, blocker: string): boolean {
  if (!(error instanceof ApiError)) return false;
  return stringList(error.body, 'scope_blockers').includes(blocker)
    || stringField(error.body, 'code') === blocker;
}

function requiresEmailVerification(error: unknown): boolean {
  return error instanceof ApiError && (
    error.body.code === 'email_verification_required' ||
    error.body.code === 'enrollment_response_ambiguous'
  );
}

function errorMessage(error: unknown, fallback: string): string {
  if (error instanceof ApiError) {
    const message = typeof error.body.message === 'string' ? error.body.message : error.message;
    return message || fallback;
  }
  return error instanceof Error ? error.message : fallback;
}

function formatTimestamp(value: string | null): string {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function compactList(values: string[], empty: string): string {
  if (!values.length) return empty;
  if (values.length <= 4) return values.join(', ');
  return `${values.slice(0, 4).join(', ')} +${values.length - 4} more`;
}

function sourceLabel(source: string): string {
  const labels: Record<string, string> = {
    all: 'All agents',
    aider: 'Aider',
    both: 'Claude Code and Codex',
    claude: 'Claude Code',
    'claude-science': 'Claude Science',
    codex: 'Codex',
    copilot: 'GitHub Copilot',
    cursor: 'Cursor',
    custom: 'Custom',
    gemini: 'Gemini',
    kimi: 'Kimi',
    openclaw: 'OpenClaw',
    opencode: 'OpenCode',
    workbuddy: 'WorkBuddy',
  };
  return labels[source] ?? source;
}

function disabledStyle(disabled: boolean): CSSProperties {
  return disabled ? { opacity: 0.5, cursor: 'not-allowed' } : {};
}

function automaticCheckInAgents(sources: string[]): string {
  const selected = new Set(sources);
  if (selected.has('claude') && selected.has('codex')) return 'Claude Code and Codex';
  if (selected.has('claude')) return 'Claude Code';
  if (selected.has('codex')) return 'Codex';
  return 'No supported agents';
}

function StatusChip({ children, tone = 'neutral' }: {
  children: React.ReactNode;
  tone?: 'neutral' | 'good' | 'warning' | 'danger' | 'info';
}) {
  const tones = {
    neutral: { background: colors.gray100, border: colors.gray200, color: colors.gray700 },
    good: { background: colors.green50, border: colors.green200, color: colors.green700 },
    warning: { background: colors.yellow50, border: colors.yellow200, color: colors.yellow700 },
    danger: { background: colors.red50, border: colors.red200, color: colors.red700 },
    info: { background: colors.blue50, border: colors.blue100, color: colors.blue700 },
  }[tone];
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', padding: '3px 8px', borderRadius: 999,
      background: tones.background, border: `1px solid ${tones.border}`, color: tones.color,
      fontSize: 11.5, fontWeight: 600,
    }}>
      {children}
    </span>
  );
}

function ModeAndHealth({ status }: { status: AutoUploadStatus }) {
  const modeLabel = status.mode === 'enabled' ? 'On' : status.mode === 'paused' ? 'Paused' : 'Off';
  const modeTone = status.mode === 'enabled' ? 'good' : status.mode === 'paused' ? 'warning' : 'neutral';
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
      <StatusChip tone={modeTone}>{modeLabel}</StatusChip>
      {status.health === 'action_required' && <StatusChip tone="danger">Action required</StatusChip>}
      {status.health === 'retrying' && <StatusChip tone="warning">Retrying</StatusChip>}
      {status.overlay === 'running' && <StatusChip tone="info">Running</StatusChip>}
      {status.overlay === 'enrollment_pending' && (
        <StatusChip tone="info">Setting up</StatusChip>
      )}
      {status.overlay === 'revocation_pending' && (
        <StatusChip tone="warning">Revocation pending</StatusChip>
      )}
      {status.pending_submission_state === 'submitting' && (
        <StatusChip tone="warning">Request may be in flight</StatusChip>
      )}
      {status.pending_submission_state === 'sealed' && (
        <StatusChip tone="info">Sealed recovery pending</StatusChip>
      )}
    </div>
  );
}

interface AuthorizationDialogProps {
  open: boolean;
  initialStatus: AutoUploadStatus;
  onClose: () => void;
  onEnabled: (status: AutoUploadStatus) => void;
  inline?: boolean;
}

function AuthorizationDialog({
  open,
  initialStatus,
  onClose,
  onEnabled,
  inline = false,
}: AuthorizationDialogProps) {
  const { toast } = useToast();
  const titleId = useId();
  const requestedRef = useRef(false);
  const challengeRequestRef = useRef(0);
  const dialogRef = useRef<HTMLElement>(null);
  const [challenge, setChallenge] = useState<AutoUploadAuthorizationChallenge | null>(null);
  const [accepted, setAccepted] = useState(false);
  const [authorizationDetailsOpen, setAuthorizationDetailsOpen] = useState(false);
  // Protocol v2: the ownership certification is a distinct affirmative act,
  // mirroring the manual share's separate certify checkbox — never bundled
  // into the terms acceptance above.
  const [ownershipCertified, setOwnershipCertified] = useState(false);
  const [loading, setLoading] = useState(false);
  // Distinct from `loading`: true only while the mutating enable POST is in
  // flight. Dismissal is blocked during that brief window but allowed during
  // the read-only challenge fetch, which can otherwise hang (slow strict scan
  // or unreachable hosted origin) and trap the user in an unclosable modal.
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [verificationRequired, setVerificationRequired] = useState(false);
  const [tokenValid, setTokenValid] = useState(false);
  const [verifiedEmail, setVerifiedEmail] = useState<string | null>(null);
  const [pendingEmail, setPendingEmail] = useState<string | null>(null);
  const [email, setEmail] = useState('');
  const [code, setCode] = useState('');
  const [devCode, setDevCode] = useState<string | null>(null);
  const [enableProgress, setEnableProgress] =
    useState<AutoUploadEnableProgress | null>(null);
  const showEmailVerification = useCallback(async (
    message: string,
    isCurrent: () => boolean = () => true,
  ) => {
    if (!isCurrent()) return;
    setVerificationRequired(true);
    setTokenValid(false);
    try {
      const status = await api.share.uploadStatus();
      if (!isCurrent()) return;
      setVerifiedEmail(status.verified_email);
      setPendingEmail(status.pending_email);
      setEmail(status.pending_email || status.verified_email || '');
    } catch {
      // Keep the verification form usable even if the status refresh failed.
    }
    if (isCurrent()) setError(message);
  }, []);

  const requestChallenge = useCallback(async () => {
    const requestId = challengeRequestRef.current + 1;
    challengeRequestRef.current = requestId;
    const isCurrent = () => challengeRequestRef.current === requestId;
    setLoading(true);
    setError(null);
    setAccepted(false);
    setAuthorizationDetailsOpen(false);
    setOwnershipCertified(false);
    setChallenge(null);
    try {
      // challenge_only can never enroll by contract, so a success here means
      // the daemon violated that contract; refuse to treat it as enabled.
      await api.autoUpload.enable({ agent: 'auto', challenge_only: true });
      if (isCurrent()) {
        setError('The service did not return the required authorization challenge. Review status before continuing.');
      }
    } catch (requestError) {
      if (!isCurrent()) return;
      const next = challengeFromError(requestError);
      if (next) {
        setChallenge(next);
      } else if (requiresEmailVerification(requestError)) {
        await showEmailVerification(
          'Verify your email before loading the recurring authorization.',
          isCurrent,
        );
      } else if (
        requestError instanceof ApiError &&
        requestError.status === 409 &&
        requestError.body.code === 'authorization_required'
      ) {
        // A 409 challenge this build cannot parse means the running daemon
        // speaks an older protocol than this page (e.g. dist upgraded but the
        // daemon not restarted). Retrying can never succeed — say so.
        setError(
          'The running ClawJournal service is older than this page and returned '
          + 'an incompatible authorization challenge. Restart the service '
          + '(clawjournal serve) and reload, then try again.',
        );
      } else if (hasScopeBlocker(requestError, 'scope_too_large')) {
        // The guided step cannot shrink an oversized scope: that needs project
        // exclusions, which have no controls here. Say what to do instead of
        // echoing the CLI-worded server message.
        setError(
          'This scope has too many exact source/project pairs for the '
          + 'hosted service. Exclude projects from sharing (clawjournal config '
          + '--exclude "<project>"), then try again.',
        );
      } else {
        setError(errorMessage(requestError, 'Could not load recurring authorization.'));
      }
    } finally {
      if (isCurrent()) setLoading(false);
    }
  }, [showEmailVerification]);

  const dismissDialog = useCallback(() => {
    challengeRequestRef.current += 1;
    onClose();
  }, [onClose]);

  useEffect(() => {
    if (!open) {
      challengeRequestRef.current += 1;
      requestedRef.current = false;
      setChallenge(null);
      setAccepted(false);
      setAuthorizationDetailsOpen(false);
      setOwnershipCertified(false);
      setError(null);
      setVerificationRequired(false);
      setTokenValid(false);
      setVerifiedEmail(null);
      setPendingEmail(null);
      setEmail('');
      setCode('');
      setDevCode(null);
      setEnableProgress(null);
      return;
    }
    if (!requestedRef.current) {
      requestedRef.current = true;
      void requestChallenge();
    }
  }, [initialStatus, open, requestChallenge]);

  // Move focus into the dialog on open and restore it to the previously
  // focused element on close, so keyboard and screen-reader focus follow the
  // modal instead of staying on the background controls behind the overlay.
  useEffect(() => {
    if (!open || inline) return;
    const previousFocus = document.activeElement as HTMLElement | null;
    dialogRef.current?.focus();
    return () => { previousFocus?.focus?.(); };
  }, [inline, open]);

  // Behave like a real modal: trap Tab within the dialog's focusables and
  // swallow every other page keystroke (capture phase) so background controls
  // can never be operated through the overlay. Escape dismisses unless a
  // mutating submit is in flight.
  useEffect(() => {
    if (!open || inline) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.stopPropagation();
        if (!submitting) { event.preventDefault(); dismissDialog(); }
        return;
      }
      if (event.key === 'Tab') {
        const focusables = dialogRef.current?.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), input:not([disabled]), ' +
          'select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
        );
        if (focusables && focusables.length > 0) {
          const first = focusables[0];
          const last = focusables[focusables.length - 1];
          if (event.shiftKey && document.activeElement === first) {
            event.preventDefault();
            last.focus();
          } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first.focus();
          }
        }
      }
      event.stopPropagation();
    };
    window.addEventListener('keydown', onKeyDown, true);
    return () => window.removeEventListener('keydown', onKeyDown, true);
  }, [dismissDialog, inline, submitting, open]);

  if (!open) return null;

  const scope = challenge?.scope ?? initialStatus.scope;
  const ai = challenge?.ai ?? initialStatus.ai;
  const cap = challenge?.cap ?? initialStatus.cap;
  const cadence = challenge?.cadence_days ?? initialStatus.cadence_days;

  const enableWithAcceptedTerms = async (authorizationAccepted = accepted) => {
    if (!challenge || !authorizationAccepted || !ownershipCertified) return;
    setSubmitting(true);
    const progressId = createAutoUploadEnableProgressId();
    const stopProgressPolling = startAutoUploadEnableProgressPolling(
      progressId,
      setEnableProgress,
    );
    try {
      const next = await api.autoUpload.enable({
        agent: 'auto',
        accepted_authorization_version: challenge.authorization.version,
        accepted_retention_version: challenge.retention.version,
        accepted_ownership_certification_version: challenge.ownership_certification.version,
        accepted_authorization_profile_hash: challenge.authorization_profile_hash,
        progress_id: progressId,
      });
      onEnabled(next);
      toast('Automatic upload enabled', 'success');
      dismissDialog();
    } catch (enableError) {
      const refreshedChallenge = challengeFromError(enableError);
      if (refreshedChallenge) {
        setChallenge(refreshedChallenge);
        setAccepted(false);
        setOwnershipCertified(false);
        setVerificationRequired(false);
        setError('The authorization changed while you were reviewing it. Please review the new text.');
      } else if (requiresEmailVerification(enableError)) {
        await showEmailVerification('Your email verification is missing or expired. Send a fresh code to continue.');
      } else {
        setError(errorMessage(enableError, 'Could not enable automatic upload.'));
      }
    } finally {
      stopProgressPolling();
      setEnableProgress(null);
      setSubmitting(false);
    }
  };

  const accept = async () => {
    const authorizationAccepted = inline ? accepted : true;
    if (!challenge || !authorizationAccepted || !ownershipCertified || loading) return;
    if (!inline) setAccepted(true);
    setLoading(true);
    setError(null);
    try {
      // Updating an active enrollment uses its pinned active credential. Only
      // a new enrollment (or an explicit credential error returned below)
      // requires another one-shot email verification.
      if (initialStatus.mode !== 'off' || initialStatus.enrollment_grant_available) {
        await enableWithAcceptedTerms(authorizationAccepted);
        return;
      }
      const status = await api.share.uploadStatus();
      setTokenValid(status.token_valid);
      setVerifiedEmail(status.verified_email);
      setPendingEmail(status.pending_email);
      setEmail(status.pending_email || status.verified_email || '');
      if (!status.token_valid) {
        setVerificationRequired(true);
        setError('A fresh email verification is required to create or update this recurring enrollment.');
        return;
      }
      setVerificationRequired(false);
      await enableWithAcceptedTerms(authorizationAccepted);
    } catch (statusError) {
      setError(errorMessage(statusError, 'Could not check email verification status.'));
    } finally {
      setLoading(false);
    }
  };

  const sendCode = async () => {
    if (!email.trim() || loading) return;
    setLoading(true);
    setError(null);
    try {
      const result = await api.share.verifyEmail(email.trim());
      setPendingEmail(result.email);
      setEmail(result.email);
      setCode('');
      setDevCode(result.dev_code || null);
      toast('Verification code sent', 'success');
    } catch (sendError) {
      setError(errorMessage(sendError, 'Could not send verification code.'));
    } finally {
      setLoading(false);
    }
  };

  const verifyCode = async () => {
    if (!code.trim() || loading) return;
    setLoading(true);
    setError(null);
    try {
      const result = await api.share.verifyConfirm(code.trim());
      setTokenValid(true);
      setVerifiedEmail(result.verified_email);
      setPendingEmail(null);
      setDevCode(null);
      toast('Email verified', 'success');
      if (challenge && accepted && ownershipCertified) {
        await enableWithAcceptedTerms();
      } else if (challenge) {
        // Verified but an acceptance box is unchecked: surface which act is
        // missing instead of a silent dead-end after the code is consumed.
        setError(
          accepted
            ? 'Check the ownership certification box, then press Continue to enable.'
            : 'Check both acceptance boxes, then press Continue to enable.',
        );
      } else {
        setVerificationRequired(false);
        requestedRef.current = true;
        await requestChallenge();
      }
    } catch (verifyError) {
      setError(errorMessage(verifyError, 'Could not verify code.'));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div
      role={inline ? undefined : 'presentation'}
      onMouseDown={event => {
        if (!inline && event.target === event.currentTarget && !submitting) dismissDialog();
      }}
      style={inline ? {
        margin: '20px auto', maxWidth: 680, padding: '18px 20px', textAlign: 'left',
        border: `1px solid ${colors.primary200}`, borderRadius: 10,
        background: colors.primary50,
      } : {
        position: 'fixed', inset: 0, zIndex: 9998, padding: 20,
        display: 'grid', placeItems: 'center', background: 'rgba(27, 26, 23, 0.45)',
      }}
    >
      <section
        ref={dialogRef}
        tabIndex={inline ? undefined : -1}
        role={inline ? 'region' : 'dialog'}
        aria-modal={inline ? undefined : 'true'}
        aria-labelledby={titleId}
        style={inline ? {
          width: '100%',
        } : {
          width: 'min(680px, calc(100vw - 40px))', maxHeight: 'calc(100vh - 40px)',
          overflow: 'auto', background: colors.white, borderRadius: 12,
          boxShadow: '0 18px 50px rgba(0,0,0,0.22)', padding: '22px 24px',
        }}
      >
        <h3 id={titleId} style={{ margin: '0 0 6px', fontSize: 18, color: colors.gray900 }}>
          {inline ? 'Share future traces automatically?' : 'Enable automatic uploads?'}
        </h3>
        <p style={{ margin: '0 0 18px', fontSize: 13, lineHeight: 1.55, color: colors.gray600 }}>
          {inline
            ? `Enable up to ${cap} eligible future traces every ${cadence} day${cadence === 1 ? '' : 's'} from all supported agent sources in the exact scope below. ${
              initialStatus.enrollment_grant_available
                ? 'The receipt from this share lets you enable it without verifying your email again.'
                : 'If its receipt-issued enrollment grant is unavailable or expires, you will verify your email before enabling.'
            }`
            : `ClawJournal will automatically upload up to ${cap} eligible, locally redacted future traces every ${cadence} day${cadence === 1 ? '' : 's'} from supported agents. You can turn this off at any time.`}
        </p>

        {!inline && challenge && (
          <button
            type="button"
            aria-expanded={authorizationDetailsOpen}
            onClick={() => setAuthorizationDetailsOpen(open => !open)}
            style={{
              ...btnGhost,
              padding: 0,
              minHeight: 0,
              marginBottom: 14,
              border: 'none',
              textDecoration: 'underline',
              fontSize: 12,
            }}
          >
            {authorizationDetailsOpen ? 'Hide scope and terms' : 'View scope and terms'}
          </button>
        )}

        {/* SessionStart hooks follow the exact upload scope. Keeping this
            read-only prevents a trigger selector from looking like a second,
            conflicting data-scope control. */}
        {(inline || authorizationDetailsOpen) && (
        <>
        <SummaryItem
          label="Automatic check-ins"
          value={automaticCheckInAgents(scope.sources) + ' - matches exact upload scope'}
        />

        <div style={summaryGridStyle}>
          <SummaryItem label="Sources" value={compactList(scope.sources, 'No supported sources found')} />
          <SummaryItem label="Projects" value={compactList(scope.projects, 'No eligible projects found')} />
          <SummaryItem label="Schedule" value={`Every ${cadence} day${cadence === 1 ? '' : 's'}, on the next supported agent session`} />
          <SummaryItem label="Per-cycle cap" value={`Up to ${cap} selected traces`} />
          {challenge && (
            <SummaryItem
              label="Hosted bundle limit"
              value={`${(challenge.maximum_bundle_size / (1024 * 1024)).toFixed(1)} MiB`}
            />
          )}
          {challenge?.destination_origin && (
            <SummaryItem label="Destination" value={challenge.destination_origin} />
          )}
          <SummaryItem
            label="Future-only boundary"
            value={initialStatus.enrolled_at
              ? `Traces completed after ${formatTimestamp(initialStatus.enrolled_at)}`
              : 'Only traces completed after enrollment'}
          />
          <SummaryItem
            label="AI-assisted PII review"
            value={ai.enabled ? `On · ${ai.backend ?? 'configured provider'}` : 'Off'}
          />
        </div>

        <div style={{ margin: '14px 0', fontSize: 12.5, lineHeight: 1.55, color: colors.gray600 }}>
          <ul style={{ margin: 0, paddingLeft: 19 }}>
            <li>All currently supported agent sources are included automatically.</li>
            <li>ClawJournal anonymizes and redacts locally, then runs the existing findings and secret-scan gates.</li>
            <li>Run now is an extra capped cycle and resets the next scheduled date.</li>
            <li>You can preview, pause, review the exact scope, or turn this off in Settings.</li>
            <li>Turning it off does not delete prior uploads. A request already being submitted may finish.</li>
          </ul>
        </div>
        </>
        )}

        {loading && !challenge && !verificationRequired && (
          <div role="status" style={noticeStyle}>Loading the current authorization and retention terms…</div>
        )}
        {error && (
          <div role="alert" style={{ ...noticeStyle, background: colors.red50, borderColor: colors.red200, color: colors.red700 }}>
            {error}
          </div>
        )}
        {error && !challenge && !loading && !verificationRequired && (
          <button onClick={() => void requestChallenge()} style={{ ...btnSecondary, marginBottom: 12 }}>
            Retry
          </button>
        )}

        {challenge && (
          <>
            {(inline || authorizationDetailsOpen) && (
            <>
            <TermsBlock
              title={`Recurring authorization · ${challenge.authorization.version}`}
              text={challenge.authorization.text}
            />
            <TermsBlock
              title={`Retention policy · ${challenge.retention.version}`}
              text={challenge.retention.text}
            />
            <TermsBlock
              title={`Exact recurring scope · ${challenge.scope.entries.length} source/project pair${challenge.scope.entries.length === 1 ? '' : 's'}`}
              text={challenge.scope.entries
                .map(([source, project]) => `${source} → ${project}`)
                .join('\n')}
            />
            </>
            )}
            {inline && (
            <label style={{
              display: 'flex', gap: 9, alignItems: 'flex-start', marginTop: 14,
              fontSize: 13, lineHeight: 1.45, color: colors.gray800,
            }}>
              <input
                type="checkbox"
                checked={accepted}
                disabled={loading}
                onChange={event => setAccepted(event.target.checked)}
                style={{ marginTop: 3 }}
              />
              <span>
                I authorize recurring sharing from the exact source/project pairs shown above and accept the authorization
                and retention versions shown above. I understand selected traces can upload without
                my reviewing each bundle, and I represent that I am authorized to share traces from
                this scope.
              </span>
            </label>
            )}
            {(inline || authorizationDetailsOpen) && (
            <TermsBlock
              title={`Ownership certification · ${challenge.ownership_certification.version}`}
              text={challenge.ownership_certification.text}
            />
            )}
            <label style={{
              display: 'flex', gap: 9, alignItems: 'flex-start', marginTop: 14,
              fontSize: 13, lineHeight: 1.45, color: colors.gray800,
            }}>
              <input
                type="checkbox"
                checked={ownershipCertified}
                disabled={loading}
                onChange={event => setOwnershipCertified(event.target.checked)}
                style={{ marginTop: 3 }}
                aria-label="Certify bundle ownership"
              />
              <span>
                {inline
                  ? 'I certify the ownership statement above for every automatically uploaded bundle.'
                  : 'I confirm future uploaded traces are mine to share and contain no third-party confidential material.'}
              </span>
            </label>
          </>
        )}

        {verificationRequired && (
          <div style={{
            marginTop: 14, padding: '12px 14px', display: 'grid', gap: 8,
            border: `1px solid ${colors.primary200}`, borderRadius: 8,
            background: colors.primary50,
          }}>
            <div style={{ fontSize: 13, fontWeight: 650, color: colors.gray900 }}>
              Verify your email
            </div>
            {/* This block is also reached from the settings panel while
                rotating an active enrollment's credentials, where no grant was
                ever issued. Naming one unconditionally would tell those users
                a receipt grant expired when they never had one. */}
            <div style={{ fontSize: 12, lineHeight: 1.45, color: colors.gray600 }}>
              {initialStatus.enrollment_grant_available
                ? 'The receipt-issued enrollment grant is unavailable or expired. Verify your email to continue; your recurring terms selection above remains unchanged.'
                : 'Recurring enrollment needs its own single-use email verification. Verify to continue; your recurring terms selection above remains unchanged.'}
            </div>
            {tokenValid ? (
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8, flexWrap: 'wrap' }}>
                <span style={{ fontSize: 12.5, color: colors.green700 }}>
                  Verified as {verifiedEmail ?? email}
                </span>
                <button
                  disabled={loading || !challenge || !accepted || !ownershipCertified}
                  onClick={() => void accept()}
                  style={{ ...btnPrimary, ...disabledStyle(loading || !challenge || !accepted || !ownershipCertified) }}
                >
                  {loading ? 'Enabling…' : 'Continue'}
                </button>
              </div>
            ) : (
              <>
                <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                  <input
                    type="email"
                    autoComplete="email"
                    value={email}
                    disabled={loading}
                    onChange={event => setEmail(event.target.value)}
                    placeholder="name@university.edu"
                    style={{
                      flex: '1 1 240px', minWidth: 0, padding: '8px 10px',
                      border: `1px solid ${colors.gray300}`, borderRadius: 8, fontSize: 13,
                    }}
                  />
                  <button
                    disabled={loading || !email.trim()}
                    onClick={() => void sendCode()}
                    style={{ ...btnSecondary, ...disabledStyle(loading || !email.trim()) }}
                  >
                    {loading && !pendingEmail ? 'Sending…' : 'Send code'}
                  </button>
                </div>
                {pendingEmail && (
                  <div style={{ fontSize: 11.5, color: colors.gray500 }}>
                    Code sent to {pendingEmail}.
                  </div>
                )}
                {(pendingEmail || devCode) && (
                  <>
                    <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                      <input
                        value={code}
                        disabled={loading}
                        inputMode="numeric"
                        autoComplete="one-time-code"
                        onChange={event => setCode(event.target.value)}
                        placeholder="Verification code"
                        style={{
                          flex: '1 1 180px', minWidth: 0, padding: '8px 10px',
                          border: `1px solid ${colors.gray300}`, borderRadius: 8, fontSize: 13,
                        }}
                      />
                      <button
                        disabled={loading || !code.trim()}
                        onClick={() => void verifyCode()}
                        style={{ ...btnPrimary, ...disabledStyle(loading || !code.trim()) }}
                      >
                        {loading && code.trim() ? 'Verifying…' : challenge && accepted && ownershipCertified ? 'Verify and enable' : 'Verify'}
                      </button>
                    </div>
                    {devCode && (
                      <div style={{ fontSize: 12, color: colors.gray500, fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace' }}>
                        <span style={{ fontFamily: 'Inter, system-ui' }}>Dev code: </span>{devCode}
                      </div>
                    )}
                  </>
                )}
              </>
            )}
          </div>
        )}

        {submitting && enableProgress && (
          <div
            role="status"
            aria-live="polite"
            style={{
              marginTop: 14,
              padding: '10px 12px',
              border: `1px solid ${colors.primary200}`,
              borderRadius: 8,
              background: colors.primary50,
              color: colors.gray800,
              fontSize: 12.5,
              lineHeight: 1.45,
            }}
          >
            <div>{enableProgress.message}</div>
            {enableProgress.stage === 'scanning'
              && enableProgress.current_project !== null
              && enableProgress.total_projects !== null
              && enableProgress.total_projects > 0 && (
                <progress
                  aria-label="Automatic upload enrollment refresh"
                  value={enableProgress.current_project}
                  max={enableProgress.total_projects}
                  style={{ width: '100%', marginTop: 8 }}
                />
              )}
          </div>
        )}

        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 20 }}>
          <button disabled={submitting} onClick={dismissDialog} style={{ ...btnSecondary, ...disabledStyle(submitting) }}>
            {inline ? 'Not now' : 'Cancel'}
          </button>
          {!verificationRequired && (
            <button
              disabled={!challenge || (inline && !accepted) || !ownershipCertified || loading}
              onClick={() => void accept()}
              style={{
                ...btnPrimary,
                ...disabledStyle(!challenge || (inline && !accepted) || !ownershipCertified || loading),
              }}
            >
              {submitting
                ? 'Enabling…'
                : loading && challenge
                  ? 'Refreshing and enabling…'
                  : 'Enable automatic upload'}
            </button>
          )}
        </div>
      </section>
    </div>
  );
}

function TermsBlock({ title, text }: { title: string; text: string }) {
  return (
    <div style={{ marginTop: 12 }}>
      <div style={{ marginBottom: 5, fontSize: 11.5, fontWeight: 700, color: colors.gray700 }}>
        {title}
      </div>
      <div style={{
        maxHeight: 130, overflow: 'auto', padding: '10px 12px', whiteSpace: 'pre-wrap',
        border: `1px solid ${colors.gray200}`, borderRadius: 8, background: colors.gray50,
        fontSize: 12, lineHeight: 1.5, color: colors.gray700,
      }}>
        {text}
      </div>
    </div>
  );
}

function SummaryItem({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div style={{ fontSize: 10.5, textTransform: 'uppercase', letterSpacing: '0.06em', color: colors.gray400 }}>
        {label}
      </div>
      <div style={{ marginTop: 2, fontSize: 12.5, lineHeight: 1.4, color: colors.gray800 }}>
        {value}
      </div>
    </div>
  );
}

export function AutoUploadOffer({ manualReceiptId }: { manualReceiptId: string | null }) {
  const [status, setStatus] = useState<AutoUploadStatus | null>(null);
  // Dismissal is scoped to the receipt it was shown for: "Not now" on one
  // share must not suppress the offer after every future manual share.
  const [dismissedReceipt, setDismissedReceipt] = useState<string | null>(() => {
    try {
      return window.localStorage.getItem(OFFER_DISMISSED_KEY);
    } catch {
      return null;
    }
  });
  const dismissed = manualReceiptId !== null && dismissedReceipt === manualReceiptId;

  const loadStatus = useCallback(() => {
    if (!manualReceiptId || dismissed) return;
    api.autoUpload.status()
      .then(setStatus)
      .catch(() => { /* The optional offer disappears if capability/status is unavailable. */ });
  }, [dismissed, manualReceiptId]);

  useEffect(() => {
    if (!manualReceiptId || dismissed) return;
    let cancelled = false;
    api.autoUpload.status()
      .then(next => { if (!cancelled) setStatus(next); })
      .catch(() => { /* The optional offer disappears if capability/status is unavailable. */ });
    return () => { cancelled = true; };
  }, [dismissed, manualReceiptId]);

  useEffect(() => {
    if (status?.overlay !== 'enrollment_pending') return;
    const interval = window.setInterval(loadStatus, 2_000);
    return () => window.clearInterval(interval);
  }, [loadStatus, status?.overlay]);

  const dismiss = () => {
    if (!manualReceiptId) return;
    setDismissedReceipt(manualReceiptId);
    try { window.localStorage.setItem(OFFER_DISMISSED_KEY, manualReceiptId); } catch { /* best effort */ }
  };

  if (!status || !status.ui_visible || dismissed) return null;

  if (status.overlay === 'enrollment_pending') {
    const setup = status.enrollment_setup;
    const progress = setup?.source && setup.position != null && setup.total != null
      ? ` Refreshing ${setup.source} logs: ${setup.position}/${setup.total}.`
      : '';
    return (
      <div style={{
        margin: '20px auto', maxWidth: 560, padding: '14px 16px', textAlign: 'left',
        border: `1px solid ${colors.blue100}`, borderRadius: 10, background: colors.blue50,
      }}>
        <div style={{ fontSize: 13.5, fontWeight: 700, color: colors.blue700 }}>
          Setting up automatic uploads in the background
        </div>
        <div style={{ marginTop: 5, fontSize: 12.5, lineHeight: 1.5, color: colors.gray700 }}>
          Your manual share is complete.{progress} You can close this page; the local
          ClawJournal daemon will continue and resume after a restart if needed.
        </div>
      </div>
    );
  }

  if (status.mode !== 'off' || !status.offer_available) return null;

  return (
    <AuthorizationDialog
      inline
      open
      initialStatus={status}
      onClose={dismiss}
      onEnabled={next => { setStatus(next); dismiss(); }}
    />
  );
}

export function AutoUploadPanel() {
  const { toast } = useToast();
  const [status, setStatus] = useState<AutoUploadStatus | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [disableOpen, setDisableOpen] = useState(false);
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [preview, setPreview] = useState<AutoUploadCandidateReport | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const busyRef = useRef(false);
  const statusGenerationRef = useRef(0);
  const statusRequestRef = useRef(0);

  const commitActionStatus = useCallback((next: AutoUploadStatus) => {
    statusGenerationRef.current += 1;
    statusRequestRef.current += 1;
    setStatus(next);
    setLoadError(null);
  }, []);

  const loadStatus = useCallback(async (quiet = false) => {
    if (busyRef.current) return;
    const generation = statusGenerationRef.current;
    const requestId = statusRequestRef.current + 1;
    statusRequestRef.current = requestId;
    try {
      const next = await api.autoUpload.status();
      if (busyRef.current
        || generation !== statusGenerationRef.current
        || requestId !== statusRequestRef.current) return;
      setStatus(next);
      setLoadError(null);
    } catch (error) {
      if (busyRef.current
        || generation !== statusGenerationRef.current
        || requestId !== statusRequestRef.current) return;
      if (!quiet) setLoadError(errorMessage(error, 'Could not load automatic-upload status.'));
    }
  }, []);

  useEffect(() => { void loadStatus(); }, [loadStatus]);

  useEffect(() => {
    if (!status || status.mode === 'off' && !status.overlay) return;
    const transient = status.overlay === 'running'
      || status.overlay === 'revocation_pending'
      || status.overlay === 'enrollment_pending'
      || status.health === 'retrying';
    const interval = window.setInterval(() => void loadStatus(true), transient ? 2_500 : 30_000);
    return () => window.clearInterval(interval);
  }, [loadStatus, status]);

  const perform = async (
    label: string,
    action: () => Promise<AutoUploadStatus>,
    successMessage: string,
  ) => {
    if (busyRef.current) return;
    busyRef.current = true;
    statusGenerationRef.current += 1;
    statusRequestRef.current += 1;
    setBusy(label);
    try {
      const next = await action();
      commitActionStatus(next);
      toast(successMessage, 'success');
    } catch (error) {
      toast(errorMessage(error, `${label} failed.`), 'error');
    } finally {
      busyRef.current = false;
      setBusy(null);
    }
  };

  const loadPreview = async (refresh = false) => {
    setPreviewLoading(true);
    try {
      setPreview(await api.autoUpload.preview({ refresh }));
    } catch (error) {
      toast(errorMessage(error, 'Could not load automatic-upload preview.'), 'error');
    } finally {
      setPreviewLoading(false);
    }
  };

  const panelStyle: CSSProperties = {
    background: colors.white,
    border: `1px solid ${colors.gray200}`,
    borderRadius: 10,
    padding: '16px 20px',
    marginBottom: 14,
  };

  if (!status && !loadError) {
    return <div style={panelStyle}><span style={{ color: colors.gray500, fontSize: 13 }}>Loading automatic-upload status…</span></div>;
  }
  if (!status) {
    return (
      <div style={panelStyle}>
        <h3 style={panelTitleStyle}>Automatic uploads</h3>
        <p style={{ ...panelHelpStyle, color: colors.red700 }}>{loadError}</p>
        <button onClick={() => void loadStatus()} style={btnSecondary}>Retry</button>
      </div>
    );
  }

  // The rollout gate must stay below the error branch: an enrolled user whose
  // status fetch failed still needs the Retry path to reach Pause/Turn off.
  if (!status.ui_visible) return null;

  const exclusionEntries = Object.entries(status.eligibility.exclusion_counts)
    .filter(([, count]) => count > 0)
    .sort((left, right) => right[1] - left[1]);
  const reviewCount = exclusionEntries
    .filter(([reason]) => reviewReasons.has(reason))
    .reduce((total, [, count]) => total + count, 0);
  const running = status.overlay === 'running';
  const mutating = busy !== null;
  const canEnable = status.mode === 'off' && status.offer_available && !status.overlay && !mutating;
  const runDisabled = mutating || running || !status.run_now_allowed;
  const setupPending = status.mode === 'off' && status.overlay === 'enrollment_pending';
  const toggleOn = status.mode !== 'off' || setupPending;
  const toggleDisabled = mutating
    || status.overlay === 'revocation_pending'
    || (!toggleOn && !canEnable);
  const setupProgress = status.enrollment_setup?.source
    && status.enrollment_setup.position != null
    && status.enrollment_setup.total != null
    ? ` Refreshing ${status.enrollment_setup.source} logs: ${status.enrollment_setup.position}/${status.enrollment_setup.total}.`
    : '';
  const exactScopeText = status.scope.entries
    .map(([source, project]) => `${sourceLabel(source)} → ${project}`)
    .join('\n');

  const toggleAutomaticUploads = () => {
    if (toggleDisabled) return;
    if (toggleOn) {
      setDisableOpen(true);
      return;
    }
    setDialogOpen(true);
  };

  return (
    <div style={panelStyle}>
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: 16, alignItems: 'center' }}>
        <div>
          <h3 style={panelTitleStyle}>Automatic uploads</h3>
          <p style={{ ...panelHelpStyle, marginBottom: 4 }}>
            Automatically share up to {status.cap} eligible future traces every {status.cadence_days} day{status.cadence_days === 1 ? '' : 's'}.
          </p>
          <button
            type="button"
            aria-expanded={detailsOpen}
            onClick={() => setDetailsOpen(open => !open)}
            style={{
              ...btnGhost,
              padding: 0,
              minHeight: 0,
              border: 'none',
              textDecoration: 'underline',
              fontSize: 12,
            }}
          >
            {detailsOpen ? 'Hide details' : 'View details'}
          </button>
        </div>
        <button
          type="button"
          role="switch"
          aria-label="Automatic uploads"
          aria-checked={toggleOn}
          disabled={toggleDisabled}
          onClick={toggleAutomaticUploads}
          title={!toggleOn && !canEnable
            ? 'Complete one successful hosted manual share before enabling automatic uploads.'
            : undefined}
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            gap: 8,
            flexShrink: 0,
            minWidth: 82,
            padding: '5px 9px 5px 5px',
            borderRadius: 999,
            border: `1px solid ${toggleOn ? colors.green200 : colors.gray300}`,
            background: toggleOn ? colors.green50 : colors.gray100,
            color: toggleOn ? colors.green700 : colors.gray700,
            cursor: toggleDisabled ? 'not-allowed' : 'pointer',
            opacity: toggleDisabled ? 0.62 : 1,
            fontSize: 12,
            fontWeight: 650,
          }}
        >
          <span
            aria-hidden="true"
            style={{
              width: 22,
              height: 22,
              borderRadius: '50%',
              background: toggleOn ? colors.green500 : colors.gray400,
              boxShadow: '0 1px 2px rgba(0, 0, 0, 0.16)',
            }}
          />
          {setupPending ? 'Setting up' : status.mode === 'paused' ? 'Paused' : toggleOn ? 'On' : 'Off'}
        </button>
      </div>

      {setupPending && (
        <div role="status" style={{ marginTop: 10, fontSize: 12.5, color: colors.blue700 }}>
          Setting up in the background.{setupProgress}
        </div>
      )}
      {status.overlay === 'revocation_pending' && (
        <div role="alert" style={{ marginTop: 10, fontSize: 12.5, color: colors.red700 }}>
          Turn-off needs attention. Open details to retry hosted revocation.
        </div>
      )}
      {status.mode === 'off' && !status.offer_available && !status.overlay && (
        <div style={{ marginTop: 10, fontSize: 12.5, color: colors.gray500 }}>
          Complete one successful hosted manual share to enable this switch.
        </div>
      )}
      {loadError && !detailsOpen && (
        <div role="alert" style={{ marginTop: 10, fontSize: 12.5, color: colors.red700 }}>{loadError}</div>
      )}

      {detailsOpen && (
      <>
      <div style={{ marginTop: 14 }}><ModeAndHealth status={status} /></div>

      {status.mode === 'off'
        && status.overlay !== 'revocation_pending'
        && status.overlay !== 'enrollment_pending'
        && !status.offer_available && (
        <div style={noticeStyle}>
          Automatic upload is unavailable right now. It can be offered after a successful hosted manual share when the recurring-upload capability is open.
        </div>
      )}
      {status.mode === 'off' && status.overlay === 'revocation_pending' && (
        <div role="alert" style={noticeStyle}>
          Local upload authority is off, but hosted revocation did not complete. It will not retry
          automatically; retry revocation when the hosted service is reachable.
        </div>
      )}
      {status.mode === 'off' && status.overlay === 'enrollment_pending' && (
        <div style={{ ...noticeStyle, color: colors.blue700 }}>
          Automatic uploads are being set up in the background. You may close the
          workbench; the local daemon will continue and resume after a restart.
          {status.enrollment_setup?.source
            && status.enrollment_setup.position != null
            && status.enrollment_setup.total != null
            ? ` Refreshing ${status.enrollment_setup.source} logs: ${status.enrollment_setup.position}/${status.enrollment_setup.total}.`
            : ''}
        </div>
      )}
      {loadError && <div role="alert" style={{ ...noticeStyle, color: colors.red700 }}>{loadError}</div>}

      <div style={{ ...summaryGridStyle, marginTop: 14 }}>
        <SummaryItem label="Sources represented" value={compactList(status.scope.sources, status.mode === 'off' ? 'Set when enabled' : 'None')} />
        <SummaryItem label="Projects represented" value={compactList(status.scope.projects, status.mode === 'off' ? 'Set when enabled' : 'None')} />
        <SummaryItem label="Future-only cutoff" value={status.enrolled_at ? formatTimestamp(status.enrolled_at) : 'Begins when enabled'} />
        <SummaryItem label="Cycle" value={`Up to ${status.cap} traces every ${status.cadence_days} day${status.cadence_days === 1 ? '' : 's'}`} />
        <SummaryItem label="AI-assisted PII" value={status.ai.enabled ? `On · ${status.ai.backend ?? 'configured provider'}` : 'Off'} />
        <SummaryItem label="Authorization" value={status.authorization.version ?? 'Not accepted'} />
        <SummaryItem label="Next due" value={formatTimestamp(status.next_due_at)} />
        <SummaryItem label="Next retry" value={formatTimestamp(status.next_retry_at)} />
      </div>

      {status.mode !== 'off' && (
        <TermsBlock
          title={`Exact enrolled scope · ${status.scope.entries.length} source/project pair${status.scope.entries.length === 1 ? '' : 's'}`}
          text={exactScopeText || 'No exact source/project pairs are stored.'}
        />
      )}

      {status.mode !== 'off' && (
        <p style={{ margin: '12px 0', fontSize: 12.5, color: colors.gray600 }}>
          Scheduled work runs on the next supported Claude Code or Codex session after it is due.
          A missed week produces one capped cycle, not a backlog burst.
        </p>
      )}

      {status.hooks.length > 0 && (
        <div style={{ marginTop: 14 }}>
          <div style={subheadingStyle}>Agent hooks</div>
          <div style={{ display: 'grid', gap: 6 }}>
            {status.hooks.map(hook => (
              <div key={hook.agent} style={{
                display: 'flex', gap: 8, justifyContent: 'space-between', alignItems: 'baseline',
                padding: '7px 9px', borderRadius: 7, background: colors.gray50, fontSize: 12,
              }}>
                <span style={{ color: colors.gray800, fontWeight: 600 }}>
                  {hook.agent === 'claude' ? 'Claude Code' : hook.agent === 'codex' ? 'Codex' : hook.agent}
                </span>
                <span style={{ color: hook.configured ? colors.green700 : colors.red700 }}>
                  {hook.configured ? 'Configured' : hook.installed ? 'Needs reinstall' : 'Missing'}
                  {' · '}{hook.last_observed_at ? `last observed ${formatTimestamp(hook.last_observed_at)}` : 'not yet observed'}
                </span>
                {hook.diagnostic && <span style={{ color: colors.gray500 }}>{hook.diagnostic}</span>}
              </div>
            ))}
          </div>
        </div>
      )}

      <div style={{ marginTop: 14 }}>
        <div style={subheadingStyle}>Eligibility</div>
        <div style={{ fontSize: 12.5, color: colors.gray700 }}>
          {status.eligibility.selected_count} selected for the next cycle from {status.eligibility.eligible_count} eligible.
          {reviewCount > 0 && (
            <> <Link to="/share" style={{ color: colors.primary700 }}>Review {reviewCount} in Share</Link>.</>
          )}
        </div>
        {exclusionEntries.length > 0 && (
          <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginTop: 8 }}>
            {exclusionEntries.map(([reason, count]) => (
              <StatusChip key={reason}>{exclusionLabels[reason] ?? reason.replaceAll('_', ' ')}: {count}</StatusChip>
            ))}
          </div>
        )}
      </div>

      {status.last_result && (
        <div style={{ marginTop: 12, fontSize: 12, color: colors.gray500 }}>
          Last result: <strong style={{ color: colors.gray700 }}>{status.last_result.code.replaceAll('_', ' ')}</strong>
          {status.last_result.count != null ? ` · ${status.last_result.count} trace${status.last_result.count === 1 ? '' : 's'}` : ''}
          {status.last_result.receipt_reference ? ` · receipt ${status.last_result.receipt_reference}` : ''}
        </div>
      )}

      {preview && (
        <div style={{ marginTop: 14, padding: '10px 12px', borderRadius: 8, background: colors.blue50, border: `1px solid ${colors.blue100}` }}>
          <div style={{ fontSize: 12.5, fontWeight: 650, color: colors.blue700 }}>
            Preview: {preview.selected_count} selected of {preview.eligible_count} eligible
          </div>
          {preview.deferred_by_cap > 0 && (
            <div style={{ marginTop: 3, fontSize: 12, color: colors.gray600 }}>
              {preview.deferred_by_cap} eligible trace{preview.deferred_by_cap === 1 ? '' : 's'} deferred by the {preview.limit}-trace cap.
            </div>
          )}
          {preview.scope_blockers.length > 0 && (
            <div style={{ marginTop: 3, fontSize: 12, color: colors.red700 }}>
              Scope blockers: {preview.scope_blockers.map(item => item.replaceAll('_', ' ')).join(', ')}.
            </div>
          )}
          <button
            disabled={previewLoading || running}
            onClick={() => void loadPreview(true)}
            style={{ ...btnGhost, marginTop: 7, ...disabledStyle(previewLoading || running) }}
          >
            {previewLoading ? 'Refreshing…' : 'Refresh local index and preview'}
          </button>
        </div>
      )}

      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginTop: 16 }}>
        {status.mode === 'off' ? (
          status.overlay === 'revocation_pending' ? (
            <button
              disabled={mutating}
              onClick={() => void perform(
                'Retry revocation',
                api.autoUpload.disable,
                'Recurring authorization revocation retried',
              )}
              style={{ ...btnDanger, ...disabledStyle(mutating) }}
            >
              {busy === 'Retry revocation' ? 'Retrying revocation…' : 'Retry revocation'}
            </button>
          ) : status.overlay === 'enrollment_pending' ? (
            <button
              disabled={mutating}
              onClick={() => void perform(
                'Cancel setup',
                api.autoUpload.disable,
                'Automatic-upload setup cancelled',
              )}
              style={{ ...btnDanger, ...disabledStyle(mutating) }}
            >
              {busy === 'Cancel setup' ? 'Cancelling…' : 'Cancel setup'}
            </button>
          ) : (
            <button
              disabled={!canEnable}
              onClick={() => setDialogOpen(true)}
              style={{ ...btnPrimary, ...disabledStyle(!canEnable) }}
            >
              Review and enable
            </button>
          )
        ) : (
          <>
            <button
              disabled={mutating || running || previewLoading}
              onClick={() => void loadPreview(false)}
              style={{ ...btnSecondary, ...disabledStyle(mutating || running || previewLoading) }}
            >
              {previewLoading ? 'Loading preview…' : 'Preview'}
            </button>
            <button
              disabled={runDisabled}
              onClick={() => void perform('Run now', api.autoUpload.run, 'Automatic-upload cycle started')}
              title={status.mode === 'paused'
                ? 'Resume automatic uploads before running now'
                : status.health === 'action_required' && !status.run_now_allowed
                  ? 'Review the required automatic-upload action before running now'
                  : undefined}
              style={{ ...btnPrimary, ...disabledStyle(runDisabled) }}
            >
              {busy === 'Run now' ? 'Starting…' : running ? 'Running…' : 'Run now'}
            </button>
            <button
              disabled={mutating || running}
              onClick={() => setDialogOpen(true)}
              style={{ ...btnSecondary, ...disabledStyle(mutating || running) }}
            >
              Review scope and terms
            </button>
            {status.mode === 'paused' ? (
              <button
                disabled={mutating}
                onClick={() => void perform('Resume', api.autoUpload.resume, 'Automatic upload resumed')}
                style={{ ...btnSecondary, ...disabledStyle(mutating) }}
              >
                {busy === 'Resume' ? 'Resuming…' : 'Resume'}
              </button>
            ) : (
              <button
                disabled={mutating}
                onClick={() => void perform('Pause', api.autoUpload.pause, 'Automatic upload paused')}
                style={{ ...btnSecondary, ...disabledStyle(mutating) }}
              >
                {busy === 'Pause' ? 'Pausing…' : 'Pause'}
              </button>
            )}
            <button
              disabled={mutating}
              onClick={() => setDisableOpen(true)}
              style={{ ...btnDanger, ...disabledStyle(mutating) }}
            >
              Turn off
            </button>
          </>
        )}
        <button disabled={mutating} onClick={() => void loadStatus()} style={{ ...btnGhost, marginLeft: 4 }}>
          Refresh status
        </button>
      </div>
      </>
      )}

      <AuthorizationDialog
        open={dialogOpen}
        initialStatus={status}
        onClose={() => setDialogOpen(false)}
        onEnabled={commitActionStatus}
      />
      <ConfirmDialog
        open={disableOpen}
        title="Turn off automatic uploads?"
        message="This takes effect locally first, removes the selected agent hooks, and removes active upload authority. Prior uploads are not deleted, and a request already being submitted may still finish."
        confirmLabel="Turn off"
        variant="danger"
        onCancel={() => setDisableOpen(false)}
        onConfirm={() => {
          setDisableOpen(false);
          void perform('Turn off', api.autoUpload.disable, 'Automatic upload turned off');
        }}
      />
    </div>
  );
}

const summaryGridStyle: CSSProperties = {
  display: 'grid',
  gridTemplateColumns: 'repeat(auto-fit, minmax(190px, 1fr))',
  gap: '12px 18px',
  padding: '12px 14px',
  border: `1px solid ${colors.gray200}`,
  borderRadius: 8,
  background: colors.gray50,
};

const noticeStyle: CSSProperties = {
  margin: '10px 0',
  padding: '9px 11px',
  border: `1px solid ${colors.yellow200}`,
  borderRadius: 7,
  background: colors.yellow50,
  color: colors.yellow700,
  fontSize: 12.5,
  lineHeight: 1.45,
};

const panelTitleStyle: CSSProperties = {
  margin: '0 0 4px', fontSize: 14, fontWeight: 600, color: colors.gray800,
};

const panelHelpStyle: CSSProperties = {
  margin: 0, fontSize: 12.5, color: colors.gray500, lineHeight: 1.5,
};

const subheadingStyle: CSSProperties = {
  marginBottom: 6, fontSize: 11, fontWeight: 700, letterSpacing: '0.05em',
  textTransform: 'uppercase', color: colors.gray400,
};
