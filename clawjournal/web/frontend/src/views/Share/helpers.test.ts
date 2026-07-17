import { describe, expect, it } from 'vitest';
import type { ReadySession, ShareReadyStats } from './types.ts';
import {
  hasLockedQueueSelection,
  queueFromSelectionParams,
  queueFromStats,
  writeQueueSelectionParams,
} from './helpers.ts';

export function readySession(id: string): ReadySession {
  return {
    session_id: id,
    project: 'project-a',
    model: 'gpt-test',
    source: 'codex',
    display_title: `Trace ${id}`,
    ai_quality_score: 4,
    ai_failure_value_score: 3,
    ai_recovery_labels: [],
    ai_failure_attribution: null,
    ai_failure_modes: [],
    ai_learning_summary: null,
    user_messages: 1,
    assistant_messages: 1,
    tool_uses: 0,
    input_tokens: 100,
    output_tokens: 50,
    outcome_badge: 'resolved',
    start_time: '2026-07-15T12:00:00Z',
    review_status: 'approved',
  };
}

export function readyStats(count = 12): ShareReadyStats {
  const sessions = Array.from({ length: count }, (_, index) => readySession(`s${index + 1}`));
  return {
    count: sessions.length,
    total_approved: sessions.length,
    projects: ['project-a'],
    models: ['gpt-test'],
    recommended_session_ids: ['s1'],
    sessions,
  };
}

describe('Share queue selection encoding', () => {
  it('defaults to every eligible trace, including queues larger than the old cap', () => {
    const stats = readyStats();
    expect(queueFromStats(stats)).toEqual(stats.sessions.map((session) => session.session_id));
    expect(queueFromSelectionParams(stats, new URLSearchParams())).toHaveLength(12);
  });

  it('keeps default and exclusion URLs compact while preserving explicit empty and ordered subsets', () => {
    const stats = readyStats();
    const defaults = queueFromStats(stats);

    const params = new URLSearchParams('ids=legacy');
    writeQueueSelectionParams(params, stats, defaults);
    expect(params.toString()).toBe('');

    writeQueueSelectionParams(params, stats, defaults.slice(1));
    expect(params.get('exclude_ids')).toBe('s1');
    expect(params.has('ids')).toBe(false);
    expect(queueFromSelectionParams(stats, params)).toEqual(defaults.slice(1));

    writeQueueSelectionParams(params, stats, []);
    expect(params.has('ids')).toBe(true);
    expect(params.get('ids')).toBe('');
    expect(params.has('exclude_ids')).toBe(false);
    expect(queueFromSelectionParams(stats, params)).toEqual([]);

    writeQueueSelectionParams(params, stats, ['s3', 's1']);
    expect(params.get('ids')).toBe('s3,s1');
    expect(queueFromSelectionParams(stats, params)).toEqual(['s3', 's1']);
  });

  it('deduplicates explicit ids and drops sessions that are no longer eligible', () => {
    const stats = readyStats(3);
    const params = new URLSearchParams('ids=s2,blocked,s2,s1,missing');

    expect(queueFromSelectionParams(stats, params)).toEqual(['s2', 's1']);
  });

  it('materializes and marks an exact downstream snapshot', () => {
    const stats = readyStats(3);
    const params = new URLSearchParams('exclude_ids=s3');

    writeQueueSelectionParams(params, stats, ['s1', 's2'], true);

    expect(params.toString()).toBe('ids=s1%2Cs2&selection=locked');
    expect(hasLockedQueueSelection(params)).toBe(true);
    expect(queueFromSelectionParams(readyStats(4), params)).toEqual(['s1', 's2']);

    params.delete('ids');
    expect(hasLockedQueueSelection(params)).toBe(false);
  });
});
