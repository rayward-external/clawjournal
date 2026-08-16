import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { ApiError, api } from '../api.ts';
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
import {
  SUPPORT_REPORT_STATE_PRESENTATION,
  SUPPORT_REPORT_CAPABILITY_TIMEOUT_MS,
  projectSupportReportList,
  projectSupportReportCapability,
  projectSupportReportStatus,
  reportUtf8Bytes,
} from '../supportReport.ts';
import type { SupportContext, SupportReportCapability, SupportReportStatus } from '../types.ts';
import {
  captureSupportScreenshot,
  markdownSha256,
} from '../supportScreenshot.ts';
import type { CapturedSupportScreenshot } from '../supportScreenshot.ts';
import { btnPrimary, btnSecondary, colors, fontFamily, inputStyle, labelStyle } from '../theme.ts';

interface BugReportDialogProps {
  open: boolean;
  onClose: () => void;
  surface: BugReportSurface;
  location?: BugReportLocation;
}

type DiagnosticsState = 'loading' | 'ready' | 'unavailable';
type CapabilityState = 'loading' | 'available' | 'unavailable';
type DialogStage = 'compose' | 'review';
type SupportOperation = 'idle' | 'submitting' | 'checking' | 'deleting';
type RecentReportsState = 'loading' | 'ready' | 'unavailable';
type ScreenshotOperation = 'idle' | 'capturing';

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
  const [capabilityState, setCapabilityState] = useState<CapabilityState>('loading');
  const [supportCapability, setSupportCapability] = useState<SupportReportCapability | null>(null);
  const [supportConsent, setSupportConsent] = useState(false);
  const [supportConsentBinding, setSupportConsentBinding] = useState<string | null>(null);
  const [capturedScreenshot, setCapturedScreenshot] = useState<CapturedSupportScreenshot | null>(null);
  const [screenshotPreviewUrl, setScreenshotPreviewUrl] = useState<string | null>(null);
  const [screenshotOperation, setScreenshotOperation] = useState<ScreenshotOperation>('idle');
  const [screenshotActionStatus, setScreenshotActionStatus] = useState('');
  const [supportReport, setSupportReport] = useState<SupportReportStatus | null>(null);
  const [supportOperation, setSupportOperation] = useState<SupportOperation>('idle');
  const [supportActionStatus, setSupportActionStatus] = useState('');
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [recentReports, setRecentReports] = useState<SupportReportStatus[]>([]);
  const [recentReportsState, setRecentReportsState] = useState<RecentReportsState>('loading');
  const [recentReportsTruncated, setRecentReportsTruncated] = useState(false);
  const [recentOperationId, setRecentOperationId] = useState<string | null>(null);
  const [recentConfirmDeleteId, setRecentConfirmDeleteId] = useState<string | null>(null);
  const [recentActionStatus, setRecentActionStatus] = useState('');
  const dialogRef = useRef<HTMLDivElement>(null);
  const summaryRef = useRef<HTMLInputElement>(null);
  const draftRef = useRef<HTMLTextAreaElement>(null);
  const supportStatusHeadingRef = useRef<HTMLHeadingElement>(null);
  const focusSubmittedStatusRef = useRef(false);
  const downloadUrlRef = useRef<string | null>(null);
  const downloadTimerRef = useRef<number | null>(null);
  const screenshotPreviewUrlRef = useRef<string | null>(null);
  const consentGenerationRef = useRef(0);
  const titleId = useId();
  const privacyId = useId();
  const diagnosticsId = useId();

  const clearSupportConsent = useCallback(() => {
    consentGenerationRef.current += 1;
    setSupportConsent(false);
    setSupportConsentBinding(null);
  }, []);

  const releaseScreenshotPreview = useCallback(() => {
    if (screenshotPreviewUrlRef.current !== null) {
      URL.revokeObjectURL(screenshotPreviewUrlRef.current);
      screenshotPreviewUrlRef.current = null;
    }
    setScreenshotPreviewUrl(null);
  }, []);

  const removeCapturedScreenshot = useCallback(() => {
    releaseScreenshotPreview();
    setCapturedScreenshot(null);
    setScreenshotActionStatus('Screenshot removed. Only the reviewed Markdown will be sent.');
    clearSupportConsent();
  }, [clearSupportConsent, releaseScreenshotPreview]);

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
      releaseScreenshotPreview();
      setCapturedScreenshot(null);
      setScreenshotActionStatus('');
      setScreenshotOperation('idle');
      return;
    }
    return () => {
      releaseDownloadUrl();
      releaseScreenshotPreview();
    };
  }, [open, releaseDownloadUrl, releaseScreenshotPreview]);

  useEffect(() => {
    if (!open) return;
    const previousFocus = document.activeElement as HTMLElement | null;
    return () => previousFocus?.focus?.();
  }, [open]);

  useEffect(() => {
    if (!open) {
      setRecentConfirmDeleteId(null);
      return;
    }
    const controller = new AbortController();
    let active = true;
    setRecentReportsState('loading');
    setRecentActionStatus('');
    const timeout = window.setTimeout(() => controller.abort(), BUG_REPORT_CONTEXT_TIMEOUT_MS);

    void api.support.list(controller.signal)
      .then(raw => {
        if (!active) return;
        const projected = projectSupportReportList(raw);
        if (!projected) throw new Error('invalid local support report list');
        setRecentReports(projected.reports);
        setRecentReportsTruncated(projected.truncated);
        setRecentReportsState('ready');
      })
      .catch(() => {
        if (!active) return;
        setRecentReports([]);
        setRecentReportsTruncated(false);
        setRecentReportsState('unavailable');
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
    if (stage === 'review') draftRef.current?.focus();
    else summaryRef.current?.focus();
  }, [open, stage]);

  useEffect(() => {
    if (!focusSubmittedStatusRef.current || !supportReport) return;
    focusSubmittedStatusRef.current = false;
    supportStatusHeadingRef.current?.focus();
  }, [supportReport]);

  useEffect(() => {
    // Consent is scoped to the exact terms fetched for this opening of the
    // dialog. Never carry a checked box across close/reopen: the service may
    // have published a new terms or retention version in between.
    clearSupportConsent();
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
  }, [clearSupportConsent, open]);

  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    let active = true;
    setCapabilityState('loading');
    setSupportCapability(null);
    const timeout = window.setTimeout(
      () => controller.abort(),
      SUPPORT_REPORT_CAPABILITY_TIMEOUT_MS,
    );

    void api.support.capability(controller.signal)
      .then(raw => {
        if (!active) return;
        const projected = projectSupportReportCapability(raw);
        setSupportCapability(projected);
        setCapabilityState(projected?.available ? 'available' : 'unavailable');
      })
      .catch(() => {
        if (!active) return;
        setSupportCapability(null);
        setCapabilityState('unavailable');
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
    setSupportActionStatus('');
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
    setSupportActionStatus('');
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

  const supportErrorMessage = (error: unknown): string => {
    if (error instanceof ApiError) {
      const code = typeof error.body.code === 'string' ? error.body.code : '';
      if (code === 'report_too_large') return 'The report is larger than private support accepts. Shorten the Markdown, then try again.';
      if (code === 'terms_mismatch') return 'The support terms changed. Review and accept the current terms before trying again.';
      if (code === 'report_busy') return 'The report is busy. Wait a moment, then check its status again.';
      if (code === 'report_not_found') return 'The local report receipt could not be found.';
    }
    return 'Private support is unavailable right now. Your draft is still here; you can copy or download it.';
  };

  const consentBindingFor = async (
    markdown: string,
    screenshot: CapturedSupportScreenshot | null,
    capability: SupportReportCapability,
  ) => JSON.stringify({
    markdown_sha256: await markdownSha256(markdown),
    screenshot_source_sha256: screenshot?.sha256 ?? null,
    terms_version: capability.terms_version,
    retention_policy_version: capability.retention_policy_version,
  });

  const updateSupportConsent = async (checked: boolean) => {
    clearSupportConsent();
    if (!checked || !supportCapability?.available) return;
    const generation = consentGenerationRef.current;
    const exactDraft = draft;
    const exactScreenshot = capturedScreenshot;
    const exactCapability = supportCapability;
    try {
      const binding = await consentBindingFor(exactDraft, exactScreenshot, exactCapability);
      if (consentGenerationRef.current !== generation) return;
      setSupportConsentBinding(binding);
      setSupportConsent(true);
    } catch {
      if (consentGenerationRef.current === generation) {
        setSupportActionStatus('Could not bind consent to the reviewed bytes. Nothing was sent.');
      }
    }
  };

  const captureScreenshot = async () => {
    const screenshotCapability = supportCapability?.screenshots;
    if (!screenshotCapability?.available || screenshotOperation !== 'idle') return;
    setScreenshotOperation('capturing');
    setScreenshotActionStatus('Creating a masked screenshot locally for your review...');
    clearSupportConsent();
    try {
      const captured = await captureSupportScreenshot(screenshotCapability);
      releaseScreenshotPreview();
      const previewUrl = URL.createObjectURL(captured.blob);
      screenshotPreviewUrlRef.current = previewUrl;
      setScreenshotPreviewUrl(previewUrl);
      setCapturedScreenshot(captured);
      setScreenshotActionStatus('Review the exact PNG below. It will not be sent until you consent and submit.');
    } catch {
      setScreenshotActionStatus('Could not create a safe screenshot. No screenshot was added or sent.');
    } finally {
      setScreenshotOperation('idle');
    }
  };

  const submitPrivately = async () => {
    if (
      !supportCapability?.available
      || !supportConsent
      || !supportConsentBinding
      || supportOperation !== 'idle'
    ) return;
    const exactMarkdown = draft;
    const exactScreenshot = capturedScreenshot;
    if (!exactMarkdown.trim() || reportUtf8Bytes(exactMarkdown) > supportCapability.max_report_bytes) return;
    if (exactScreenshot && !supportCapability.screenshots.available) return;
    const binding = await consentBindingFor(exactMarkdown, exactScreenshot, supportCapability);
    if (binding !== supportConsentBinding) {
      clearSupportConsent();
      setSupportActionStatus('The reviewed bytes changed. Review them and consent again.');
      return;
    }
    setSupportOperation('submitting');
    setSupportActionStatus(
      exactScreenshot
        ? 'Asking the local daemon to durably queue the exact Markdown and reviewed PNG...'
        : 'Asking the local daemon to submit this exact Markdown...',
    );
    setConfirmDelete(false);
    try {
      const result = await api.support.submit({
        report_markdown: exactMarkdown,
        accepted_terms_version: supportCapability.terms_version,
        accepted_retention_policy_version: supportCapability.retention_policy_version,
        ...(exactScreenshot ? {
          screenshot_png_base64: exactScreenshot.png_base64,
          screenshot_source_sha256: exactScreenshot.sha256,
          screenshot_width: exactScreenshot.width,
          screenshot_height: exactScreenshot.height,
        } : {}),
      });
      const projected = projectSupportReportStatus(result);
      if (!projected) throw new Error('invalid local support response');
      focusSubmittedStatusRef.current = true;
      setSupportReport(projected);
      clearSupportConsent();
      releaseScreenshotPreview();
      setCapturedScreenshot(null);
      setScreenshotActionStatus('');
      setSupportActionStatus('');
    } catch (error) {
      if (error instanceof ApiError && error.body.code === 'terms_mismatch') {
        clearSupportConsent();
        setCapabilityState('loading');
        try {
          const refreshed = projectSupportReportCapability(await api.support.capability());
          setSupportCapability(refreshed);
          setCapabilityState(refreshed?.available ? 'available' : 'unavailable');
          if (!refreshed?.screenshots.available && capturedScreenshot) {
            releaseScreenshotPreview();
            setCapturedScreenshot(null);
            setScreenshotActionStatus('Screenshot intake is no longer available; the local screenshot was removed.');
          }
        } catch {
          setSupportCapability(null);
          setCapabilityState('unavailable');
        }
      }
      setSupportActionStatus(supportErrorMessage(error));
    } finally {
      setSupportOperation('idle');
    }
  };

  const checkSupportStatus = async () => {
    if (!supportReport || supportOperation !== 'idle') return;
    setSupportOperation('checking');
    setSupportActionStatus('Checking the private report receipt...');
    setConfirmDelete(false);
    try {
      const result = await api.support.status(supportReport.client_report_id);
      const projected = projectSupportReportStatus(result, supportReport.client_report_id);
      if (!projected) throw new Error('invalid local support response');
      setSupportReport(projected);
      setSupportActionStatus('Status checked.');
    } catch (error) {
      setSupportActionStatus(supportErrorMessage(error));
    } finally {
      setSupportOperation('idle');
    }
  };

  const deleteSupportReport = async () => {
    if (!supportReport || supportOperation !== 'idle') return;
    setSupportOperation('deleting');
    setSupportActionStatus('Deleting the private report...');
    try {
      const result = await api.support.remove(supportReport.client_report_id);
      if (result.client_report_id !== supportReport.client_report_id || result.state !== 'deleted') {
        throw new Error('invalid local delete response');
      }
      setRecentReports(current => current.filter(report => (
        report.client_report_id !== supportReport.client_report_id
      )));
      setSupportReport(null);
      clearSupportConsent();
      releaseScreenshotPreview();
      setCapturedScreenshot(null);
      setScreenshotActionStatus('');
      setConfirmDelete(false);
      setSupportActionStatus('The private report was deleted. The editable Markdown remains in this tab.');
    } catch (error) {
      setSupportActionStatus(supportErrorMessage(error));
    } finally {
      setSupportOperation('idle');
    }
  };

  const checkRecentSupportReport = async (clientReportId: string) => {
    if (recentOperationId !== null) return;
    setRecentOperationId(clientReportId);
    setRecentConfirmDeleteId(null);
    setRecentActionStatus('Checking the saved private report receipt...');
    try {
      const result = await api.support.status(clientReportId);
      const projected = projectSupportReportStatus(result, clientReportId);
      if (!projected) throw new Error('invalid local support response');
      setRecentReports(current => current.map(report => (
        report.client_report_id === clientReportId ? projected : report
      )));
      if (supportReport?.client_report_id === clientReportId) setSupportReport(projected);
      setRecentActionStatus('Saved private report status checked.');
    } catch (error) {
      setRecentActionStatus(supportErrorMessage(error));
    } finally {
      setRecentOperationId(null);
    }
  };

  const deleteRecentSupportReport = async (clientReportId: string) => {
    if (recentOperationId !== null) return;
    setRecentOperationId(clientReportId);
    setRecentActionStatus('Deleting the saved private report...');
    try {
      const result = await api.support.remove(clientReportId);
      if (result.client_report_id !== clientReportId || result.state !== 'deleted') {
        throw new Error('invalid local delete response');
      }
      setRecentReports(current => current.filter(report => report.client_report_id !== clientReportId));
      if (supportReport?.client_report_id === clientReportId) setSupportReport(null);
      setRecentConfirmDeleteId(null);
      setRecentActionStatus('The saved private report was deleted.');
    } catch (error) {
      setRecentActionStatus(supportErrorMessage(error));
    } finally {
      setRecentOperationId(null);
    }
  };

  const startAnotherReport = () => {
    if (supportReport) {
      setRecentReports(current => [
        supportReport,
        ...current.filter(report => report.client_report_id !== supportReport.client_report_id),
      ].slice(0, 100));
      setRecentReportsState('ready');
    }
    setSupportReport(null);
    setSummary('');
    setWhatHappened('');
    setExpectedBehavior('');
    setDraft('');
    setStage('compose');
    clearSupportConsent();
    releaseScreenshotPreview();
    setCapturedScreenshot(null);
    setScreenshotActionStatus('');
    setConfirmDelete(false);
    setSupportActionStatus('The previous receipt remains available under Recent private reports.');
    setActionStatus('');
  };

  const canReview = summary.trim().length > 0 && whatHappened.trim().length > 0;
  const draftBytes = reportUtf8Bytes(draft);
  const reportIsLocked = supportOperation !== 'idle'
    || screenshotOperation !== 'idle'
    || (supportReport !== null && supportReport.state !== 'rejected');
  const canSendPrivately = capabilityState === 'available'
    && supportCapability?.available === true
    && supportConsent
    && supportConsentBinding !== null
    && draft.trim().length > 0
    && draftBytes <= supportCapability.max_report_bytes
    && screenshotOperation === 'idle'
    && supportOperation === 'idle';

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
              The draft stays in this browser tab until you explicitly send or share it. ClawJournal never captures screenshots automatically. An optional button can create a heavily masked app-view PNG for your exact review; logs, traces, hidden diagnostics, and the reporter itself are excluded.
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

        {recentReportsState === 'ready' && recentReports.some(report => (
          report.client_report_id !== supportReport?.client_report_id
        )) && (
          <section aria-labelledby={`${titleId}-recent-support`} style={{
            marginTop: 12,
            padding: 12,
            border: `1px solid ${colors.gray200}`,
            borderRadius: 8,
            background: colors.gray50,
          }}>
            <h3 id={`${titleId}-recent-support`} style={{ margin: 0, color: colors.gray900, fontSize: 14 }}>
              Recent private reports from this device
            </h3>
            <p style={{ margin: '5px 0 8px', color: colors.gray600, fontSize: 12 }}>
              These saved receipts are separate from the draft below. Opening one never changes or sends the current draft.
            </p>
            {recentReports.filter(report => report.client_report_id !== supportReport?.client_report_id).map(report => {
              const busy = recentOperationId === report.client_report_id;
              const confirmingDelete = recentConfirmDeleteId === report.client_report_id;
              return (
                <div key={report.client_report_id} style={{
                  padding: '9px 0',
                  borderTop: `1px solid ${colors.gray200}`,
                }}>
                  <div style={{ display: 'flex', flexWrap: 'wrap', justifyContent: 'space-between', gap: 8 }}>
                    <span style={{ color: colors.gray800, fontSize: 12.5, fontWeight: 600 }}>
                      {SUPPORT_REPORT_STATE_PRESENTATION[report.state].label}
                    </span>
                    <span style={{ color: colors.gray500, fontSize: 11.5 }}>{report.created_at}</span>
                  </div>
                  {report.receipt_id && (
                    <div style={{ marginTop: 4, color: colors.gray600, fontSize: 11.5, overflowWrap: 'anywhere' }}>
                      Receipt: {report.receipt_id}
                    </div>
                  )}
                  {report.screenshot && (
                    <div style={{ marginTop: 4, color: colors.gray600, fontSize: 11.5 }}>
                      Screenshot: {SUPPORT_REPORT_STATE_PRESENTATION[report.screenshot.state].label}
                    </div>
                  )}
                  <div style={{ display: 'flex', flexWrap: 'wrap', justifyContent: 'flex-end', gap: 7, marginTop: 7 }}>
                    <button
                      type="button"
                      aria-label={`Check status for report ${report.client_report_id}`}
                      disabled={recentOperationId !== null}
                      onClick={() => void checkRecentSupportReport(report.client_report_id)}
                      style={btnSecondary}
                    >
                      {busy && !confirmingDelete ? 'Checking…' : 'Check status'}
                    </button>
                    {!confirmingDelete ? (
                      <button
                        type="button"
                        aria-label={`Delete report ${report.client_report_id}`}
                        disabled={recentOperationId !== null}
                        onClick={() => setRecentConfirmDeleteId(report.client_report_id)}
                        style={btnSecondary}
                      >
                        Delete
                      </button>
                    ) : (
                      <>
                        <button type="button" disabled={recentOperationId !== null} onClick={() => setRecentConfirmDeleteId(null)} style={btnSecondary}>
                          Cancel
                        </button>
                        <button
                          type="button"
                          aria-label={`Confirm delete report ${report.client_report_id}`}
                          disabled={recentOperationId !== null}
                          onClick={() => void deleteRecentSupportReport(report.client_report_id)}
                          style={{ ...btnSecondary, color: colors.red700 }}
                        >
                          {busy ? 'Deleting…' : 'Confirm delete'}
                        </button>
                      </>
                    )}
                  </div>
                </div>
              );
            })}
            {recentReportsTruncated && (
              <p style={{ margin: '8px 0 0', color: colors.gray500, fontSize: 11.5 }}>
                Showing the 100 most recent saved reports.
              </p>
            )}
          </section>
        )}
        <div role="status" aria-live="polite" style={{ minHeight: 18, marginTop: 4, color: colors.blue700, fontSize: 11.5 }}>
          {recentActionStatus}
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
              readOnly={reportIsLocked}
              onChange={event => {
                setDraft(event.target.value.slice(0, MAX_BUG_REPORT_DRAFT_LENGTH));
                setActionStatus('');
                clearSupportConsent();
                setSupportActionStatus('');
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
              Copy or download only after reviewing this text. Opening GitHub sends none of it automatically. Private support receives this exact Markdown and only an optional PNG that you capture, preview, and explicitly include below.
            </p>

            {capabilityState === 'loading' && !supportReport && (
              <p role="status" aria-live="polite" style={{ margin: '12px 0 0', color: colors.gray500, fontSize: 12.5 }}>
                Checking private support availability…
              </p>
            )}

            {capabilityState === 'unavailable' && !supportReport && (
              <p role="status" aria-live="polite" style={{
                margin: '12px 0 0',
                padding: '8px 10px',
                borderRadius: 7,
                color: colors.yellow700,
                background: colors.yellow50,
                fontSize: 12.5,
              }}>
                Private submission is unavailable right now. Copy, download, and the blank GitHub issue remain available.
              </p>
            )}

            {supportCapability?.available && (
              <section aria-labelledby={`${titleId}-private-support`} style={{
                marginTop: 14,
                padding: 12,
                border: `1px solid ${colors.gray200}`,
                borderRadius: 8,
                background: colors.gray50,
              }}>
                <h3 id={`${titleId}-private-support`} style={{ margin: 0, color: colors.gray900, fontSize: 14 }}>
                  Private support
                </h3>
                <dl style={{ margin: '8px 0 0', fontSize: 12.5, lineHeight: 1.5 }}>
                  <dt style={{ fontWeight: 600, color: colors.gray700 }}>Purpose</dt>
                  <dd style={{ margin: '2px 0 8px', color: colors.gray600 }}>{supportCapability.purpose}</dd>
                  <dt style={{ fontWeight: 600, color: colors.gray700 }}>Retention</dt>
                  <dd style={{ margin: '2px 0 8px', color: colors.gray600 }}>{supportCapability.retention_text}</dd>
                </dl>
                <details style={{ color: colors.gray600, fontSize: 12.5 }}>
                  <summary style={{ cursor: 'pointer' }}>Support terms</summary>
                  <p style={{ whiteSpace: 'pre-wrap', lineHeight: 1.5 }}>{supportCapability.terms_text}</p>
                </details>
                <div style={{ marginTop: 8, color: draftBytes > supportCapability.max_report_bytes ? colors.red700 : colors.gray500, fontSize: 11.5 }}>
                  {draftBytes.toLocaleString()} / {supportCapability.max_report_bytes.toLocaleString()} UTF-8 bytes
                </div>

                {supportCapability.screenshots.available && (!supportReport || supportReport.state === 'rejected') && (
                  <section aria-labelledby={`${titleId}-optional-screenshot`} style={{
                    marginTop: 12,
                    paddingTop: 11,
                    borderTop: `1px solid ${colors.gray200}`,
                  }}>
                    <h4 id={`${titleId}-optional-screenshot`} style={{ margin: 0, color: colors.gray800, fontSize: 13 }}>
                      Optional masked app screenshot
                    </h4>
                    <p style={{ margin: '5px 0 8px', color: colors.gray600, fontSize: 12, lineHeight: 1.5 }}>
                      Capture is local and explicit. Dynamic text, form values, images, canvases, embedded pages, URL-bearing styles, and this reporter are masked or removed by default. Inspect the exact full-resolution PNG before including it.
                    </p>
                    {!capturedScreenshot ? (
                      <button
                        type="button"
                        disabled={screenshotOperation !== 'idle' || supportOperation !== 'idle'}
                        onClick={() => void captureScreenshot()}
                        style={btnSecondary}
                      >
                        {screenshotOperation === 'capturing' ? 'Capturing locally…' : 'Capture masked app view'}
                      </button>
                    ) : (
                      <figure style={{ margin: '8px 0 0' }}>
                        {screenshotPreviewUrl && (
                          <img
                            src={screenshotPreviewUrl}
                            alt="Exact masked screenshot selected for private support"
                            style={{
                              display: 'block',
                              width: '100%',
                              maxHeight: 360,
                              objectFit: 'contain',
                              background: colors.white,
                              border: `1px solid ${colors.gray300}`,
                              borderRadius: 6,
                            }}
                          />
                        )}
                        <figcaption style={{ marginTop: 6, color: colors.gray600, fontSize: 11.5, lineHeight: 1.45 }}>
                          Exact PNG: {capturedScreenshot.width} × {capturedScreenshot.height}, {capturedScreenshot.bytes.toLocaleString()} bytes<br />
                          SHA-256: <span style={{ overflowWrap: 'anywhere' }}>{capturedScreenshot.sha256}</span>
                        </figcaption>
                        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 7, marginTop: 7 }}>
                          {screenshotPreviewUrl && (
                            <a href={screenshotPreviewUrl} target="_blank" rel="noopener noreferrer" style={{ ...btnSecondary, textDecoration: 'none' }}>
                              Open full-resolution PNG
                            </a>
                          )}
                          <button type="button" disabled={screenshotOperation !== 'idle'} onClick={() => void captureScreenshot()} style={btnSecondary}>
                            {screenshotOperation === 'capturing' ? 'Retaking…' : 'Retake'}
                          </button>
                          <button type="button" disabled={screenshotOperation !== 'idle'} onClick={removeCapturedScreenshot} style={btnSecondary}>
                            Remove screenshot
                          </button>
                        </div>
                      </figure>
                    )}
                    <div role="status" aria-live="polite" style={{ minHeight: 18, marginTop: 5, color: colors.blue700, fontSize: 11.5 }}>
                      {screenshotActionStatus}
                    </div>
                  </section>
                )}

                {(!supportReport || supportReport.state === 'rejected') && (
                  <>
                    <label style={{ display: 'flex', alignItems: 'flex-start', gap: 8, marginTop: 10, color: colors.gray700, fontSize: 12.5, lineHeight: 1.45 }}>
                      <input
                        type="checkbox"
                        checked={supportConsent}
                        disabled={supportOperation !== 'idle' || screenshotOperation !== 'idle'}
                        onChange={event => void updateSupportConsent(event.target.checked)}
                        style={{ marginTop: 2 }}
                      />
                      <span>
                        I reviewed the exact Markdown above{capturedScreenshot ? ' and the exact PNG preview (including its SHA-256)' : ''} and accept support terms {supportCapability.terms_version} and retention policy {supportCapability.retention_policy_version}.
                      </span>
                    </label>
                    <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 10 }}>
                      <button
                        type="button"
                        disabled={!canSendPrivately}
                        onClick={() => void submitPrivately()}
                        style={{
                          ...btnPrimary,
                          opacity: canSendPrivately ? 1 : 0.55,
                          cursor: canSendPrivately ? 'pointer' : 'not-allowed',
                        }}
                      >
                        {supportOperation === 'submitting' ? 'Submitting…' : supportReport?.state === 'rejected' ? 'Retry privately' : 'Send privately'}
                      </button>
                    </div>
                  </>
                )}
              </section>
            )}

            {supportReport && (
              <section aria-labelledby={`${titleId}-private-status`} style={{
                marginTop: 12,
                padding: 12,
                border: `1px solid ${supportReport.state === 'rejected' ? colors.yellow200 : colors.blue100}`,
                borderRadius: 8,
                background: supportReport.state === 'rejected' ? colors.yellow50 : colors.blue50,
              }}>
                <h3 ref={supportStatusHeadingRef} tabIndex={-1} id={`${titleId}-private-status`} style={{ margin: 0, color: colors.gray900, fontSize: 14 }}>
                  Private report: {SUPPORT_REPORT_STATE_PRESENTATION[supportReport.state].label}
                </h3>
                <p style={{ margin: '6px 0 0', color: colors.gray700, fontSize: 12.5, lineHeight: 1.5 }}>
                  {SUPPORT_REPORT_STATE_PRESENTATION[supportReport.state].description}
                </p>
                {supportReport.message && (
                  <p style={{ margin: '6px 0 0', color: colors.gray600, fontSize: 12.5 }}>
                    {supportReport.message.slice(0, 500)}
                  </p>
                )}
                {supportReport.screenshot && (
                  <div style={{ marginTop: 8, padding: 8, borderRadius: 6, background: colors.white, color: colors.gray700, fontSize: 12, lineHeight: 1.5 }}>
                    <strong>Screenshot: {SUPPORT_REPORT_STATE_PRESENTATION[supportReport.screenshot.state].label}</strong>
                    <div>{supportReport.screenshot.message}</div>
                    <div>
                      {supportReport.screenshot.width} × {supportReport.screenshot.height}, {supportReport.screenshot.source_bytes.toLocaleString()} source bytes
                    </div>
                    <div style={{ overflowWrap: 'anywhere' }}>Source SHA-256: {supportReport.screenshot.source_sha256}</div>
                    {supportReport.screenshot.sanitized_sha256 && (
                      <div style={{ overflowWrap: 'anywhere' }}>
                        Sanitized SHA-256: {supportReport.screenshot.sanitized_sha256}
                      </div>
                    )}
                  </div>
                )}
                <dl style={{ margin: '8px 0 0', color: colors.gray600, fontSize: 12, lineHeight: 1.5 }}>
                  {supportReport.receipt_id && (
                    <><dt style={{ fontWeight: 600 }}>Receipt</dt><dd style={{ margin: '0 0 4px', overflowWrap: 'anywhere' }}>{supportReport.receipt_id}</dd></>
                  )}
                  <dt style={{ fontWeight: 600 }}>Local report ID</dt>
                  <dd style={{ margin: '0 0 4px', overflowWrap: 'anywhere' }}>{supportReport.client_report_id}</dd>
                  {supportReport.expires_at && (
                    <><dt style={{ fontWeight: 600 }}>Scheduled deletion</dt><dd style={{ margin: 0, overflowWrap: 'anywhere' }}>{supportReport.expires_at}</dd></>
                  )}
                </dl>
                <div style={{ display: 'flex', flexWrap: 'wrap', justifyContent: 'flex-end', gap: 8, marginTop: 10 }}>
                  <button type="button" disabled={supportOperation !== 'idle'} onClick={() => void checkSupportStatus()} style={btnSecondary}>
                    {supportOperation === 'checking' ? 'Checking…' : 'Check status'}
                  </button>
                  {!confirmDelete ? (
                    <button type="button" disabled={supportOperation !== 'idle'} onClick={() => setConfirmDelete(true)} style={btnSecondary}>
                      Delete private report
                    </button>
                  ) : (
                    <>
                      <button type="button" disabled={supportOperation !== 'idle'} onClick={() => setConfirmDelete(false)} style={btnSecondary}>
                        Cancel delete
                      </button>
                      <button type="button" disabled={supportOperation !== 'idle'} onClick={() => void deleteSupportReport()} style={{ ...btnSecondary, color: colors.red700 }}>
                        {supportOperation === 'deleting' ? 'Deleting…' : 'Confirm delete'}
                      </button>
                    </>
                  )}
                  <button type="button" disabled={supportOperation !== 'idle' || confirmDelete} onClick={startAnotherReport} style={btnSecondary}>
                    Report another problem
                  </button>
                </div>
              </section>
            )}

            <div role="status" aria-live="polite" style={{ minHeight: 20, marginTop: 6, color: colors.blue700, fontSize: 12.5 }}>
              {supportActionStatus || actionStatus}
            </div>
            <div style={{
              display: 'flex',
              flexWrap: 'wrap',
              justifyContent: 'flex-end',
              gap: 8,
              marginTop: 10,
            }}>
              <button
                type="button"
                disabled={reportIsLocked}
                onClick={() => {
                  setStage('compose');
                  setActionStatus('');
                  setSupportActionStatus('');
                  clearSupportConsent();
                }}
                style={{ ...btnSecondary, opacity: reportIsLocked ? 0.55 : 1 }}
              >
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
