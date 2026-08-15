import { describe, expect, it } from 'vitest';
import type { ReadySession, ShareReadyStats } from './types.ts';
import {
  classify,
  collectExpectedLogicalRevisions,
  expandLogicalQueueSelection,
  groupReadySessions,
  hasLockedQueueSelection,
  queueFromSelectionParams,
  queueFromStats,
  writeQueueSelectionParams,
} from './helpers.ts';

describe('Share review defaults', () => {
  it('auto-clears deterministic-only results when AI was intentionally disabled', () => {
    expect(classify({ messages: [], loading: false, aiCoverage: 'disabled' })).toBe('clear');
  });

  it('fails closed when an enabled AI pass was unavailable or uncertain', () => {
    expect(classify({ messages: [], loading: false, aiCoverage: 'rules_only' })).toBe('review');
    expect(classify({
      messages: [],
      loading: false,
      aiCoverage: 'full',
      aiPiiFindings: [{
        entity_type: 'person', entity_text: 'masked', confidence: 0.5, field: 'content', source: 'ai',
      }],
    })).toBe('review');
  });
});

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
  it('defaults to the first 50 ranked eligible traces', () => {
    const stats = readyStats(75);
    const expected = stats.sessions.slice(0, 50).map((session) => session.session_id);
    expect(queueFromStats(stats)).toEqual(expected);
    expect(queueFromSelectionParams(stats, new URLSearchParams())).toEqual(expected);
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

  it('caps explicit selections at 50 while retaining eligible traces outside the default queue', () => {
    const stats = readyStats(75);
    const reversed = stats.sessions.map((session) => session.session_id).reverse();
    const params = new URLSearchParams({ ids: reversed.join(',') });

    expect(queueFromSelectionParams(stats, params)).toEqual(reversed.slice(0, 50));
    expect(queueFromSelectionParams(stats, params)[0]).toBe('s75');
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

describe('logical conversation queue projection', () => {
  function checkpoint(id: string, segmentIndex: number, checkpointCount = 2): ReadySession {
    return {
      ...readySession(id),
      logical_session_id: 'conversation-1',
      logical_revision: 'logical-r1',
      checkpoint_count: checkpointCount,
      segment_index: segmentIndex,
      input_tokens: 10 * (segmentIndex + 1),
      output_tokens: 5 * (segmentIndex + 1),
    };
  }

  it('groups checkpoint rows once while retaining physical members for upload', () => {
    const groups = groupReadySessions([
      checkpoint('conversation-1_seg-0001', 1, 3),
      checkpoint('conversation-1', 0, 3),
    ]);

    expect(groups).toHaveLength(1);
    expect(groups[0].members.map((session) => session.session_id)).toEqual([
      'conversation-1',
      'conversation-1_seg-0001',
    ]);
    expect(groups[0].display.checkpoint_count).toBe(3);
    expect(groups[0].display.input_tokens).toBe(30);
    expect(groups[0].display.ai_failure_value_score).toBeNull();
    expect(groups[0].display.ai_quality_score).toBeNull();
    expect(groups[0].logical_incomplete).toBe(false);
  });

  it('expands a selected checkpoint to all currently eligible group members', () => {
    const sessions = [
      checkpoint('conversation-1', 0),
      checkpoint('conversation-1_seg-0001', 1),
      readySession('standalone'),
    ];
    const stats: ShareReadyStats = { ...readyStats(0), count: 3, sessions };

    expect(expandLogicalQueueSelection(stats, ['conversation-1_seg-0001'])).toEqual([
      'conversation-1',
      'conversation-1_seg-0001',
    ]);
  });

  it('does not split a checkpoint family at the 50-checkpoint default cap', () => {
    const standalone = Array.from({ length: 49 }, (_, index) => readySession(`s${index + 1}`));
    const sessions = [
      ...standalone,
      checkpoint('conversation-1', 0),
      checkpoint('conversation-1_seg-0001', 1),
      readySession('fits-after-group'),
    ];
    const stats: ShareReadyStats = { ...readyStats(0), count: sessions.length, sessions };

    expect(queueFromStats(stats)).toHaveLength(50);
    expect(queueFromStats(stats)).not.toContain('conversation-1');
    expect(queueFromStats(stats)).not.toContain('conversation-1_seg-0001');
    expect(queueFromStats(stats)).toContain('fits-after-group');
  });

  it('rejects conflicting logical revisions before packaging', () => {
    const first = checkpoint('conversation-1', 0);
    const second = { ...checkpoint('conversation-1_seg-0001', 1), logical_revision: 'logical-r2' };

    expect(() => collectExpectedLogicalRevisions([first, second])).toThrow(/changed while preparing/);
  });

  it('collects one logical precondition for multiple physical checkpoints', () => {
    expect(collectExpectedLogicalRevisions([
      checkpoint('conversation-1', 0),
      checkpoint('conversation-1_seg-0001', 1),
    ])).toEqual({ 'conversation-1': 'logical-r1' });
  });
});
