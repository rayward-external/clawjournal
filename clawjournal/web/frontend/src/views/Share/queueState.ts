const IDS_PARAM = 'ids';
const SELECTION_PARAM = 'selection';
const EXCLUDE_PARAM = 'exclude';
const LEGACY_EXCLUDE_PARAM = 'exclude_ids';
const QUEUE_REF_PARAM = 'queue_ref';
const STORAGE_PREFIX = 'clawjournal.share.queue.';

// Python's loopback HTTP server rejects request lines above 65,536 bytes.
// Leave headroom for the path and the Share workflow's other query params.
export const MAX_QUEUE_QUERY_LENGTH = 48_000;

export interface QueueSelectionStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

function parseIds(value: string | null): string[] {
  return value ? value.split(',').filter(Boolean) : [];
}

function storageKey(ref: string): string {
  return `${STORAGE_PREFIX}${ref}`;
}

function parseStoredQueue(
  params: URLSearchParams,
  storage: QueueSelectionStorage | null,
): string[] | null {
  const ref = params.get(QUEUE_REF_PARAM);
  if (!ref || !/^[A-Za-z0-9_-]{1,128}$/.test(ref) || !storage) return null;
  try {
    const raw = storage.getItem(storageKey(ref));
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed) || !parsed.every((id) => typeof id === 'string')) return null;
    return parsed.filter(Boolean);
  } catch {
    return null;
  }
}

function createQueueRef(): string {
  try {
    if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  } catch { /* fall through */ }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

function replaceQueueParams(target: URLSearchParams, source: URLSearchParams): void {
  target.delete(IDS_PARAM);
  target.delete(SELECTION_PARAM);
  target.delete(EXCLUDE_PARAM);
  target.delete(LEGACY_EXCLUDE_PARAM);
  target.delete(QUEUE_REF_PARAM);
  source.forEach((value, key) => target.set(key, value));
}

/**
 * Restore queue membership from the URL once the eligible default queue is
 * known. `null` means the URL does not carry an explicit selection.
 */
export function queueSelectionFromSearchParams(
  params: URLSearchParams,
  defaultQueue: string[],
  storage: QueueSelectionStorage | null = null,
): string[] | null {
  // `has` intentionally distinguishes an explicit empty queue (`ids=`) from
  // an absent parameter, which means "use the default".
  if (params.has(IDS_PARAM)) return parseIds(params.get(IDS_PARAM));

  if (
    params.get(SELECTION_PARAM) === 'all'
    || params.has(EXCLUDE_PARAM)
    || params.has(LEGACY_EXCLUDE_PARAM)
  ) {
    const excluded = new Set([
      ...parseIds(params.get(EXCLUDE_PARAM)),
      ...parseIds(params.get(LEGACY_EXCLUDE_PARAM)),
    ]);
    return defaultQueue.filter((id) => !excluded.has(id));
  }

  // A reference is an explicit selection even when its local entry was
  // cleared, evicted, or opened in another browser. Fail closed instead of
  // treating an unreadable reference as "no selection" and defaulting to all.
  if (params.has(QUEUE_REF_PARAM)) return parseStoredQueue(params, storage) ?? [];

  return null;
}

/** Return whether an explicit URL selection can be restored exactly. */
export function isQueueSelectionRestorable(
  params: URLSearchParams,
  storage: QueueSelectionStorage | null = null,
): boolean {
  if (params.has(IDS_PARAM)) return true;
  if (params.has(QUEUE_REF_PARAM)) return parseStoredQueue(params, storage) !== null;
  return params.get(SELECTION_PARAM) === 'all';
}

/**
 * Persist the queue using the shorter lossless representation.
 *
 * A default or near-default queue is encoded as `selection=all` plus the few
 * exclusions. Explicit ids remain available for small subsets and custom
 * reorderings. This keeps the normal opt-out flow compact even when thousands
 * of sessions are eligible.
 */
export function syncQueueSelectionToSearchParams(
  params: URLSearchParams,
  queueOrder: string[],
  defaultQueue: string[] | null,
  storage: QueueSelectionStorage | null = null,
  refFactory: () => string = createQueueRef,
): void {
  const existingRef = params.get(QUEUE_REF_PARAM);
  const encoded = new URLSearchParams();

  if (defaultQueue === null) {
    encoded.set(IDS_PARAM, queueOrder.join(','));
  } else {
    const selected = new Set(queueOrder);
    const defaultIds = new Set(defaultQueue);
    const canonicalSelection = defaultQueue.filter((id) => selected.has(id));
    const preservesDefaultOrder = (
      queueOrder.length === canonicalSelection.length
      && queueOrder.every((id, index) => (
        defaultIds.has(id) && canonicalSelection[index] === id
      ))
    );
    const excluded = defaultQueue.filter((id) => !selected.has(id));
    const explicitCsv = queueOrder.join(',');
    const excludedCsv = excluded.join(',');

    // Exclusions are lossless while selected rows retain the default order.
    // Otherwise keep explicit ids so drag-reordering survives a reload.
    if (preservesDefaultOrder && excludedCsv.length < explicitCsv.length) {
      encoded.set(SELECTION_PARAM, 'all');
      if (excludedCsv) encoded.set(EXCLUDE_PARAM, excludedCsv);
    } else {
      encoded.set(IDS_PARAM, explicitCsv);
    }
  }

  if (encoded.toString().length <= MAX_QUEUE_QUERY_LENGTH) {
    replaceQueueParams(params, encoded);
    if (existingRef && storage) {
      try { storage.removeItem(storageKey(existingRef)); } catch { /* ignore */ }
    }
    return;
  }

  // Arbitrary orderings and large middle-sized subsets cannot be represented
  // safely in a request URL. Store their exact local-only state behind a short
  // opaque reference; session ids are local identifiers and the workbench URL
  // is only meaningful on this machine.
  if (storage) {
    const ref = existingRef || refFactory();
    try {
      storage.setItem(storageKey(ref), JSON.stringify(queueOrder));
      const stored = new URLSearchParams();
      stored.set(QUEUE_REF_PARAM, ref);
      replaceQueueParams(params, stored);
      return;
    } catch { /* use the compact fallback below */ }
  }

  // Storage can be unavailable in locked-down browser contexts or when its
  // quota is exhausted. Fail closed: a reload may lose the oversized queue,
  // but it must never broaden the user's selection to every eligible trace.
  const fallback = new URLSearchParams();
  fallback.set(IDS_PARAM, '');
  replaceQueueParams(params, fallback);
}
