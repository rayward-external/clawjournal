import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api, ApiError } from '../../api.ts';
import { colors } from '../../theme.ts';
import { Spinner } from '../../components/Spinner.tsx';
import { challengeFromError } from '../../components/autoUploadChallenge.ts';
import type {
  AutoUploadAgent,
  AutoUploadAuthorizationChallenge,
  AutoUploadStatus,
} from '../../types.ts';
import type { HostedConsent, ShareDestination } from './types.ts';
import { SHARE_SHELL_WIDTH, btnPrimary, btnSecondary } from './styles.tsx';
import { CheckboxRow, Icon } from './shared.tsx';
import { cancelSuccessChime, playSuccessChime, primeSuccessChime } from './successChime.ts';

export interface SubmitStepProps {
  stepperHeader: React.ReactNode;
  shareId: string | null;
  bundle: { traces: number; created: string; approxSize: string } | null;
  shareDestination: ShareDestination | null;
  aiPiiEnabled: boolean;
  onSubmitted: (receiptId: string, status?: string | null, supportContact?: string | null) => void;
  onDownloadZip: () => void;
  globalStyles: React.ReactNode;
  toast: (message: string, type?: 'success' | 'error' | 'info') => void;
}

type UploadStatus = {
  verified_email: string | null;
  token_valid: boolean;
  expires_at: string | number | null;
  pending_email: string | null;
};

function automaticUploadAgent(
  challenge: AutoUploadAuthorizationChallenge,
): AutoUploadAgent {
  const sources = new Set(
    challenge.scope.sources.filter(source => (
      source === 'claude' || source === 'codex'
    )),
  );
  if (sources.size === 1) {
    return sources.has('codex') ? 'codex' : 'claude';
  }
  return 'all';
}

export function SubmitStep(p: SubmitStepProps) {
  const [consent, setConsent] = useState<HostedConsent | null>(null);
  const [loadingConsent, setLoadingConsent] = useState(true);
  const [tokenValid, setTokenValid] = useState(false);
  const [verifiedEmail, setVerifiedEmail] = useState<string | null>(null);
  const [pendingEmail, setPendingEmail] = useState<string | null>(null);
  const [email, setEmail] = useState('');
  const [code, setCode] = useState('');
  const [devCode, setDevCode] = useState<string | null>(null);
  const [acceptTerms, setAcceptTerms] = useState(false);
  const [ownership, setOwnership] = useState(false);
  const [autoUploadStatus, setAutoUploadStatus] =
    useState<AutoUploadStatus | null>(null);
  const [autoUploadChallenge, setAutoUploadChallenge] =
    useState<AutoUploadAuthorizationChallenge | null>(null);
  // The final submit screen defaults to the streamlined combined action once
  // the exact recurring challenge is available.  The local enrollment itself
  // remains off until Submit succeeds, and the participant can uncheck this.
  const [enableAutomaticUploads, setEnableAutomaticUploads] = useState(true);
  const [automaticUploadOptionLoading, setAutomaticUploadOptionLoading] = useState(true);
  const automaticUploadChoiceRef = useRef(true);
  const [showAutomaticUploadDetails, setShowAutomaticUploadDetails] = useState(false);
  const [showAcceptedDomains, setShowAcceptedDomains] = useState(false);
  const [busy, setBusy] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [submitStageIndex, setSubmitStageIndex] = useState(0);
  const [submitProgress, setSubmitProgress] = useState(0);
  const [error, setError] = useState<string | null>(null);

  const updateAutomaticUploadChoice = useCallback((enabled: boolean) => {
    automaticUploadChoiceRef.current = enabled;
    setEnableAutomaticUploads(enabled);
  }, []);

  const submitStages = useMemo(() => [
    {
      buttonLabel: 'Checking...',
      detail: 'Refreshing consent and email verification.',
      delayMs: 0,
      progress: 8,
    },
    {
      buttonLabel: p.aiPiiEnabled ? 'Reviewing PII...' : 'Applying rules...',
      detail: p.aiPiiEnabled
        ? 'AI PII review is running before upload.'
        : 'Rules-only redaction is running before upload.',
      delayMs: 900,
      progress: 28,
    },
    {
      buttonLabel: 'Rebuilding bundle...',
      detail: 'Re-exporting the redacted bundle.',
      delayMs: 7000,
      progress: 50,
    },
    {
      buttonLabel: 'Secret scan...',
      detail: 'Running the final secret scan.',
      delayMs: 14000,
      progress: 70,
    },
    {
      buttonLabel: 'Uploading...',
      detail: 'Uploading the finalized zip.',
      delayMs: 22000,
      progress: 86,
    },
  ], [p.aiPiiEnabled]);

  const applySubmitState = useCallback((consentData: HostedConsent, status: UploadStatus) => {
    setConsent(consentData);
    setTokenValid(!!status.token_valid);
    setVerifiedEmail(status.verified_email);
    setPendingEmail(status.pending_email);
    setEmail(status.pending_email || status.verified_email || '');
  }, []);

  const refreshSubmitState = useCallback(async () => {
    const [consentData, status] = await Promise.all([
      api.share.consent(),
      api.share.uploadStatus(),
    ]);
    applySubmitState(consentData, status);
    return { consentData, status };
  }, [applySubmitState]);

  const loadSubmitState = useCallback(async () => {
    setLoadingConsent(true);
    try {
      await refreshSubmitState();
      setError(null);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Could not load hosted submission state');
    } finally {
      setLoadingConsent(false);
    }
  }, [refreshSubmitState]);

  useEffect(() => {
    void loadSubmitState();
  }, [loadSubmitState]);

  const loadAutomaticUploadOption = useCallback(async () => {
    setAutomaticUploadOptionLoading(true);
    try {
      const status = await api.autoUpload.status();
      setAutoUploadStatus(status);
      if (status.mode !== 'off') {
        setAutoUploadChallenge(null);
        updateAutomaticUploadChoice(true);
        setOwnership(false);
        return;
      }
      try {
        await api.autoUpload.enable({
          agent: 'all',
          challenge_only: true,
          prepare_for_manual_share: true,
        });
        setAutoUploadChallenge(null);
        updateAutomaticUploadChoice(false);
        setOwnership(false);
      } catch (challengeError) {
        const preparedChallenge = challengeFromError(challengeError);
        setAutoUploadChallenge(preparedChallenge);
        if (preparedChallenge) {
          if (automaticUploadChoiceRef.current) setOwnership(false);
        } else {
          updateAutomaticUploadChoice(false);
          setOwnership(false);
        }
      }
    } catch {
      // Automatic upload is optional.  Capability, scope, or network
      // failures must never block the manual share the participant reviewed.
      setAutoUploadStatus(null);
      setAutoUploadChallenge(null);
      updateAutomaticUploadChoice(false);
      setOwnership(false);
    } finally {
      setAutomaticUploadOptionLoading(false);
    }
  }, [updateAutomaticUploadChoice]);

  useEffect(() => {
    void loadAutomaticUploadOption();
  }, [loadAutomaticUploadOption]);

  useEffect(() => {
    if (!tokenValid || busy) return;
    const refreshIfVisible = () => {
      if (document.visibilityState === 'visible') void loadSubmitState();
    };
    window.addEventListener('focus', refreshIfVisible);
    document.addEventListener('visibilitychange', refreshIfVisible);
    return () => {
      window.removeEventListener('focus', refreshIfVisible);
      document.removeEventListener('visibilitychange', refreshIfVisible);
    };
  }, [busy, loadSubmitState, tokenValid]);

  useEffect(() => {
    if (!submitting) {
      setSubmitStageIndex(0);
      setSubmitProgress(0);
      return;
    }

    setSubmitStageIndex(0);
    setSubmitProgress(submitStages[0]?.progress ?? 8);
    const timers = submitStages.slice(1).map((stage, index) => (
      window.setTimeout(() => {
        setSubmitStageIndex(index + 1);
        setSubmitProgress((prev) => Math.max(prev, stage.progress));
      }, stage.delayMs)
    ));
    const tick = window.setInterval(() => {
      setSubmitProgress((prev) => Math.min(92, prev + (prev < 64 ? 3 : 1)));
    }, 700);

    return () => {
      timers.forEach((timer) => window.clearTimeout(timer));
      window.clearInterval(tick);
    };
  }, [submitting, submitStages]);

  const sendCode = async () => {
    if (!email.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const result = await api.share.verifyEmail(email.trim());
      setPendingEmail(result.email);
      setDevCode(result.dev_code || null);
      p.toast('Verification code sent', 'success');
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Could not send verification code');
    } finally {
      setBusy(false);
    }
  };

  const verifyCode = async () => {
    if (!code.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const result = await api.share.verifyConfirm(code.trim());
      setTokenValid(true);
      setVerifiedEmail(result.verified_email);
      setPendingEmail(null);
      setDevCode(null);
      p.toast('Email verified', 'success');
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Could not verify code');
    } finally {
      setBusy(false);
    }
  };

  const submit = async () => {
    if (!p.shareId) return;
    let submitted = false;
    primeSuccessChime();
    setBusy(true);
    setSubmitting(true);
    setError(null);
    try {
      const { consentData, status } = await refreshSubmitState();
      if (!status.token_valid) {
        setError('Verification expired. Send a new code to continue.');
        return;
      }
      const result = await api.shares.upload(p.shareId, {
        accept_terms: acceptTerms,
        ownership_certification: ownership,
        consent_version: consentData.consent_version,
        retention_policy_version: consentData.retention_policy_version,
        ai_pii: p.aiPiiEnabled,
        automatic_upload: enableAutomaticUploads && autoUploadChallenge
          ? {
              agent: automaticUploadAgent(autoUploadChallenge),
              accepted_authorization_version:
                autoUploadChallenge.authorization.version,
              accepted_retention_version:
                autoUploadChallenge.retention.version,
              accepted_ownership_certification_version:
                autoUploadChallenge.ownership_certification.version,
              accepted_authorization_profile_hash:
                autoUploadChallenge.authorization_profile_hash,
            }
          : undefined,
      });
      submitted = true;
      playSuccessChime();
      if (enableAutomaticUploads && autoUploadChallenge) {
        if (result.automatic_upload?.queued
          || result.automatic_upload?.mode === 'enabled'
          || result.automatic_upload?.mode === 'paused') {
          p.toast(
            result.automatic_upload?.queued
              ? 'Submitted. Automatic upload setup continues in the background.'
              : 'Submitted and automatic uploads enabled',
            'success',
          );
        } else {
          p.toast(
            'Submitted. Automatic upload still needs review on the receipt page.',
            'info',
          );
        }
      } else if (!enableAutomaticUploads || !autoUploadChallenge) {
        p.toast('Submitted', 'success');
      }
      p.onSubmitted(result.receipt_id, result.hosted_status || null, consentData.support_contact || p.shareDestination?.support_contact || null);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Submission failed';
      setError(msg);
      // 401/403 from the daemon means it cleared the upload token (the
      // hosted service rejected it). Re-poll status so the verify
      // sub-flow comes back; otherwise the user sees "verified" but
      // can't submit. 400 with consent/version wording means stale
      // terms — clear checkboxes and reload to show the new version.
      const status = err instanceof ApiError ? err.status : null;
      if (status === 401 || status === 403) {
        void loadSubmitState();
      } else if (/consent|retention|version|terms/i.test(msg)) {
        setAcceptTerms(false);
        setOwnership(false);
        void loadSubmitState();
      }
    } finally {
      if (!submitted) cancelSuccessChime();
      setBusy(false);
      setSubmitting(false);
    }
  };

  const acceptedDomains = (
    p.shareDestination?.supported_institution_email_policy?.domain_suffixes ?? []
  ).filter((d): d is string => typeof d === 'string' && d.length > 0);
  const supportsExplicitCollaborators =
    p.shareDestination?.supported_institution_email_policy
      ?.explicit_collaborators_supported === true;

  const automaticUploadsAlreadyConfigured =
    autoUploadStatus?.mode === 'enabled' || autoUploadStatus?.mode === 'paused';
  const willEnableAutomaticUploads =
    enableAutomaticUploads
    && (automaticUploadOptionLoading || autoUploadChallenge !== null);
  const automaticUploadCap =
    autoUploadChallenge?.cap ?? autoUploadStatus?.cap ?? 5;
  const automaticUploadCadence =
    autoUploadChallenge?.cadence_days ?? autoUploadStatus?.cadence_days ?? 1;
  const automaticUploadScopeEntries =
    autoUploadChallenge?.scope.entries ?? autoUploadStatus?.scope.entries ?? [];
  const disabled = busy || loadingConsent || !p.shareId || !tokenValid
    || !acceptTerms || !ownership || !consent
    || (enableAutomaticUploads && automaticUploadOptionLoading);
  const supportContact = consent?.support_contact || p.shareDestination?.support_contact || null;
  const currentSubmitStage = submitStages[submitStageIndex] ?? submitStages[0];
  const submitPipelineLabel = p.aiPiiEnabled
    ? 'AI PII review -> redaction -> secret scan -> upload'
    : 'redaction -> secret scan -> upload';

  return (
    <div style={{ padding: '32px 24px 48px', maxWidth: SHARE_SHELL_WIDTH, margin: '0 auto' }}>
      {p.globalStyles}
      {p.stepperHeader}
      <div style={{ maxWidth: 760, margin: '0 auto', padding: '32px 0 0' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', gap: 16, alignItems: 'flex-start', marginBottom: 18, flexWrap: 'wrap' }}>
          <div>
            <h2 style={{ fontSize: 20, fontWeight: 500, margin: '0 0 6px', color: colors.gray900 }}>
              Submit to ClawJournal Research
            </h2>
            <div style={{ color: colors.gray500, fontSize: 13 }}>
              {p.bundle ? `${p.bundle.traces} trace${p.bundle.traces === 1 ? '' : 's'} · ${p.bundle.approxSize}` : 'Finalized bundle'}
            </div>
          </div>
          <button onClick={p.onDownloadZip} style={btnSecondary}>
            <Icon name="download" size={14} /> Download zip instead
          </button>
        </div>

        {error && (
          <div style={{
            marginBottom: 14, padding: '10px 12px',
            background: colors.red50, border: `1px solid ${colors.red200}`,
            color: colors.red500, borderRadius: 8, fontSize: 13,
          }}>
            {error}
          </div>
        )}

        <div style={{
          display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(min(100%, 280px), 1fr))',
          gap: 18, alignItems: 'start',
        }}>
          <div style={{ background: colors.white, border: `1px solid ${colors.gray200}`, borderRadius: 8, overflow: 'hidden' }}>
            <div style={{ padding: '12px 14px', borderBottom: `1px solid ${colors.gray200}`, display: 'flex', alignItems: 'center', gap: 8 }}>
              <Icon name="shield" size={15} />
              <span style={{ fontSize: 13, fontWeight: 600, color: colors.gray900 }}>Consent and retention</span>
            </div>
            <div style={{ maxHeight: 320, overflow: 'auto', padding: 14, color: colors.gray700, fontSize: 13.5, lineHeight: 1.6 }}>
              {loadingConsent ? (
                <Spinner text="Loading terms..." />
              ) : consent ? (
                <>
                  <p style={{ margin: '0 0 12px' }}>{consent.consent_text}</p>
                  <p style={{ margin: 0 }}>{consent.retention_text}</p>
                  <div style={{ marginTop: 14, display: 'flex', gap: 8, flexWrap: 'wrap', color: colors.gray500, fontSize: 11.5, fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace' }}>
                    <span>{consent.consent_version}</span>
                    <span>{consent.retention_policy_version}</span>
                  </div>
                </>
              ) : (
                <span>Terms are unavailable.</span>
              )}
            </div>
          </div>

          <div style={{ display: 'grid', gap: 12 }}>
            <div style={{ background: colors.white, border: `1px solid ${colors.gray200}`, borderRadius: 8, padding: 14 }}>
              <div style={{ fontSize: 12, fontWeight: 600, color: colors.gray900, marginBottom: 10 }}>Verified email</div>
              {tokenValid ? (
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, color: colors.green500, fontSize: 13 }}>
                  <Icon name="check" size={14} /> {verifiedEmail}
                </div>
              ) : (
                <div style={{ display: 'grid', gap: 8 }}>
                  <input
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    placeholder="name@university.edu"
                    style={{ padding: '9px 10px', border: `1px solid ${colors.gray300}`, borderRadius: 8, fontSize: 13 }}
                  />
                  {acceptedDomains.length > 0 && (
                    <div style={{ fontSize: 11.5, color: colors.gray500, lineHeight: 1.4 }}>
                      {supportsExplicitCollaborators
                        ? 'Academic and approved collaborator emails are supported.'
                        : 'Academic email addresses are supported.'}{' '}
                      <button
                        type="button"
                        aria-expanded={showAcceptedDomains}
                        aria-controls="accepted-email-domains"
                        onClick={() => setShowAcceptedDomains(value => !value)}
                        style={{
                          padding: 0,
                          border: 0,
                          background: 'transparent',
                          color: colors.primary500,
                          font: 'inherit',
                          textDecoration: 'underline',
                          cursor: 'pointer',
                        }}
                      >
                        {showAcceptedDomains ? 'Hide accepted domains' : 'View accepted domains'}
                      </button>
                      {showAcceptedDomains && (
                        <div id="accepted-email-domains" style={{ marginTop: 4 }}>
                          Accepted domains: {acceptedDomains.join(', ')}
                        </div>
                      )}
                    </div>
                  )}
                  <button onClick={sendCode} disabled={busy || !email.trim()} style={{ ...btnPrimary, justifyContent: 'center', opacity: busy || !email.trim() ? 0.5 : 1 }}>
                    Send code
                  </button>
                  {(pendingEmail || devCode) && (
                    <>
                      <input
                        value={code}
                        onChange={(e) => setCode(e.target.value)}
                        placeholder="Verification code"
                        style={{ padding: '9px 10px', border: `1px solid ${colors.gray300}`, borderRadius: 8, fontSize: 13 }}
                      />
                      {devCode && (
                        <div style={{ fontSize: 12, color: colors.gray500, fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace' }}>
                          <span style={{ fontFamily: 'Inter, system-ui' }}>Dev code: </span>{devCode}
                        </div>
                      )}
                      <button onClick={verifyCode} disabled={busy || !code.trim()} style={{ ...btnSecondary, justifyContent: 'center', opacity: busy || !code.trim() ? 0.5 : 1 }}>
                        Verify
                      </button>
                    </>
                  )}
                </div>
              )}
            </div>

            <div style={{ background: colors.white, border: `1px solid ${colors.gray200}`, borderRadius: 8, padding: 14, display: 'grid', gap: 10 }}>
              <CheckboxRow checked={acceptTerms} onChange={setAcceptTerms}>
                I accept the displayed consent and data-use terms.
              </CheckboxRow>
              <CheckboxRow checked={ownership} onChange={setOwnership}>
                {willEnableAutomaticUploads
                  ? 'I certify this bundle and future automatically uploaded bundles are mine to submit and contain no third-party confidential material.'
                  : 'I certify this bundle is mine to submit and contains no third-party confidential material.'}
              </CheckboxRow>
              {(automaticUploadOptionLoading || autoUploadChallenge || automaticUploadsAlreadyConfigured) && (
                <div style={{
                  paddingTop: 10,
                  borderTop: `1px solid ${colors.gray200}`,
                  display: 'grid',
                  gap: 8,
                }}>
                  <div style={{
                    display: 'flex',
                    alignItems: 'flex-start',
                    gap: 9,
                    fontSize: 12.5,
                    color: colors.gray700,
                    lineHeight: 1.4,
                  }}>
                    <input
                      id="enable-automatic-uploads-after-share"
                      type="checkbox"
                      aria-label={automaticUploadOptionLoading
                        ? 'Enable automatic uploads after this share (loading details)'
                        : 'Enable automatic uploads after this share'}
                      checked={enableAutomaticUploads}
                      disabled={automaticUploadsAlreadyConfigured || busy}
                      onChange={event => {
                        if (automaticUploadsAlreadyConfigured || busy) return;
                        const checked = event.target.checked;
                        updateAutomaticUploadChoice(checked);
                        if (checked) {
                          // The recurring ownership text covers future
                          // bundles, so a check made against the manual-only
                          // wording cannot be reused as that distinct act.
                          setOwnership(false);
                        }
                      }}
                      style={{
                        marginTop: 2,
                        width: 15,
                        height: 15,
                        accentColor: colors.gray900,
                        flexShrink: 0,
                        cursor: automaticUploadsAlreadyConfigured || busy
                          ? 'not-allowed'
                          : 'pointer',
                      }}
                    />
                    <span>
                      <label
                        htmlFor="enable-automatic-uploads-after-share"
                        style={{
                          cursor: automaticUploadsAlreadyConfigured || busy
                            ? 'not-allowed'
                            : 'pointer',
                        }}
                      >
                        {automaticUploadsAlreadyConfigured
                          ? `Automatic uploads are already ${
                              autoUploadStatus?.mode === 'paused'
                                ? 'configured (currently paused)'
                                : 'enabled'
                            }: up to ${automaticUploadCap} trace uploads every ${
                              automaticUploadCadence
                            } day${automaticUploadCadence === 1 ? '' : 's'}.`
                          : automaticUploadOptionLoading
                            ? `After this share succeeds, enable up to ${
                                automaticUploadCap
                              } automatic trace uploads every ${
                                automaticUploadCadence
                              } day${automaticUploadCadence === 1 ? '' : 's'}. Loading exact scope and terms…`
                          : `After this share succeeds, enable up to ${
                              automaticUploadCap
                            } automatic trace uploads every ${
                              automaticUploadCadence
                            } day${automaticUploadCadence === 1 ? '' : 's'} for this exact scope.`}
                      </label>{' '}
                      {automaticUploadOptionLoading ? (
                        <span style={{ color: colors.gray500 }}>Loading details…</span>
                      ) : (
                        <button
                          type="button"
                          aria-expanded={showAutomaticUploadDetails}
                          onClick={() => setShowAutomaticUploadDetails(value => !value)}
                          style={{
                            padding: 0,
                            border: 0,
                            background: 'transparent',
                            color: colors.primary500,
                            font: 'inherit',
                            textDecoration: 'underline',
                            cursor: 'pointer',
                          }}
                        >
                          {showAutomaticUploadDetails ? 'Hide details' : 'View details'}
                        </button>
                      )}
                    </span>
                  </div>
                  {showAutomaticUploadDetails && !automaticUploadOptionLoading && (
                    <div style={{
                      maxHeight: 190,
                      overflow: 'auto',
                      padding: '10px 12px',
                      border: `1px solid ${colors.gray200}`,
                      borderRadius: 8,
                      background: colors.gray50,
                      color: colors.gray600,
                      fontSize: 11.5,
                      lineHeight: 1.5,
                      whiteSpace: 'pre-wrap',
                    }}>
                      {autoUploadChallenge ? (
                        <>
                          <strong style={{ color: colors.gray800 }}>
                            Recurring authorization · {autoUploadChallenge.authorization.version}
                          </strong>
                          {'\n'}{autoUploadChallenge.authorization.text}
                          {'\n\n'}
                          <strong style={{ color: colors.gray800 }}>
                            Retention · {autoUploadChallenge.retention.version}
                          </strong>
                          {'\n'}{autoUploadChallenge.retention.text}
                          {'\n\n'}
                          <strong style={{ color: colors.gray800 }}>
                            Ownership · {autoUploadChallenge.ownership_certification.version}
                          </strong>
                          {'\n'}{autoUploadChallenge.ownership_certification.text}
                          {'\n\n'}
                        </>
                      ) : (
                        <>
                          <strong style={{ color: colors.gray800 }}>
                            Existing recurring enrollment
                          </strong>
                          {'\n'}
                          This manual share does not change the automatic-upload authorization
                          already stored on this device.
                          {'\n\n'}
                          <strong style={{ color: colors.gray800 }}>
                            Authorization · {autoUploadStatus?.authorization.version ?? 'accepted'}
                          </strong>
                          {'\n'}
                          <strong style={{ color: colors.gray800 }}>
                            Retention · {autoUploadStatus?.retention.version ?? 'accepted'}
                          </strong>
                          {'\n\n'}
                        </>
                      )}
                      <strong style={{ color: colors.gray800 }}>Exact scope</strong>
                      {'\n'}
                      {automaticUploadScopeEntries
                        .map(([source, project]) => `${source} → ${project}`)
                        .join('\n') || 'Stored enrollment scope'}
                    </div>
                  )}
                </div>
              )}
              <button
                onClick={submit}
                disabled={disabled}
                aria-busy={submitting}
                style={{
                  ...btnPrimary,
                  justifyContent: 'center',
                  opacity: disabled && !submitting ? 0.45 : 1,
                  cursor: submitting ? 'wait' : disabled ? 'not-allowed' : 'pointer',
                }}
              >
                {submitting ? (
                  <>
                    <span style={{
                      width: 14,
                      height: 14,
                      border: '2px solid rgba(255,255,255,0.35)',
                      borderTopColor: colors.white,
                      borderRadius: '50%',
                      animation: 'clawSpin 700ms linear infinite',
                      flexShrink: 0,
                    }} />
                    {currentSubmitStage.buttonLabel}
                  </>
                ) : (
                  <>
                    <Icon name="check" size={14} />
                    {willEnableAutomaticUploads
                      ? 'Submit and enable automatic uploads'
                      : 'Submit to ClawJournal Research'}
                  </>
                )}
              </button>
              {submitting && (
                <div
                  role="status"
                  aria-live="polite"
                  style={{
                    display: 'grid',
                    gap: 8,
                    padding: '10px 12px',
                    border: `1px solid ${colors.primary200}`,
                    background: colors.primary50,
                    borderRadius: 8,
                  }}
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12.5, color: colors.gray700 }}>
                    <span style={{
                      width: 8,
                      height: 8,
                      borderRadius: '50%',
                      background: colors.primary500,
                      boxShadow: `0 0 0 3px ${colors.primary100}`,
                      flexShrink: 0,
                    }} />
                    {currentSubmitStage.detail}
                  </div>
                  <div style={{ height: 4, background: colors.gray200, borderRadius: 2, overflow: 'hidden' }}>
                    <div style={{
                      width: `${submitProgress}%`,
                      height: '100%',
                      background: `linear-gradient(90deg, ${colors.primary500}, ${colors.green500})`,
                      transition: 'width 300ms ease',
                    }} />
                  </div>
                  <div style={{
                    fontSize: 11,
                    color: colors.gray500,
                    fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
                    overflowWrap: 'anywhere',
                  }}>
                    {submitPipelineLabel}
                  </div>
                </div>
              )}
              {supportContact && (
                <div style={{ fontSize: 11.5, color: colors.gray500, textAlign: 'center' }}>
                  Support: {supportContact}
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
