import { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { useLocation, useSearchParams } from 'react-router-dom';
import type { Session, Share as ShareType } from '../../types.ts';
import { api, ApiError } from '../../api.ts';
import { useToast } from '../../components/Toast.tsx';
import { Spinner } from '../../components/Spinner.tsx';
import { Stepper } from '../../components/Stepper.tsx';
import {
  LARGE_BUNDLE_CONFIRM_THRESHOLD,
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
  hasLockedQueueSelection,
  parseStep,
  queueFromStats,
  sanitizeQueueSelection,
  sessionTotalTokens,
} from './helpers.ts';
import {
  isQueueSelectionRestorable,
  queueSelectionFromSearchParams,
  syncQueueSelectionToSearchParams,
} from './queueState.ts';
import type { QueueSelectionStorage } from './queueState.ts';
import {
  beginRedactionRetry,
  beginRedactionRun,
  cancelRedactionRetries,
  cancelRedactionRun,
  finishRedactionRetry,
  finishRedactionRun,
  isRedactionRetryActive,
  isRedactionRunActive,
  settlePendingRedactionEntries,
} from './redactionRun.ts';
import type { RedactionRetrySlot, RedactionRun } from './redactionRun.ts';
import { SHARE_SHELL_WIDTH, globalStyles } from './styles.tsx';
import { QueueStep } from './QueueStep.tsx';
import { RedactStep } from './RedactStep.tsx';
import { ReviewStep } from './ReviewStep.tsx';
import { PackageStep } from './PackageStep.tsx';
import { SubmitStep } from './SubmitStep.tsx';
import { DoneStep } from './DoneStep.tsx';

function stepConsumesUnpackagedQueue(step: StepKey, packagedShareId: string | null): boolean {
  return step === 'redact'
    || step === 'review'
    || (step === 'package' && !packagedShareId);
}

function hasRestorableLockedQueueSelection(
  params: URLSearchParams,
  storage: QueueSelectionStorage | null,
): boolean {
  return hasLockedQueueSelection(params)
    && isQueueSelectionRestorable(params, storage);
}

function safeStepFromParams(
  params: URLSearchParams,
  storage: QueueSelectionStorage | null,
): StepKey {
  const requested = parseStep(params.get('step'));
  return stepConsumesUnpackagedQueue(requested, params.get('share'))
    && !hasRestorableLockedQueueSelection(params, storage)
    ? 'queue'
    : requested;
}

function restoredQueueFromParams(
  stats: ShareReadyStats,
  params: URLSearchParams,
  storage: QueueSelectionStorage | null,
): string[] {
  const defaultQueue = queueFromStats(stats);
  const restored = queueSelectionFromSearchParams(params, defaultQueue, storage);
  return sanitizeQueueSelection(stats, restored ?? defaultQueue);
}

const PACKAGE_LOG_TRACE_LIMIT = 20;
const PACKAGE_ANIMATION_MAX_MS = 10_000;

export interface ShareProps {
  onSubmittedShareChange?: (shareId: string | null) => void;
}

export function Share({ onSubmittedShareChange }: ShareProps = {}) {
  const { toast } = useToast();
  const location = useLocation();
  const [searchParams, setSearchParams] = useSearchParams();
  const latestSearchParamsRef = useRef(searchParams);
  latestSearchParamsRef.current = searchParams;
  const queueStorage = useMemo(() => {
    try { return window.localStorage; } catch { return null; }
  }, []);
  const internalSearchRef = useRef<string | null>(null);
  const skipNextUrlSyncRef = useRef(false);

  const [activeStep, setActiveStep] = useState<StepKey>(
    () => safeStepFromParams(searchParams, queueStorage),
  );
  const [completedKeys, setCompletedKeys] = useState<Set<string>>(() => {
    const step = safeStepFromParams(searchParams, queueStorage);
    return completedKeysForStep(step);
  });

  const [readyStats, setReadyStats] = useState<ShareReadyStats | null>(null);
  const [shares, setShares] = useState<ShareType[]>([]);

  // Queue state: ordered list (drag-reorder) with a derived Set for lookups.
  const [queueOrder, setQueueOrder] = useState<string[]>(() => {
    if (!searchParams.has('ids') && !searchParams.has('queue_ref')) return [];
    return queueSelectionFromSearchParams(searchParams, [], queueStorage) || [];
  });
  // Explicit URL ids can only be trusted after the share-ready response tells
  // us which sessions are still eligible. Keep the raw order while loading,
  // then sanitize and deduplicate it against that server-filtered set.
  const [selectionInitialized, setSelectionInitialized] = useState(() => (
    (searchParams.has('ids') || searchParams.has('queue_ref'))
    && isQueueSelectionRestorable(searchParams, queueStorage)
  ));
  const [selectionLocked, setSelectionLocked] = useState(
    () => safeStepFromParams(searchParams, queueStorage) !== 'queue'
      && hasRestorableLockedQueueSelection(searchParams, queueStorage),
  );
  const [confirmedLargeQueueIds, setConfirmedLargeQueueIds] = useState<Set<string> | null>(() => (
    safeStepFromParams(searchParams, queueStorage) !== 'queue'
      && hasRestorableLockedQueueSelection(searchParams, queueStorage)
      ? new Set(queueSelectionFromSearchParams(searchParams, [], queueStorage) || [])
      : null
  ));
  const queueSet = useMemo(() => new Set(queueOrder), [queueOrder]);

  const [note, setNote] = useState(() => searchParams.get('note') || '');
  const [aiPiiEnabled, setAiPiiEnabled] = useState(() => searchParams.get('ai_pii') === '1');
  const [drawerSessionId, setDrawerSessionId] = useState<string | null>(null);
  const [showAddTraces, setShowAddTraces] = useState(true);
  const [showHelp, setShowHelp] = useState(false);
  const [loading, setLoading] = useState(true);

  // Add-traces filter
  const [searchQuery, setSearchQuery] = useState('');
  const [sourceFilter, setSourceFilter] = useState('');
  const [projectFilter, setProjectFilter] = useState('');
  const [scoreFilter, setScoreFilter] = useState(0);
  const [dateFilter, setDateFilter] = useState('');

  const resetAddTracesPicker = useCallback(() => {
    setShowAddTraces(true);
    setSearchQuery('');
    setSourceFilter('');
    setProjectFilter('');
    setScoreFilter(0);
    setDateFilter('');
  }, []);

  // Redaction state
  const [redactedSessions, setRedactedSessions] = useState<Record<string, RedactedSessionData>>({});
  const redactionRunRef = useRef<RedactionRun | null>(null);
  const redactionRetryRef = useRef<RedactionRetrySlot>({ current: new Map() });
  const cancelRedaction = useCallback(() => {
    cancelRedactionRun(redactionRunRef);
  }, []);
  const cancelAiRetries = useCallback(() => {
    const sessionIds = [...redactionRetryRef.current.current.keys()];
    cancelRedactionRetries(redactionRetryRef.current);
    if (sessionIds.length === 0) return;
    setRedactedSessions((prev) => {
      let changed = false;
      const next = { ...prev };
      sessionIds.forEach((sessionId) => {
        const data = next[sessionId];
        if (data?.loading) {
          next[sessionId] = { ...data, loading: false };
          changed = true;
        }
      });
      return changed ? next : prev;
    });
  }, []);

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
  const [packageBlockReason, setPackageBlockReason] = useState<string | null>(null);
  const [installingScanners, setInstallingScanners] = useState(false);
  const [blockedPackageSessions, setBlockedPackageSessions] = useState<BlockedShareSession[]>([]);

  // Done state
  const [bundleInfo, setBundleInfo] = useState<{ traces: number; created: string; approxSize: string } | null>(null);
  const [receiptId, setReceiptId] = useState<string | null>(null);
  const [hostedStatus, setHostedStatus] = useState<string | null>(null);
  const [supportContact, setSupportContact] = useState<string | null>(null);

  useEffect(() => {
    onSubmittedShareChange?.(receiptId ? packagedShareId : null);
  }, [onSubmittedShareChange, packagedShareId, receiptId]);

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
      const latestSearchParams = latestSearchParamsRef.current;
      setReadyStats(stats);
      setShares(shareList);
      setScoringBackend(backend);
      setQueueOrder(restoredQueueFromParams(stats, latestSearchParams, queueStorage));
      setSelectionInitialized(true);
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
    if (selectionInitialized || !readyStats) return;
    setQueueOrder(restoredQueueFromParams(readyStats, searchParams, queueStorage));
    setSelectionInitialized(true);
  }, [readyStats, searchParams, selectionInitialized, queueStorage]);

  useEffect(() => {
    const currentSearch = location.search.startsWith('?') ? location.search.slice(1) : location.search;
    if (internalSearchRef.current === currentSearch) return;

    cancelRedaction();
    cancelAiRetries();
    internalSearchRef.current = currentSearch;
    skipNextUrlSyncRef.current = true;

    const requestedStep = parseStep(searchParams.get('step'));
    const lockedSelection = hasRestorableLockedQueueSelection(searchParams, queueStorage);
    const selected = readyStats
      ? restoredQueueFromParams(readyStats, searchParams, queueStorage)
      : isQueueSelectionRestorable(searchParams, queueStorage)
        ? queueSelectionFromSearchParams(searchParams, [], queueStorage) || []
        : [];
    // Before readyStats arrives, an exact non-empty carrier may provisionally
    // restore the step, but guardedActiveStep still prevents work. Once the
    // eligible set is known, an empty sanitized snapshot fails back to Queue.
    const usableLockedSelection = lockedSelection
      && (readyStats === null || selected.length > 0);
    const step = stepConsumesUnpackagedQueue(requestedStep, searchParams.get('share'))
      && !usableLockedSelection
      ? 'queue'
      : requestedStep;
    const downstreamSelectionLocked = step !== 'queue' && usableLockedSelection;
    setActiveStep(step);
    setCompletedKeys(completedKeysForStep(step));
    setSelectionLocked(downstreamSelectionLocked);
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

    setQueueOrder(selected);
    setConfirmedLargeQueueIds(downstreamSelectionLocked ? new Set(selected) : null);
    setSelectionInitialized(
      readyStats !== null || isQueueSelectionRestorable(searchParams, queueStorage),
    );

    // Reject stale/bookmarked downstream URLs that do not carry an exact
    // Queue snapshot. Keep its exact ids/ref for review, but never strip the
    // unlocked selection=all encoding used by the opt-out queue.
    const staleLockedMarker = step === 'queue'
      && searchParams.get('selection') === 'locked';
    if (step !== requestedStep || staleLockedMarker) {
      const next = new URLSearchParams(searchParams);
      next.delete('step');
      if (next.get('selection') === 'locked') next.delete('selection');
      internalSearchRef.current = next.toString();
      setSearchParams(next, { replace: true });
    }
  }, [location.search, readyStats, searchParams, queueStorage, cancelRedaction, cancelAiRetries, setSearchParams]);

  useEffect(() => {
    if (skipNextUrlSyncRef.current) {
      skipNextUrlSyncRef.current = false;
      return;
    }
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      const locked = selectionLocked && activeStep !== 'queue';
      if (activeStep === 'queue') next.delete('step');
      else next.set('step', activeStep);
      if (selectionInitialized) {
        const defaultQueue = !locked && readyStats ? queueFromStats(readyStats) : null;
        syncQueueSelectionToSearchParams(next, queueOrder, defaultQueue, queueStorage);
        if (locked) next.set('selection', 'locked');
      }
      if (note) next.set('note', note); else next.delete('note');
      if (aiPiiEnabled) next.set('ai_pii', '1'); else next.delete('ai_pii');
      if (packagedShareId) next.set('share', packagedShareId); else next.delete('share');
      internalSearchRef.current = next.toString();
      return next;
    }, { replace: true });
  }, [activeStep, queueOrder, selectionInitialized, selectionLocked, readyStats, queueStorage, note, aiPiiEnabled, packagedShareId, setSearchParams]);

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
  const confirmedLargeQueueCoversCurrent = confirmedLargeQueueIds !== null
    && queueOrder.every((id) => confirmedLargeQueueIds.has(id));
  const largeBundleNeedsConfirmation = queuedSessions.length > LARGE_BUNDLE_CONFIRM_THRESHOLD
    && !confirmedLargeQueueCoversCurrent;
  // Only a locked exact-ID snapshot can restore an unpackaged downstream step.
  // Existing packaged Submit/Done flows do not consume this queue and remain
  // reloadable without repeating redaction.
  const consumesUnpackagedQueue = stepConsumesUnpackagedQueue(activeStep, packagedShareId);
  const lockedSelectionReady = selectionLocked
    && selectionInitialized
    && readyStats !== null
    && queuedSessions.length > 0;
  const guardedActiveStep: StepKey = consumesUnpackagedQueue
    && (!lockedSelectionReady || largeBundleNeedsConfirmation)
    ? 'queue'
    : activeStep;
  const approvedSessions = useMemo(
    () => queuedSessions.filter((s) => approvedIds.has(s.session_id)),
    [queuedSessions, approvedIds],
  );

  // A locked deep link can only be validated after the eligible-session list
  // arrives. If every saved id has since become ineligible, revoke the lock
  // and stay on Queue. Do this without replaying the full external-navigation
  // reset, which would erase Package state whenever a normal reload refreshes
  // readyStats.
  useEffect(() => {
    if (
      readyStats === null
      || !selectionInitialized
      || !selectionLocked
      || !stepConsumesUnpackagedQueue(activeStep, packagedShareId)
      || queuedSessions.length > 0
    ) return;

    cancelRedaction();
    setSelectionLocked(false);
    setConfirmedLargeQueueIds(null);
    setCompletedKeys(completedKeysForStep('queue'));
    setActiveStep('queue');
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      next.delete('step');
      if (next.get('selection') === 'locked') next.delete('selection');
      internalSearchRef.current = next.toString();
      return next;
    }, { replace: true });
  }, [
    activeStep,
    cancelRedaction,
    packagedShareId,
    queuedSessions.length,
    readyStats,
    selectionInitialized,
    selectionLocked,
    setSearchParams,
  ]);

  useEffect(() => {
    if (!packagedShareId) return;
    let cancelled = false;
    api.shares.get(packagedShareId).then((share) => {
      if (cancelled) return;
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
    return () => { cancelled = true; };
  }, [packagedShareId, searchParams]);

  // =================================================
  // Queue actions
  // =================================================

  const removeFromQueue = (id: string) => {
    cancelAiRetries();
    const next = queueOrder.filter((x) => x !== id);
    setQueueOrder(next);
  };

  const addToQueue = (id: string) => {
    cancelAiRetries();
    setQueueOrder((prev) => {
      if (prev.includes(id)) return prev;
      const defaults = readyStats ? queueFromStats(readyStats) : [];
      const previousIds = new Set(prev);
      const defaultOrderedSubset = defaults.filter((sessionId) => previousIds.has(sessionId));
      const followsDefaultOrder = defaultOrderedSubset.length === prev.length
        && defaultOrderedSubset.every((sessionId, index) => sessionId === prev[index]);
      if (followsDefaultOrder && defaults.includes(id)) {
        previousIds.add(id);
        return defaults.filter((sessionId) => previousIds.has(sessionId));
      }
      return [...prev, id];
    });
  };

  // Batch selection helpers. With 1000s of eligible sessions, unchecking traces
  // one at a time is impractical, so the queue exposes select-all / deselect-all
  // controls (scoped to whatever filters are active in the picker).
  const clearQueue = () => {
    cancelAiRetries();
    setQueueOrder([]);
  };

  const addManyToQueue = (ids: string[]) => {
    if (ids.length === 0) return;
    cancelAiRetries();
    setQueueOrder((prev) => {
      const defaults = readyStats ? queueFromStats(readyStats) : [];
      const defaultIds = new Set(defaults);
      const selectedIds = new Set(prev);
      const newIds = ids.filter((id) => !selectedIds.has(id));
      if (newIds.length === 0) return prev;

      // Keep the compact default order only while the user has not manually
      // reordered the queue. Once the order is custom, batch additions must
      // behave like single additions and leave the existing sequence intact.
      const defaultOrderedSubset = defaults.filter((id) => selectedIds.has(id));
      const followsDefaultOrder = defaultOrderedSubset.length === prev.length
        && defaultOrderedSubset.every((id, index) => id === prev[index]);
      if (followsDefaultOrder && newIds.every((id) => defaultIds.has(id))) {
        newIds.forEach((id) => selectedIds.add(id));
        return defaults.filter((id) => selectedIds.has(id));
      }
      return [...prev, ...newIds];
    });
  };

  const removeManyFromQueue = (ids: string[]) => {
    if (ids.length === 0) return;
    cancelAiRetries();
    const drop = new Set(ids);
    setQueueOrder((prev) => prev.filter((id) => !drop.has(id)));
  };

  const updateAiPiiEnabled = (enabled: boolean) => {
    cancelRedaction();
    cancelAiRetries();
    setAiPiiEnabled(enabled);
    setSelectionLocked(false);
    setConfirmedLargeQueueIds(null);
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

  const returnToQueue = () => {
    cancelRedaction();
    setSelectionLocked(false);
    setConfirmedLargeQueueIds(null);
    setActiveStep('queue');
  };

  const lockQueueSelection = () => {
    setSelectionLocked(true);
  };

  const reorderQueue = (fromId: string, overId: string) => {
    if (fromId === overId) return;
    cancelAiRetries();
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
    cancelRedaction();
    cancelAiRetries();
    setQueueOrder(readyStats ? queueFromStats(readyStats) : []);
    setSelectionLocked(false);
    setConfirmedLargeQueueIds(null);
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
  }, [cancelRedaction, cancelAiRetries, readyStats, resetAddTracesPicker]);

  const onStepClick = (key: string) => {
    const k = key as StepKey;
    if (k === guardedActiveStep) return;
    cancelAiRetries();
    if (k === 'queue') { returnToQueue(); return; }
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

  // Cancel work that no longer belongs to the active selection. Aborting the
  // fetch stops the browser from waiting on in-flight reports; the per-run
  // identity checks below also prevent stale results and future batches.
  useEffect(() => {
    if (activeStep !== 'redact') cancelRedaction();
  }, [activeStep, cancelRedaction]);

  useEffect(() => {
    cancelRedaction();
  }, [queuedSessions, aiPiiEnabled, cancelRedaction]);

  useEffect(() => () => cancelRedaction(), [cancelRedaction]);

  useEffect(() => {
    if (activeStep !== 'review') cancelAiRetries();
  }, [activeStep, cancelAiRetries]);

  useEffect(() => {
    cancelAiRetries();
  }, [queuedSessions, aiPiiEnabled, cancelAiRetries]);

  useEffect(() => () => cancelRedactionRetries(redactionRetryRef.current), []);

  const runRedaction = useCallback(async () => {
    const run = beginRedactionRun(redactionRunRef);
    if (!run) return;
    const isActive = () => isRedactionRunActive(redactionRunRef, run);
    const sessions = queuedSessions;
    const cached = redactedSessions;
    const missing = sessions.filter((s) => {
      const data = cached[s.session_id];
      return !data || data.loading;
    });

    // mark missing ones as loading up-front so the per-trace list renders
    if (missing.length > 0) {
      setRedactedSessions((prev) => {
        if (!isActive()) return prev;
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
        if (!isActive()) throw new Error('Redaction canceled');
        try {
          return await api.sessions.redactionReport(sessionId, {
            aiPii: aiPiiEnabled,
            signal: run.controller.signal,
          });
        } catch (e) {
          lastErr = e;
          if (!isActive()) throw e;
          // A hard request deadline means the daemon or response path is wedged.
          // Retrying immediately would only double the bounded wait.
          if (e instanceof ApiError && e.status === 408) break;
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
        if (!isActive()) return;
        const report = await fetchReport(s.session_id);
        if (!isActive()) return;
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
        setRedactedSessions((prev) => {
          if (!isActive()) return prev;
          return {
            ...prev,
            [s.session_id]: {
              messages: msgs, loading: false,
              redactionCount: report.redaction_count,
              aiPiiFindings: report.ai_pii_findings || [],
              aiCoverage: report.ai_coverage || (aiPiiEnabled ? 'rules_only' : 'disabled'),
              buckets,
              trufflehogHits,
            },
          };
        });
      } catch (error) {
        // A browser deadline is a queue-level stop condition. Let the outer
        // loop abort its sibling request and settle every unstarted trace
        // instead of spending another full deadline on each later batch.
        if (error instanceof ApiError && error.status === 408) throw error;
        if (!isActive()) return;
        setRedactedSessions((prev) => {
          if (!isActive()) return prev;
          return {
            ...prev,
            [s.session_id]: {
              messages: [{ role: 'system', content: '(unable to load redacted content)' }],
              loading: false,
              redactionCount: 0,
              aiCoverage: aiPiiEnabled ? 'rules_only' : 'disabled',
              buckets: emptyBuckets(),
            },
          };
        });
      }
    };

    try {
      for (let i = 0; i < missing.length; i += REDACTION_CONCURRENCY) {
        if (!isActive()) break;
        const batch = missing.slice(i, i + REDACTION_CONCURRENCY);
        await Promise.all(batch.map(processOne));
      }
    } catch (error) {
      if (error instanceof ApiError && error.status === 408 && isActive()) {
        setRedactedSessions((prev) => settlePendingRedactionEntries(
          prev,
          missing.map((session) => session.session_id),
          {
            messages: [{ role: 'system', content: '(redaction preview timed out)' }],
            loading: false,
            redactionCount: 0,
            aiCoverage: aiPiiEnabled ? 'rules_only' : 'disabled',
            buckets: emptyBuckets(),
          },
        ));
        run.controller.abort();
        toast('Redaction preview timed out; stopped the remaining traces.', 'error');
      }
    } finally {
      finishRedactionRun(redactionRunRef, run);
    }
  }, [queuedSessions, redactedSessions, aiPiiEnabled, toast]);

  const handleStartRedaction = () => {
    // Confirmation covers this exact set and any later subset (for example,
    // removing a blocked trace during review), but never newly added traces.
    setConfirmedLargeQueueIds(new Set(queueOrder));
    lockQueueSelection();
    setCompletedKeys((prev) => new Set([...prev, 'queue']));
    setActiveStep('redact');
  };

  // if Redact step is the active step and there are sessions lacking data, kick it off
  useEffect(() => {
    if (guardedActiveStep !== 'redact') return;
    const anyMissing = queuedSessions.some((s) => !redactedSessions[s.session_id] || redactedSessions[s.session_id].loading);
    if (anyMissing) void runRedaction();
  }, [guardedActiveStep, queuedSessions, redactedSessions, runRedaction]);

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
    const run = beginRedactionRetry(redactionRetryRef.current, id);
    if (!run) return;
    const isActive = () => isRedactionRetryActive(redactionRetryRef.current, id, run);

    try {
      setRedactedSessions((prev) => ({
        ...prev,
        [id]: { ...(prev[id] || { messages: [] }), loading: true },
      }));

      // One automatic retry mirrors the initial-run policy.
      let report: Awaited<ReturnType<typeof api.sessions.redactionReport>> | null = null;
      for (let attempt = 0; attempt <= 1; attempt++) {
        if (!isActive()) return;
        try {
          report = await api.sessions.redactionReport(id, {
            aiPii: true,
            signal: run.controller.signal,
          });
          break;
        } catch (e) {
          if (!isActive()) return;
          if (e instanceof ApiError && e.status === 408) break;
          if (attempt === 0) await new Promise((r) => setTimeout(r, 800));
        }
      }
      if (!isActive()) return;
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
      if (!isActive()) return;
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
    } catch {
      if (isActive()) {
        toast('AI review retry failed', 'error');
        setRedactedSessions((prev) => ({
          ...prev,
          [id]: { ...(prev[id] || { messages: [] }), loading: false },
        }));
      }
    } finally {
      finishRedactionRetry(redactionRetryRef.current, id, run);
    }
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
    setPackageBlockReason(null);
    setBlockedPackageSessions([]);
    setPackageProgress(0);
    setPackageLog('Allocating bundle...');
    setCompletedKeys((prev) => new Set([...prev, 'queue', 'redact', 'review']));

    const approvedList = approvedSessions;
    const traceLogLines = approvedList
      .slice(0, PACKAGE_LOG_TRACE_LIMIT)
      .map((s) => `Adding ${s.session_id.slice(0, 10)}.jsonl...`);
    if (approvedList.length > PACKAGE_LOG_TRACE_LIMIT) {
      traceLogLines.push(`Adding ${approvedList.length - PACKAGE_LOG_TRACE_LIMIT} more traces...`);
    }
    const logLines = [
      'Allocating bundle...',
      'Writing manifest.json...',
      ...traceLogLines,
      aiPiiEnabled ? 'Running final AI PII review...' : 'Running final rules-only PII review...',
      'Running final secret scan...',
      'Sealing bundle...',
    ];

    const animStart = Date.now();
    const duration = Math.min(PACKAGE_ANIMATION_MAX_MS, 2200 + approvedList.length * 220);

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
      const expectedRevisions = Object.fromEntries(
        approvedList
          .filter((s): s is ReadySession & { revision_hash: string } => Boolean(s.revision_hash))
          .map((s) => [s.session_id, s.revision_hash]),
      );
      const { share_id } = await api.shares.create(
        ids,
        note || undefined,
        undefined,
        expectedRevisions,
      );

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
      const blockReason = err instanceof ApiError && typeof err.body.block_reason === 'string'
        ? err.body.block_reason
        : null;
      setBlockedPackageSessions(blockedSessionsFromError(err));
      setPackageBlockReason(blockReason);
      setPackagingFailed(msg);
      setPackageLog(`Failed: ${msg}`);
      toast(msg, 'error');
    } finally {
      packagingStartedRef.current = false;
    }
  }, [approvedSessions, note, toast, aiPiiEnabled]);

  const installScannersAndRetry = useCallback(async () => {
    if (installingScanners) return;
    setInstallingScanners(true);
    setPackageLog('Installing pinned local scanners...');
    try {
      await api.share.installScanners();
      toast('Local secret scanners installed', 'success');
      await runPackage();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Scanner installation failed';
      setPackageBlockReason('scanner-not-installed');
      setPackagingFailed(msg);
      setPackageLog(`Failed: ${msg}`);
      toast(msg, 'error');
    } finally {
      setInstallingScanners(false);
    }
  }, [installingScanners, runPackage, toast]);

  const handleStartPackage = () => {
    if (queuedSessions.length === 0) return;
    cancelAiRetries();
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
    if (guardedActiveStep === 'package' && !packagingStartedRef.current && !packagedShareId) {
      void runPackage();
    }
  }, [guardedActiveStep, packagedShareId, runPackage]);

  // Belt-and-suspenders: advance to Done once the share id lands and the
  // animation has finished. Runs even if the inline `setActiveStep('done')`
  // inside `runPackage` fell through a silent error path.
  useEffect(() => {
    // Wait for the destination probe before deciding submit-vs-done. The probe
    // loads independently of the page now, so a null `shareDestination` here may
    // just mean "still loading" — routing to Done on that would strand a
    // genuinely-submittable bundle, since this effect can't re-route once
    // `activeStep` leaves 'package'.
    if (guardedActiveStep === 'package' && packagedShareId && packageProgress >= 100 && !packagingFailed && !destinationLoading) {
      setCompletedKeys((prev) => new Set([...prev, 'package']));
      const canSubmit = !!shareDestination?.daemon_upload_supported && !!shareDestination?.submissions_open;
      setActiveStep(canSubmit ? 'submit' : 'done');
    }
  }, [guardedActiveStep, packagedShareId, packageProgress, packagingFailed, shareDestination, destinationLoading]);

  // Reload/deep-link robustness: a page reload or bookmark can land directly on
  // Submit. If hosted submission isn't available (disabled, closed, or the
  // destination failed to load) and the share hasn't already been submitted,
  // fall back to the download-only Done view instead of stranding the user on a
  // Submit step they can't complete. Gated on `destinationLoading` so we wait
  // for the (now-independent) destination probe — a still-null destination only
  // after it resolves means not-submittable. Mirrors the package→done branch.
  useEffect(() => {
    if (guardedActiveStep !== 'submit' || receiptId || loading || destinationLoading) return;
    const canSubmit = !!shareDestination?.daemon_upload_supported && !!shareDestination?.submissions_open;
    if (!canSubmit) setActiveStep('done');
  }, [guardedActiveStep, shareDestination, receiptId, loading, destinationLoading]);

  // A Submit deep-link with no packaged share has nothing to act on; send the
  // user back to the start rather than rendering a dead-end Submit bound to a
  // null share id. In the normal flow `packagedShareId` is always set before we
  // advance to Submit, so this only fires on a stale/partial deep-link.
  useEffect(() => {
    if (guardedActiveStep === 'submit' && !packagedShareId) setActiveStep('queue');
  }, [guardedActiveStep, packagedShareId]);

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
    // Keep the current step even if it's 'submit' (e.g. a Submit deep-link
    // resolving to a non-submittable destination) so the stepper never
    // highlights nothing for the frame before the reroute to 'done'.
    ? STEPS.filter(s => s.key !== 'submit' || s.key === guardedActiveStep)
    : STEPS;

  const stepperHeader = (
    <Stepper
      steps={visibleSteps}
      activeKey={guardedActiveStep}
      completedKeys={completedKeys}
      onStepClick={onStepClick}
    />
  );

  // =====================================================
  // STEP 1: QUEUE
  // =====================================================
  if (guardedActiveStep === 'queue') {
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
        onClearAll={clearQueue}
        onAddMany={addManyToQueue}
        onRemoveMany={removeManyFromQueue}
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
  if (guardedActiveStep === 'redact') {
    return (
      <RedactStep
        stepperHeader={stepperHeader}
        queuedSessions={queuedSessions}
        redactedSessions={redactedSessions}
        allDone={redactAllDone}
        aiPiiEnabled={aiPiiEnabled}
        onBack={returnToQueue}
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
  if (guardedActiveStep === 'review') {
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
        onBack={() => {
          cancelAiRetries();
          setActiveStep('redact');
        }}
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
  if (guardedActiveStep === 'package') {
    return (
      <PackageStep
        stepperHeader={stepperHeader}
        approvedCount={approvedSessions.length}
        approvedList={approvedSessions}
        progress={packageProgress}
        log={packageLog}
        failed={packagingFailed}
        missingScanners={packageBlockReason === 'scanner-not-installed'}
        installingScanners={installingScanners}
        blockedSessions={blockedPackageSessions}
        onInstallScannersAndRetry={installScannersAndRetry}
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
  if (guardedActiveStep === 'submit') {
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
  if (guardedActiveStep === 'done') {
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
