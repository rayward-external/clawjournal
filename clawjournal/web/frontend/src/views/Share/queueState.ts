const IDS_PARAM = 'ids';
const SELECTION_PARAM = 'selection';
const EXCLUDE_PARAM = 'exclude';

function parseIds(value: string | null): string[] {
  return value ? value.split(',').filter(Boolean) : [];
}

/**
 * Restore queue membership from the URL once the eligible default queue is
 * known. `null` means the URL does not carry an explicit selection.
 */
export function queueSelectionFromSearchParams(
  params: URLSearchParams,
  defaultQueue: string[],
): string[] | null {
  // `has` intentionally distinguishes an explicit empty queue (`ids=`) from
  // an absent parameter, which means "use the default".
  if (params.has(IDS_PARAM)) return parseIds(params.get(IDS_PARAM));

  if (params.get(SELECTION_PARAM) === 'all') {
    const excluded = new Set(parseIds(params.get(EXCLUDE_PARAM)));
    return defaultQueue.filter((id) => !excluded.has(id));
  }

  return null;
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
): void {
  params.delete(IDS_PARAM);
  params.delete(SELECTION_PARAM);
  params.delete(EXCLUDE_PARAM);

  if (defaultQueue === null) {
    params.set(IDS_PARAM, queueOrder.join(','));
    return;
  }

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

  // Exclusions are only lossless while the selected rows retain the default
  // order. Otherwise keep the explicit queue so drag-reordering survives a
  // reload. Choose ids for an empty or small subset because it is shorter.
  if (preservesDefaultOrder && excludedCsv.length < explicitCsv.length) {
    params.set(SELECTION_PARAM, 'all');
    if (excludedCsv) params.set(EXCLUDE_PARAM, excludedCsv);
    return;
  }

  params.set(IDS_PARAM, explicitCsv);
}
