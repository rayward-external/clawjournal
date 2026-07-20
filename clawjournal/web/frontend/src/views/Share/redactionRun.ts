export interface RedactionRun {
  controller: AbortController;
}

export interface RedactionRunSlot {
  current: RedactionRun | null;
}

export interface RedactionRetrySlot {
  current: Map<string, RedactionRun>;
}

export function beginRedactionRun(slot: RedactionRunSlot): RedactionRun | null {
  if (slot.current) return null;
  const run = { controller: new AbortController() };
  slot.current = run;
  return run;
}

export function cancelRedactionRun(slot: RedactionRunSlot): void {
  const run = slot.current;
  slot.current = null;
  run?.controller.abort();
}

export function isRedactionRunActive(slot: RedactionRunSlot, run: RedactionRun): boolean {
  return slot.current === run && !run.controller.signal.aborted;
}

export function finishRedactionRun(slot: RedactionRunSlot, run: RedactionRun): void {
  if (slot.current === run) slot.current = null;
}

export function beginRedactionRetry(slot: RedactionRetrySlot, sessionId: string): RedactionRun | null {
  if (slot.current.has(sessionId)) return null;
  const run = { controller: new AbortController() };
  slot.current.set(sessionId, run);
  return run;
}

export function cancelRedactionRetry(slot: RedactionRetrySlot, sessionId: string): void {
  const run = slot.current.get(sessionId);
  slot.current.delete(sessionId);
  run?.controller.abort();
}

export function cancelRedactionRetries(slot: RedactionRetrySlot): void {
  const runs = [...slot.current.values()];
  slot.current.clear();
  runs.forEach((run) => run.controller.abort());
}

export function isRedactionRetryActive(
  slot: RedactionRetrySlot,
  sessionId: string,
  run: RedactionRun,
): boolean {
  return slot.current.get(sessionId) === run && !run.controller.signal.aborted;
}

export function finishRedactionRetry(
  slot: RedactionRetrySlot,
  sessionId: string,
  run: RedactionRun,
): void {
  if (slot.current.get(sessionId) === run) slot.current.delete(sessionId);
}

export function settlePendingRedactionEntries<T extends { loading: boolean }>(
  entries: Record<string, T>,
  sessionIds: readonly string[],
  failedEntry: T,
): Record<string, T> {
  const next = { ...entries };
  for (const sessionId of sessionIds) {
    if (!next[sessionId] || next[sessionId].loading) next[sessionId] = failedEntry;
  }
  return next;
}
