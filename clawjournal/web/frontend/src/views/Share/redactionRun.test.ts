import { describe, expect, it } from 'vitest';
import {
  beginRedactionRun,
  cancelRedactionRun,
  clearInterruptedRedactionEntries,
  finishRedactionRun,
  isRedactionRunActive,
  settleFinishedRun,
  settlePendingRedactionEntries,
} from './redactionRun.ts';
import type { RedactionRunSlot } from './redactionRun.ts';

interface Entry {
  loading: boolean;
  tag?: string;
}

const loading = (): Entry => ({ loading: true });
const done = (tag: string): Entry => ({ loading: false, tag });

describe('clearInterruptedRedactionEntries', () => {
  it('drops traces a canceled run left mid-flight so the next run refetches them', () => {
    const entries: Record<string, Entry> = { s1: done('ok'), s2: loading() };

    const next = clearInterruptedRedactionEntries(entries, ['s1', 's2']);

    expect(next).not.toBe(entries);
    expect(next.s1).toEqual(done('ok'));
    expect('s2' in next).toBe(false);
  });

  it('returns the same map when nothing was left loading', () => {
    const entries: Record<string, Entry> = { s1: done('ok'), s2: done('ok') };

    // Identity matters: a fresh object here would re-trigger the kick-off
    // effect on every completed run and refetch traces that already settled.
    expect(clearInterruptedRedactionEntries(entries, ['s1', 's2'])).toBe(entries);
  });

  it('leaves traces outside the finished run untouched', () => {
    const entries: Record<string, Entry> = { s1: loading(), s2: loading() };

    const next = clearInterruptedRedactionEntries(entries, ['s1']);

    expect('s1' in next).toBe(false);
    expect(next.s2).toEqual(loading());
  });

  it('ignores ids with no entry', () => {
    const entries: Record<string, Entry> = { s1: done('ok') };

    expect(clearInterruptedRedactionEntries(entries, ['missing'])).toBe(entries);
  });

  it('hands back traces a canceled run abandoned, so the next run refetches', () => {
    const slot: RedactionRunSlot = { current: null };
    const run = beginRedactionRun(slot)!;
    const entries: Record<string, Entry> = { s1: done('ok'), s2: loading() };

    cancelRedactionRun(slot);
    expect(isRedactionRunActive(slot, run)).toBe(false);
    finishRedactionRun(slot, run);

    const next = settleFinishedRun(slot, entries, ['s1', 's2']);

    expect(next.s1).toEqual(done('ok'));
    expect('s2' in next).toBe(false);
    expect(Object.values(next).every((entry) => !entry.loading)).toBe(true);
  });

  it('leaves the map alone while a successor run owns the slot', () => {
    // The successor is already refetching these traces; clearing them here
    // would race it into a duplicate request.
    const slot: RedactionRunSlot = { current: null };
    const first = beginRedactionRun(slot)!;
    const entries: Record<string, Entry> = { s1: done('ok'), s2: loading() };

    cancelRedactionRun(slot);
    const second = beginRedactionRun(slot);
    finishRedactionRun(slot, first);

    expect(slot.current).toBe(second);
    expect(settleFinishedRun(slot, entries, ['s1', 's2'])).toBe(entries);
  });

  it('is a no-op for a run that completed normally', () => {
    const slot: RedactionRunSlot = { current: null };
    const run = beginRedactionRun(slot)!;
    const entries: Record<string, Entry> = { s1: done('ok'), s2: done('ok') };

    finishRedactionRun(slot, run);

    expect(settleFinishedRun(slot, entries, ['s1', 's2'])).toBe(entries);
  });
});

describe('settlePendingRedactionEntries', () => {
  it('settles only entries that never finished', () => {
    const entries: Record<string, Entry> = { s1: done('ok'), s2: loading() };

    const next = settlePendingRedactionEntries(entries, ['s1', 's2'], done('timeout'));

    expect(next.s1).toEqual(done('ok'));
    expect(next.s2).toEqual(done('timeout'));
  });
});
