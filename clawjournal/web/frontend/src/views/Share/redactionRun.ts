export interface RedactionRun {
  controller: AbortController;
}

export interface RedactionRunSlot {
  current: RedactionRun | null;
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
