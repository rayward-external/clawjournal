import { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { useLocation, useSearchParams } from 'react-router-dom';
import type { Session, Share as ShareType } from '../../types.ts';
import { api } from '../../api.ts';
import { useToast } from '../../components/Toast.tsx';
import { Spinner } from '../../components/Spinner.tsx';
import { Stepper } from '../../components/Stepper.tsx';
import {
  STEPS,
} from './types.ts';
import type {
  BlockedShareSession,
  ReadySession,
  RedactedReviewMessage,
  RedactedSessionData,
  ShareDestination,
  ShareReadyStats,
  StepKey,
} from './types.ts';
import {
  blockedSessionsFromError,
  bucketOf,
  classify,
  completedKeysForStep,
  emptyBuckets,
  formatBytes,
  formatDate,
  parseStep,
  queueFromStats,
  sessionTotalTokens,
} from './helpers.ts';
import { SHARE_SHELL_WIDTH, globalStyles } from './styles.tsx';
import { QueueStep } from './QueueStep.tsx';
import { RedactStep } from './RedactStep.tsx';
import { ReviewStep } from './ReviewStep.tsx';
import { PackageStep } from './PackageStep.tsx';
import { SubmitStep } from './SubmitStep.tsx';
import { DoneStep } from './DoneStep.tsx';

export function Share() {
  const { toast } = useToast();
  const location = useLocation();
  const [searchParams, setSearchParams] = useSearchParams();
  const internalSearchRef = useRef<string | null>(null);
  const skipNextUrlSyncRef = useRef(false);

  const [activeStep, setActiveStep] = useState<StepKey>(
    () => parseStep(searchParams.get('step')),
  );
  const [completedKeys, setCompletedKeys] = useState<Set<string>>(() => {
    const step = parseStep(searchParams.get('step'));
    return completedKeysForStep(step);
  });

  const [readyStats, setReadyStats] = useState<ShareReadyStats | null>(null);
  const [shares, setShares] = useState<ShareType[]>([]);

  // Queue state: ordered list (drag-reorder) with a derived Set for lookups.
  const [queueOrder, setQueueOrder] = useState<string[]>(() => {
    const csv = searchParams.get('ids');
    return csv ? csv.split(',').filter(Boolean) : [];
  });
  const [selectionInitialized, setSelectionInitialized] = useState(() => !!searchParams.get('ids'));
  const queueSet = useMemo(() => new Set(queueOrder), [queueOrder]);

  const [note, setNote] = useState(() => searchParams.get('note') || '');
  const [aiPiiEnabled, setAiPiiEnabled] = useState(() => searchParams.get('ai_pii') === '1');
  const [drawerSessionId, setDrawerSessionId] = useState<string | null>(null);
  const [showAddTraces, setShowAddTraces] = useState(false);
  const [showHelp, setShowHelp] = useState(false);
  const [loading, setLoading] = useState(true);

  // Add-traces filter
  const [searchQuery, setSearchQuery] = useState('');
  const [sourceFilter, setSourceFilter] = useState('');
  const [projectFilter, setProjectFilter] = useState('');
  const [scoreFilter, setScoreFilter] = useState(0);
  const [dateFilter, setDateFilter] = useState('');

  const resetAddTracesPicker = useCallback(() => {
    setShowAddTraces(false);
    setSearchQuery('');
    setSourceFilter('');
    setProjectFilter('');
    setScoreFilter(0);
    setDateFilter('');
  }, []);

  // Redaction state
  const [redactedSessions, setRedactedSessions] = useState<Record<string, RedactedSessionData>>({});

  // Review state
  const [approvedIds, setApprovedIds] = useState<Set<string>>(new Set());
  const [expandedReviewIds, setExpandedReviewIds] = useState<Set<string>>(new Set());

  // Package state
  const [packagedShareId, setPackagedShareId] = useState<string | null>(
    () => searchParams.get('share'),
  );
  const [packageProgress, setPackageProgress] = useState(0);
  const [packageLog, setPackageLog] = useState('');
  const [packagingFailed, setPackagingFailed] = useState<string | null>(null);
  const [blockedPackageSessions, setBlockedPackageSessions] = useState<BlockedShareSession[]>([]);

  // Done state
  const [bundleInfo, setBundleInfo] = useState<{ traces: number; created: string; approxSize: string } | null>(null);
  const [receiptId, setReceiptId] = useState<string | null>(null);
  const [hostedStatus, setHostedStatus] = useState<string | null>(null);
  const [supportContact, setSupportContact] = useState<string | null>(null);

  // Candidates (empty queue hint)
  const [candidates, setCandidates] = useState<Session[]>([]);
  const [scoringBackend, setScoringBackend] = useState<{ backend: string | null; display_name: string | null } | null>(null);
  const [shareDestination, setShareDestination] = useState<ShareDestination | null>(null);
  // Distinguish "still loading / failed to reach" from "loaded, not configured".
  // The hosted-destination probe is a remote network call, so collapsing all
  // three into `shareDestination === null` made the Done page hide the Submit
  // button on a transient failure (or a slow load right after a daemon
  // restart), which looks identical to "no submit option for this install".
  const [destinationLoading, setDestinationLoading] = useState(true);
  const [destinationFailed, setDestinationFailed] = useState(false);

  // =================================================
  // Initial load
  // =================================================

  const loadShareDestination = useCallback(() => {
    setDestinationLoading(true);
    setDestinationFailed(false);
    api.shareDestination()
      .then((destination) => {
        setShareDestination(destination);
        setSupportContact(destination?.support_contact || null);
      })
      .catch(() => {
        setShareDestination(null);
        setDestinationFailed(true);
      })
      .finally(() => setDestinationLoading(false));
  }, []);

  useEffect(() => {
    // The hosted-destination probe can be slow/flaky (remote round-trip); load
    // it independently so it never blocks the page or its failure poisons the
    // whole initial load.
    loadShareDestination();
    Promise.all([
      api.shareReady({ includeUnapproved: true }),
      api.shares.list(),
      api.scoringBackend().catch(() => ({ backend: null, display_name: null })),
    ]).then(([stats, shareList, backend]) => {
      setReadyStats(stats);
      setShares(shareList);
      setScoringBackend(backend);
      if (!selectionInitialized && stats.sessions.length > 0) {
        // Server recommendation is an ordered, reviewable default package.
        // Trust it and only filter out ids the client can't resolve
        // (eg. excluded projects).
        const recommended = queueFromStats(stats);
        setQueueOrder(recommended);
        setSelectionInitialized(true);
      }
      if (stats.sessions.length === 0) {
        api.sessions.list({ status: 'new', sort: 'start_time', order: 'desc', limit: 10 })
          .then(setCandidates)
          .catch(() => setCandidates([]));
      }
      setLoading(false);
    }).catch((e) => {
      toast(e instanceof Error ? e.message : 'Failed to load data', 'error');
      setLoading(false);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // =================================================
  // URL sync
  // =================================================

  useEffect(() => {
    if (selectionInitialized || !readyStats || searchParams.get('ids')) return;
    setQueueOrder(queueFromStats(readyStats));
    setSelectionInitialized(true);
  }, [readyStats, searchParams, selectionInitialized]);

  useEffect(() => {
    const currentSearch = location.search.startsWith('?') ? location.search.slice(1) : location.search;
    if (internalSearchRef.current === currentSearch) return;

    internalSearchRef.current = currentSearch;
    skipNextUrlSyncRef.current = true;

    const idsParam = searchParams.get('ids');
    const ids = idsParam ? idsParam.split(',').filter(Boolean) : null;
    const step = parseStep(searchParams.get('step'));

    setActiveStep(step);
    setCompletedKeys(completedKeysForStep(step));
    setNote(searchParams.get('note') || '');
    setAiPiiEnabled(searchParams.get('ai_pii') === '1');
    setPackagedShareId(searchParams.get('share'));
    setRedactedSessions({});
    setApprovedIds(new Set());
    setExpandedReviewIds(new Set());
    setBundleInfo(null);
    setReceiptId(null);
    setHostedStatus(null);
    setSupportContact(null);
    setPackageProgress(0);
    setPackageLog('');
    setPackagingFailed(null);
    setBlockedPackageSessions([]);

    if (ids) {
      setQueueOrder(ids);
      setSelectionInitialized(true);
      return;
    }

    const recommended = readyStats && readyStats.sessions.length > 0
      ? queueFromStats(readyStats)
      : [];
    setQueueOrder(recommended);
    setSelectionInitialized(!!readyStats);
  }, [location.search, readyStats, searchParams]);

  useEffect(() => {
    if (skipNextUrlSyncRef.current) {
      skipNextUrlSyncRef.current = false;
      return;
    }
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      if (activeStep === 'queue') next.delete('step'); else next.set('step', activeStep);
      const csv = queueOrder.join(',');
      if (csv) next.set('ids', csv); else next.delete('ids');
      if (note) next.set('note', note); else next.delete('note');
      if (aiPiiEnabled) next.set('ai_pii', '1'); else next.delete('ai_pii');
      if (packagedShareId) next.set('share', packagedShareId); else next.delete('share');
      internalSearchRef.current = next.toString();
      return next;
    }, { replace: true });
  }, [activeStep, queueOrder, note, aiPiiEnabled, packagedShareId, setSearchParams]);

  // Drop cached redacted entries when sessions leave the queue.
  useEffect(() => {
    setRedactedSessions((prev) => {
      let changed = false;
      const next: Record<string, RedactedSessionData> = {};
      for (const [sid, data] of Object.entries(prev)) {
        if (queueSet.has(sid)) next[sid] = data;
        else changed = true;
      }
      return changed ? next : prev;
    });
    setApprovedIds((prev) => {
      let changed = false;
      const next = new Set<string>();
      for (const id of prev) {
        if (queueSet.has(id)) next.add(id);
        else changed = true;
      }
      return changed ? next : prev;
    });
  }, [queueSet]);

  const reload = () => {
    api.shareReady({ includeUnapproved: true }).then((stats) => {
      setReadyStats(stats);
      if (stats.sessions.length === 0) {
        api.sessions.list({ status: 'new', sort: 'start_time', order: 'desc', limit: 10 })
          .then(setCandidates)
          .catch(() => { });
      } else {
        setCandidates([]);
      }
    }).catch(() => { });
    api.shares.list().then(setShares).catch(() => { });
  };

  const sessionById = useMemo(() => {
    const m: Record<string, ReadySession> = {};
    readyStats?.sessions.forEach((s) => { m[s.session_id] = s; });
    return m;
  }, [readyStats]);

  const queuedSessions = useMemo(
    () => queueOrder.map((id) => sessionById[id]).filter((s): s is ReadySession => !!s),
    [queueOrder, sessionById],
  );

  useEffect(() => {
    if (!packagedShareId) return;
    api.shares.get(packagedShareId).then((share) => {
      const piiReview = (share.manifest?.redaction_summary as { pii_review?: { ai_enabled?: unknown } } | undefined)?.pii_review;
      if (!searchParams.get('ai_pii') && typeof piiReview?.ai_enabled === 'boolean') {
        setAiPiiEnabled(piiReview.ai_enabled);
      }
      if (share.hosted_receipt_id) {
        setReceiptId(share.hosted_receipt_id);
        setCompletedKeys((prev) => new Set([...prev, 'submit']));
        setActiveStep((step) => step === 'submit' ? 'done' : step);
      }
      if (share.hosted_status) setHostedStatus(share.hosted_status);
      if (share.session_count) {
        setBundleInfo((current) => current || {
          traces: share.session_count,
          created: share.created_at ? formatDate(share.created_at) : '',
          approxSize: share.zip_size_bytes ? formatBytes(share.zip_size_bytes) : 'ready',
        });
      }
    }).catch(() => { });
  }, [packagedShareId, searchParams]);

  // =================================================
  // Queue actions
  // =================================================

  const removeFromQueue = (id: string) => {
    const next = queueOrder.filter((x) => x !== id);
    setQueueOrder(next);
    if (next.length === 0) resetAddTracesPicker();
  };

  const addToQueue = (id: string) => {
    setQueueOrder((prev) => prev.includes(id) ? prev : [...prev, id]);
  };

  const updateAiPiiEnabled = (enabled: boolean) => {
    setAiPiiEnabled(enabled);
    setRedactedSessions({});
    setApprovedIds(new Set());
    setExpandedReviewIds(new Set());
    setPackagedShareId(null);
    setPackageProgress(0);
    setPackageLog('');
    setPackagingFailed(null);
    setBlockedPackageSessions([]);
    setBundleInfo(null);
    // Toggling AI-PII invalidates the packaged bundle and every step after the
    // queue, so collapse the stepper and clear any prior submit/receipt state.
    // Otherwise a completed Submit/Done entry stays clickable with a now-null
    // share id, stranding the user in a dead-end step.
    setCompletedKeys(completedKeysForStep('queue'));
    setReceiptId(null);
    setHostedStatus(null);
    setSupportContact(null);
  };

  const reorderQueue = (fromId: string, overId: string) => {
    if (fromId === overId) return;
    setQueueOrder((prev) => {
      const fromIdx = prev.indexOf(fromId);
      const overIdx = prev.indexOf(overId);
      if (fromIdx === -1 || overIdx === -1) return prev;
      const next = [...prev];
      const [moved] = next.splice(fromIdx, 1);
      next.splice(overIdx, 0, moved);
      return next;
    });
  };

  const startFreshShare = useCallback(() => {
    setQueueOrder([]);
    setCompletedKeys(new Set());
    setPackagedShareId(null);
    setNote('');
    setAiPiiEnabled(false);
    setRedactedSessions({});
    setApprovedIds(new Set());
    setExpandedReviewIds(new Set());
    setBundleInfo(null);
    setReceiptId(null);
    setHostedStatus(null);
    setSupportContact(null);
    setPackageProgress(0);
    setPackageLog('');
    setPackagingFailed(null);
    setBlockedPackageSessions([]);
    resetAddTracesPicker();
    setActiveStep('queue');
  }, [resetAddTracesPicker]);

  const onStepClick = (key: string) => {
    const k = key as StepKey;
    if (k === activeStep) return;
    if (k === 'queue') { setActiveStep('queue'); return; }
    if (completedKeys.has(k)) setActiveStep(k);
  };

  // =================================================
  // Step 2: Redact
  // =================================================

  // Serialize redaction: the local Claude CLI we call for AI PII review is
  // single-threaded per install and starts timing out under parallel load.
  // One trace at a time is slower wall-clock but dramatically cuts the
  // rules-only fallback rate. A single automatic retry on failure catches
  // transient timeouts without doubling the wait for the common case.
  const REDACTION_CONCURRENCY = 2;
  const REDACTION_RETRIES = 1;
  const redactionStartedRef = useRef(false);

  const runRedaction = useCallback(async () => {
    if (redactionStartedRef.current) return;
    redactionStartedRef.current = true;
    const sessions = queuedSessions;
    const cached = redactedSessions;
    const missing = sessions.filter((s) => !cached[s.session_id]);

    // mark missing ones as loading up-front so the per-trace list renders
    if (missing.length > 0) {
      setRedactedSessions((prev) => {
        const next = { ...prev };
        missing.forEach((s) => {
          if (!next[s.session_id]) next[s.session_id] = { messages: [], loading: true };
        });
        return next;
      });
    }

    const fetchReport = async (sessionId: string) => {
      let lastErr: unknown = null;
      for (let attempt = 0; attempt <= REDACTION_RETRIES; attempt++) {
        try {
          return await api.sessions.redactionReport(sessionId, { aiPii: aiPiiEnabled });
        } catch (e) {
          lastErr = e;
          if (attempt < REDACTION_RETRIES) {
            // brief pause to let a flaky CLI/model settle before retrying
            await new Promise((r) => setTimeout(r, 800));
          }
        }
      }
      throw lastErr;
    };

    const processOne = async (s: ReadySession) => {
      try {
        const report = await fetchReport(s.session_id);
        const msgs: RedactedReviewMessage[] = (report.redacted_session.messages || []).map((m) => ({
          role: m.role,
          content: m.content || '',
          thinking: m.thinking,
          tool_uses: m.tool_uses,
          timestamp: m.timestamp,
        }));
        const buckets = emptyBuckets();
        for (const entry of report.redaction_log || []) {
          buckets[bucketOf(entry.type)] += 1;
        }
        const trufflehogHits = (report.redaction_log || [])
          .filter((entry) => entry.type && entry.type.startsWith('trufflehog'))
          .length;
        setRedactedSessions((prev) => ({
          ...prev,
          [s.session_id]: {
            messages: msgs, loading: false,
            redactionCount: report.redaction_count,
            aiPiiFindings: report.ai_pii_findings || [],
            aiCoverage: report.ai_coverage || (aiPiiEnabled ? 'rules_only' : 'disabled'),
            buckets,
            trufflehogHits,
          },
        }));
      } catch {
        setRedactedSessions((prev) => ({
          ...prev,
          [s.session_id]: {
            messages: [{ role: 'system', content: '(unable to load redacted content)' }],
            loading: false,
            redactionCount: 0,
            aiCoverage: aiPiiEnabled ? 'rules_only' : 'disabled',
            buckets: emptyBuckets(),
          },
        }));
      }
    };

    for (let i = 0; i < missing.length; i += REDACTION_CONCURRENCY) {
      const batch = missing.slice(i, i + REDACTION_CONCURRENCY);
      await Promise.all(batch.map(processOne));
    }
    redactionStartedRef.current = false;
  }, [queuedSessions, redactedSessions, aiPiiEnabled]);

  const handleStartRedaction = () => {
    setCompletedKeys((prev) => new Set([...prev, 'queue']));
    setActiveStep('redact');
    void runRedaction();
  };

  // if Redact step is the active step and there are sessions lacking data, kick it off
  useEffect(() => {
    if (activeStep !== 'redact') return;
    const anyMissing = queuedSessions.some((s) => !redactedSessions[s.session_id] || redactedSessions[s.session_id].loading);
    if (anyMissing) void runRedaction();
  }, [activeStep, queuedSessions, redactedSessions, runRedaction]);

  const redactAllDone = queuedSessions.length > 0 && queuedSessions.every((s) => {
    const d = redactedSessions[s.session_id];
    return d && !d.loading;
  });

  const goToReview = () => {
    setCompletedKeys((prev) => new Set([...prev, 'queue', 'redact']));
    // Auto-expand the first unapproved trace for immediate attention.
    const firstUnapproved = queuedSessions.find((s) => !approvedIds.has(s.session_id));
    if (firstUnapproved) {
      setExpandedReviewIds(new Set([firstUnapproved.session_id]));
    }
    setActiveStep('review');
  };

  // =================================================
  // Step 3: Review
  // =================================================

  const approveTrace = (id: string) => {
    setApprovedIds((prev) => new Set([...prev, id]));
    // auto-advance: collapse this row, expand next unapproved
    setExpandedReviewIds((prev) => {
      const n = new Set(prev);
      n.delete(id);
      const currentIdx = queuedSessions.findIndex((s) => s.session_id === id);
      for (let i = 1; i <= queuedSessions.length; i++) {
        const next = queuedSessions[(currentIdx + i) % queuedSessions.length];
        if (!next) break;
        if (!approvedIds.has(next.session_id) && next.session_id !== id) {
          n.add(next.session_id);
          break;
        }
      }
      return n;
    });
  };

  const approveAllClean = () => {
    setApprovedIds((prev) => {
      const n = new Set(prev);
      queuedSessions.forEach((s) => {
        if (classify(redactedSessions[s.session_id]) === 'clear') n.add(s.session_id);
      });
      return n;
    });
  };

  const retryAiReview = async (id: string) => {
    setRedactedSessions((prev) => ({
      ...prev,
      [id]: { ...(prev[id] || { messages: [] }), loading: true },
    }));
    // One automatic retry mirrors the initial-run policy.
    let report: Awaited<ReturnType<typeof api.sessions.redactionReport>> | null = null;
    for (let attempt = 0; attempt <= 1; attempt++) {
      try {
        report = await api.sessions.redactionReport(id, { aiPii: true });
        break;
      } catch {
        if (attempt === 0) await new Promise((r) => setTimeout(r, 800));
      }
    }
    if (!report) {
      toast('AI review retry failed', 'error');
      setRedactedSessions((prev) => ({
        ...prev,
        [id]: { ...(prev[id] || { messages: [] }), loading: false },
      }));
      return;
    }
    const msgs: RedactedReviewMessage[] = (report.redacted_session.messages || []).map((m) => ({
      role: m.role,
      content: m.content || '',
      thinking: m.thinking,
      tool_uses: m.tool_uses,
      timestamp: m.timestamp,
    }));
    const buckets = emptyBuckets();
    for (const entry of report.redaction_log || []) buckets[bucketOf(entry.type)] += 1;
    const trufflehogHits = (report.redaction_log || [])
      .filter((entry) => entry.type && entry.type.startsWith('trufflehog'))
      .length;
    setRedactedSessions((prev) => ({
      ...prev,
      [id]: {
        messages: msgs, loading: false,
        redactionCount: report.redaction_count,
        aiPiiFindings: report.ai_pii_findings || [],
        aiCoverage: report.ai_coverage || 'rules_only',
        buckets,
        trufflehogHits,
      },
    }));
  };

  const toggleReviewExpand = (id: string) => {
    setExpandedReviewIds((prev) => {
      const n = new Set(prev);
      if (n.has(id)) n.delete(id); else n.add(id);
      return n;
    });
  };

  // =================================================
  // Step 4: Package
  // =================================================

  const packagingStartedRef = useRef(false);

  const runPackage = useCallback(async () => {
    if (packagingStartedRef.current) return;
    packagingStartedRef.current = true;
    setPackagingFailed(null);
    setBlockedPackageSessions([]);
    setPackageProgress(0);
    setPackageLog('Allocating bundle...');
    setCompletedKeys((prev) => new Set([...prev, 'queue', 'redact', 'review']));

    const approvedList = queuedSessions.filter((s) => approvedIds.has(s.session_id));
    const logLines = [
      'Allocating bundle...',
      'Writing manifest.json...',
      ...approvedList.map((s) => `Adding ${s.session_id.slice(0, 10)}.jsonl...`),
      aiPiiEnabled ? 'Running final AI PII review...' : 'Running final rules-only PII review...',
      'Running final secret scan...',
      'Sealing bundle...',
    ];

    const animStart = Date.now();
    const duration = 2200 + approvedList.length * 220;

    const timers: number[] = [];
    logLines.forEach((line, i) => {
      timers.push(window.setTimeout(() => setPackageLog(line), (duration / logLines.length) * i));
    });
    const tick = window.setInterval(() => {
      const elapsed = Date.now() - animStart;
      setPackageProgress(Math.min(95, Math.round((elapsed / duration) * 95)));
    }, 60);

    const clearAllTimers = () => {
      window.clearInterval(tick);
      timers.forEach((t) => window.clearTimeout(t));
    };

    try {
      const ids = approvedList.map((s) => s.session_id);
      const { share_id } = await api.shares.create(ids, note || undefined);

      // Finish the animation cleanly before exposing the share id — the
      // `packageProgress >= 100 && packagedShareId` combination triggers the
      // fallback useEffect that flips the stepper to Done.
      const animRemaining = Math.max(0, duration - (Date.now() - animStart));
      await new Promise((r) => window.setTimeout(r, animRemaining));
      clearAllTimers();
      setPackageProgress(98);
      setPackageLog('Finalizing zip...');

      // Seal performs the final local-only PII pass and post-PII TruffleHog
      // gate without triggering a browser save. The Done button is the only
      // action that downloads bytes.
      const sealed = await api.shares.seal(share_id, { aiPii: aiPiiEnabled });

      setPackageProgress(100);
      setPackageLog('Done.');

      // Bundle info — use `undefined` locale (empty-array form is a known
      // source of RangeError in some Intl configs).
      const totalTokens = approvedList.reduce((sum, s) => sum + sessionTotalTokens(s), 0);
      const approxMB = Math.max(0.1, (totalTokens * 0.3) / (1024 * 1024));
      const sizeLabel = sealed.zip_size_bytes ? formatBytes(sealed.zip_size_bytes) : (approxMB >= 1 ? `${approxMB.toFixed(1)} MB` : `${(approxMB * 1024).toFixed(0)} KB`);
      try {
        setBundleInfo({
          traces: approvedList.length,
          created: new Date().toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' }),
          approxSize: sizeLabel,
        });
      } catch {
        setBundleInfo({
          traces: approvedList.length,
          created: new Date().toISOString().slice(11, 16),
          approxSize: sizeLabel,
        });
      }

      // Setting `packagedShareId` last — the fallback useEffect watches this
      // to force the step transition, so it must come after every other
      // success-path state update.
      setPackagedShareId(share_id);

      try { reload(); } catch { /* ignore */ }
      try { toast('Bundle ready', 'success'); } catch { /* ignore */ }
    } catch (err: unknown) {
      clearAllTimers();
      const msg = err instanceof Error ? err.message : 'Package failed';
      setBlockedPackageSessions(blockedSessionsFromError(err));
      setPackagingFailed(msg);
      setPackageLog(`Failed: ${msg}`);
      toast(msg, 'error');
    } finally {
      packagingStartedRef.current = false;
    }
  }, [queuedSessions, approvedIds, note, toast, aiPiiEnabled]);

  const handleStartPackage = () => {
    if (queuedSessions.length === 0) return;
    setCompletedKeys((prev) => new Set([...prev, 'queue', 'redact', 'review']));
    setActiveStep('package');
    void runPackage();
  };

  const removeBlockedAndRetry = useCallback(() => {
    if (blockedPackageSessions.length === 0) return;
    const blockedIds = new Set(blockedPackageSessions.map((s) => s.session_id));
    const remainingApproved = queuedSessions.filter((s) => (
      approvedIds.has(s.session_id) && !blockedIds.has(s.session_id)
    ));

    setQueueOrder((prev) => prev.filter((id) => !blockedIds.has(id)));
    setApprovedIds((prev) => {
      const next = new Set(prev);
      blockedIds.forEach((id) => next.delete(id));
      return next;
    });
    setBlockedPackageSessions([]);
    setPackageProgress(0);
    setPackagedShareId(null);

    if (remainingApproved.length === 0) {
      setPackagingFailed('All approved traces were blocked by the final secret scan.');
      setPackageLog('No approved traces remain.');
      setActiveStep('review');
      return;
    }

    setPackagingFailed(null);
    setPackageLog('Removed blocked traces. Retrying...');
  }, [approvedIds, blockedPackageSessions, queuedSessions]);

  // kick off packaging when the step becomes active (eg. back-forward)
  useEffect(() => {
    if (activeStep === 'package' && !packagingStartedRef.current && !packagedShareId) {
      void runPackage();
    }
  }, [activeStep, packagedShareId, runPackage]);

  // Belt-and-suspenders: advance to Done once the share id lands and the
  // animation has finished. Runs even if the inline `setActiveStep('done')`
  // inside `runPackage` fell through a silent error path.
  useEffect(() => {
    // Wait for the destination probe before deciding submit-vs-done. The probe
    // loads independently of the page now, so a null `shareDestination` here may
    // just mean "still loading" — routing to Done on that would strand a
    // genuinely-submittable bundle, since this effect can't re-route once
    // `activeStep` leaves 'package'.
    if (activeStep === 'package' && packagedShareId && packageProgress >= 100 && !packagingFailed && !destinationLoading) {
      setCompletedKeys((prev) => new Set([...prev, 'package']));
      const canSubmit = !!shareDestination?.daemon_upload_supported && !!shareDestination?.submissions_open;
      setActiveStep(canSubmit ? 'submit' : 'done');
    }
  }, [activeStep, packagedShareId, packageProgress, packagingFailed, shareDestination, destinationLoading]);

  // Reload/deep-link robustness: a page reload or bookmark can land directly on
  // Submit. If hosted submission isn't available (disabled, closed, or the
  // destination failed to load) and the share hasn't already been submitted,
  // fall back to the download-only Done view instead of stranding the user on a
  // Submit step they can't complete. Gated on `destinationLoading` so we wait
  // for the (now-independent) destination probe — a still-null destination only
  // after it resolves means not-submittable. Mirrors the package→done branch.
  useEffect(() => {
    if (activeStep !== 'submit' || receiptId || loading || destinationLoading) return;
    const canSubmit = !!shareDestination?.daemon_upload_supported && !!shareDestination?.submissions_open;
    if (!canSubmit) setActiveStep('done');
  }, [activeStep, shareDestination, receiptId, loading, destinationLoading]);

  // A Submit deep-link with no packaged share has nothing to act on; send the
  // user back to the start rather than rendering a dead-end Submit bound to a
  // null share id. In the normal flow `packagedShareId` is always set before we
  // advance to Submit, so this only fires on a stale/partial deep-link.
  useEffect(() => {
    if (activeStep === 'submit' && !packagedShareId) setActiveStep('queue');
  }, [activeStep, packagedShareId]);

  // =================================================
  // Step 5: Done actions
  // =================================================

  const handleDownloadZip = async () => {
    if (!packagedShareId) return;
    try {
      await api.shares.download(packagedShareId, { aiPii: aiPiiEnabled });
      toast('Download started', 'success');
    } catch (err: unknown) {
      toast(err instanceof Error ? err.message : 'Download failed', 'error');
    }
  };

  const handleSubmitComplete = (receipt: string, status?: string | null, support?: string | null) => {
    setReceiptId(receipt);
    setHostedStatus(status || null);
    if (support) setSupportContact(support);
    setCompletedKeys((prev) => new Set([...prev, 'submit']));
    setActiveStep('done');
    try { reload(); } catch { /* ignore */ }
  };

  // =================================================
  // Render
  // =================================================

  if (loading) {
    return <div style={{ padding: '32px 24px 48px', maxWidth: SHARE_SHELL_WIDTH, margin: '0 auto' }}>
      <Spinner text="Loading share data..." />
    </div>;
  }

  // Drop the Submit pill when hosted submission isn't available, so the stepper
  // doesn't advertise a step the flow will skip (Package → Done). Only filter
  // once the destination probe has resolved — show the full STEPS while loading
  // to avoid a flicker, matching the routing effects above. The STEPS const
  // itself is unchanged (helpers.ts still uses it for full-flow step indexing).
  const canSubmit = !!shareDestination?.daemon_upload_supported && !!shareDestination?.submissions_open;
  const visibleSteps = (!destinationLoading && !canSubmit)
    ? STEPS.filter(s => s.key !== 'submit')
    : STEPS;

  const stepperHeader = (
    <Stepper
      steps={visibleSteps}
      activeKey={activeStep}
      completedKeys={completedKeys}
      onStepClick={onStepClick}
    />
  );

  // =====================================================
  // STEP 1: QUEUE
  // =====================================================
  if (activeStep === 'queue') {
    return (
      <QueueStep
        stepperHeader={stepperHeader}
        readyStats={readyStats}
        shares={shares}
        candidates={candidates}
        scoringBackend={scoringBackend}
        queueOrder={queueOrder}
        queuedSessions={queuedSessions}
        note={note}
        setNote={setNote}
        aiPiiEnabled={aiPiiEnabled}
        setAiPiiEnabled={updateAiPiiEnabled}
        onRemove={removeFromQueue}
        onAdd={addToQueue}
        onReorder={reorderQueue}
        onHelp={() => setShowHelp(true)}
        onContinue={handleStartRedaction}
        drawerSessionId={drawerSessionId}
        setDrawerSessionId={setDrawerSessionId}
        showAddTraces={showAddTraces}
        setShowAddTraces={setShowAddTraces}
        searchQuery={searchQuery}
        setSearchQuery={setSearchQuery}
        sourceFilter={sourceFilter}
        setSourceFilter={setSourceFilter}
        projectFilter={projectFilter}
        setProjectFilter={setProjectFilter}
        scoreFilter={scoreFilter}
        setScoreFilter={setScoreFilter}
        dateFilter={dateFilter}
        setDateFilter={setDateFilter}
        reload={reload}
        globalStyles={globalStyles}
        showHelp={showHelp}
        setShowHelp={setShowHelp}
        toast={toast}
      />
    );
  }

  // =====================================================
  // STEP 2: REDACT
  // =====================================================
  if (activeStep === 'redact') {
    return (
      <RedactStep
        stepperHeader={stepperHeader}
        queuedSessions={queuedSessions}
        redactedSessions={redactedSessions}
        allDone={redactAllDone}
        aiPiiEnabled={aiPiiEnabled}
        onBack={() => setActiveStep('queue')}
        onContinue={goToReview}
        globalStyles={globalStyles}
        showHelp={showHelp}
        setShowHelp={setShowHelp}
      />
    );
  }

  // =====================================================
  // STEP 3: REVIEW
  // =====================================================
  if (activeStep === 'review') {
    return (
      <ReviewStep
        stepperHeader={stepperHeader}
        queuedSessions={queuedSessions}
        redactedSessions={redactedSessions}
        approvedIds={approvedIds}
        expandedIds={expandedReviewIds}
        aiPiiEnabled={aiPiiEnabled}
        onToggleExpand={toggleReviewExpand}
        onApprove={approveTrace}
        onApproveAllClean={approveAllClean}
        onRemove={removeFromQueue}
        onRetryAi={retryAiReview}
        onBack={() => setActiveStep('redact')}
        onPackage={handleStartPackage}
        globalStyles={globalStyles}
        showHelp={showHelp}
        setShowHelp={setShowHelp}
      />
    );
  }

  // =====================================================
  // STEP 4: PACKAGE
  // =====================================================
  if (activeStep === 'package') {
    return (
      <PackageStep
        stepperHeader={stepperHeader}
        approvedCount={queuedSessions.filter((s) => approvedIds.has(s.session_id)).length}
        approvedList={queuedSessions.filter((s) => approvedIds.has(s.session_id))}
        progress={packageProgress}
        log={packageLog}
        failed={packagingFailed}
        blockedSessions={blockedPackageSessions}
        onRetry={runPackage}
        onRemoveBlockedAndRetry={removeBlockedAndRetry}
        onBack={() => setActiveStep('review')}
        globalStyles={globalStyles}
      />
    );
  }

  // =====================================================
  // STEP 5: SUBMIT
  // =====================================================
  if (activeStep === 'submit') {
    return (
      <SubmitStep
        stepperHeader={stepperHeader}
        shareId={packagedShareId}
        bundle={bundleInfo}
        shareDestination={shareDestination}
        aiPiiEnabled={aiPiiEnabled}
        onSubmitted={handleSubmitComplete}
        onDownloadZip={handleDownloadZip}
        globalStyles={globalStyles}
        toast={toast}
      />
    );
  }

  // =====================================================
  // STEP 6: DONE
  // =====================================================
  if (activeStep === 'done') {
    return (
      <DoneStep
        stepperHeader={stepperHeader}
        bundle={bundleInfo}
        receiptId={receiptId}
        hostedStatus={hostedStatus}
        supportContact={supportContact || shareDestination?.support_contact || null}
        onDownloadAgain={handleDownloadZip}
        onNew={() => { startFreshShare(); reload(); }}
        globalStyles={globalStyles}
        shareDestination={shareDestination}
        destinationLoading={destinationLoading}
        destinationFailed={destinationFailed}
        onRetryDestination={loadShareDestination}
      />
    );
  }

  return null;
}
