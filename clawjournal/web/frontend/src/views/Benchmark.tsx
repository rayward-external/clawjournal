import type React from 'react';
import { useState, useEffect, useCallback, useRef } from 'react';
import { Link } from 'react-router-dom';
import type { Benchmark as BM, BenchmarkSummary, BenchmarkTask, BenchmarkTrend } from '../types.ts';
import { api, ApiError } from '../api.ts';
import { Spinner, EmptyState } from '../components/Spinner.tsx';
import { useToast } from '../components/Toast.tsx';
import { colors, fontFamily, cardStyle, btnPrimary, btnSecondary, btnGhost, selectStyle } from '../theme.ts';

/* ------------------------------------------------------------------ */
/*  Vocabulary + helpers                                              */
/* ------------------------------------------------------------------ */

const READINESS: Record<string, { label: string; fg: string; bg: string }> = {
  ready: { label: 'ready', fg: colors.green700, bg: colors.green50 },
  needs_staging: { label: 'needs staging', fg: colors.yellow700, bg: colors.yellow50 },
  needs_review: { label: 'needs review', fg: colors.blue600, bg: colors.blue50 },
  local_only: { label: 'local only', fg: colors.red700, bg: colors.red50 },
  retired: { label: 'retired', fg: colors.gray500, bg: colors.gray100 },
};

const RISK_FG: Record<string, string> = { low: colors.gray500, medium: colors.yellow700, high: colors.red700 };

const EXPORT_KINDS: { kind: string; label: string }[] = [
  { kind: 'authoring_md', label: 'Authoring (.md)' },
  { kind: 'agent_packet_md', label: 'Agent packet (.md)' },
  { kind: 'grader_packet_md', label: 'Grader packet (.md)' },
  { kind: 'agent_packet_json', label: 'Agent packet (.json)' },
  { kind: 'grader_packet_json', label: 'Grader packet (.json)' },
];

function shortDate(iso: string | null | undefined): string {
  return iso ? String(iso).slice(0, 10) : '?';
}

function timeAgo(iso: string | null | undefined): string {
  if (!iso) return 'never';
  const then = new Date(String(iso).replace(' ', 'T')).getTime();
  if (Number.isNaN(then)) return shortDate(iso);
  const s = Math.max(0, (Date.now() - then) / 1000);
  if (s < 90) return 'just now';
  if (s < 5400) return `${Math.round(s / 60)}m ago`;
  if (s < 129600) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

function download(filename: string, content: string) {
  const blob = new Blob([content], { type: 'text/plain' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

function Chip({ children, fg, bg }: { children: React.ReactNode; fg: string; bg: string }) {
  return (
    <span style={{
      fontSize: 11, fontWeight: 600, color: fg, background: bg, padding: '2px 8px',
      borderRadius: 6, whiteSpace: 'nowrap',
    }}>{children}</span>
  );
}

/* ------------------------------------------------------------------ */
/*  Generation progress — slow run, keep it visibly alive             */
/* ------------------------------------------------------------------ */

// Maps the backend's human stage prose (clawjournal/benchmark/generate.py note()
// calls) to a percent, parsing "(k/total)" for smooth within-stage movement.
function stagePercent(stage: string): number {
  const s = (stage || '').toLowerCase();
  const m = s.match(/\((\d+)\s*\/\s*(\d+)\)/);
  const frac = m && Number(m[2]) > 0 ? Number(m[1]) / Number(m[2]) : 0;
  if (s.includes('finaliz') || s.includes('done')) return 100;
  if (s.includes('writing') || s.includes('review') || s.includes('design') || s.includes('critiqu')) return 55 + frac * 35; // 55→90
  if (s.includes('group') || s.includes('cluster')) return 50;
  if (s.includes('reading') || s.includes('deep-read')) return 10 + frac * 35; // 10→45
  return 8;
}

function GeneratingBanner({ stage, elapsedMs }: { stage: string; elapsedMs: number }) {
  const pct = stagePercent(stage);
  const secs = Math.floor(elapsedMs / 1000);
  const elapsed = secs < 60 ? `${secs}s` : `${Math.floor(secs / 60)}m ${secs % 60}s`;
  return (
    <div style={{ ...cardStyle, padding: 16, marginTop: 16, background: colors.primary50, borderColor: colors.primary200 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10 }}>
        <span style={{ fontSize: 14, fontWeight: 600, color: colors.primary700, animation: 'bench-pulse 1.8s ease-in-out infinite' }}>
          ⟳ Generating benchmark — {stage || 'starting…'}
        </span>
        <span style={{ fontSize: 12, color: colors.gray500 }}>{elapsed} elapsed</span>
      </div>
      <div style={{ height: 8, background: colors.primary100, borderRadius: 6, overflow: 'hidden' }}>
        <div style={{
          width: `${pct}%`, height: '100%', borderRadius: 6, transition: 'width 0.6s ease',
          backgroundImage: `linear-gradient(90deg, ${colors.primary400}, ${colors.primary200}, ${colors.primary400})`,
          backgroundSize: '200% 100%', animation: 'bench-shimmer 1.4s linear infinite',
        }} />
      </div>
      <div style={{ fontSize: 12, color: colors.gray500, marginTop: 8 }}>
        Runs the deep pipeline against your last 7 days of failures (~40+ model calls). A few minutes is normal — you can leave this tab and come back; it keeps running.
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Task detail — agent packet vs grader packet                       */
/* ------------------------------------------------------------------ */

function TaskDetail({ task }: { task: BenchmarkTask }) {
  const [packet, setPacket] = useState<'agent' | 'grader'>('agent');
  const [copied, setCopied] = useState(false);
  const r = READINESS[task.readiness] || READINESS.needs_review;

  const agentPrompt = `# ${task.title}\n\n## Scenario\n${task.scenario}\n\n## Seed inputs\n${task.seed_inputs}`;

  const copyPrompt = () => {
    navigator.clipboard.writeText(agentPrompt).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  };

  const seg = (key: 'agent' | 'grader', label: string) => (
    <button onClick={() => setPacket(key)} style={{
      padding: '5px 12px', fontSize: 13, fontWeight: 600, fontFamily, cursor: 'pointer',
      border: 'none', borderRadius: 6,
      background: packet === key ? colors.white : 'transparent',
      color: packet === key ? colors.gray800 : colors.gray500,
      boxShadow: packet === key ? '0 1px 2px rgba(0,0,0,0.08)' : 'none',
    }}>{label}</button>
  );

  return (
    <div style={{ padding: 20, overflow: 'auto' }}>
      <div style={{ fontSize: 17, fontWeight: 650, color: colors.gray900 }}>{task.id} — {task.title}</div>
      <div style={{ marginTop: 6, fontSize: 12, color: colors.gray500, display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
        <span>{(task.domains || []).join(' / ') || '—'}</span><span>·</span>
        <span>{task.difficulty}</span><span>·</span>
        <span>{task.points} pts</span><span>·</span>
        <span>{task.grading}</span>
        {(task.source_agents || []).map(a => <Chip key={a} fg={colors.gray600} bg={colors.gray100}>{a}</Chip>)}
      </div>
      <div style={{ marginTop: 10, display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
        <Chip fg={r.fg} bg={r.bg}>{r.label}</Chip>
        <Chip fg={RISK_FG[task.privacy_risk] || colors.gray500} bg={colors.gray50}>privacy: {task.privacy_risk}</Chip>
        <Chip fg={RISK_FG[task.leakage_risk] || colors.gray500} bg={colors.gray50}>leakage: {task.leakage_risk}</Chip>
        {task.critique?.discriminating ? <Chip fg={colors.green700} bg={colors.green50}>discriminating</Chip> : null}
        {task.critique?.gameable ? <Chip fg={colors.red700} bg={colors.red50}>gameable</Chip> : null}
      </div>

      <div style={{ marginTop: 16, display: 'inline-flex', gap: 2, background: colors.gray100, padding: 3, borderRadius: 8 }}>
        {seg('agent', 'Agent packet')}
        {seg('grader', 'Grader packet')}
      </div>

      {packet === 'agent' ? (
        <div style={{ marginTop: 14 }}>
          <button onClick={copyPrompt} style={{ ...btnSecondary, marginBottom: 12 }}>
            {copied ? '✓ Copied' : '⧉ Copy prompt'}
          </button>
          <Field label="Scenario" body={task.scenario} />
          <Field label="Seed inputs" body={task.seed_inputs} />
          <div style={{ marginTop: 8, fontSize: 11, color: colors.gray400 }}>
            The trap, ideal trajectory, pass criteria and grounded sessions are withheld here — switch to the grader packet to grade a run.
          </div>
        </div>
      ) : (
        <div style={{ marginTop: 14 }}>
          {task.critique?.staging_notes && (
            <div style={{ background: colors.yellow50, border: `1px solid ${colors.yellow200}`, color: colors.yellow700, padding: '8px 12px', borderRadius: 8, fontSize: 13, marginBottom: 12 }}>
              ⚠ Staging: {task.critique.staging_notes}
            </div>
          )}
          <Field label="The trap" body={task.the_trap} />
          {task.ideal_trajectory?.length > 0 && (
            <ListField label="Ideal trajectory" items={task.ideal_trajectory} ordered />
          )}
          {task.pass_criteria?.length > 0 && (
            <div style={{ marginBottom: 14 }}>
              <div style={labelStyle}>Pass criteria</div>
              {task.pass_criteria.map((c, i) => (
                <label key={i} style={{ display: 'flex', gap: 8, alignItems: 'flex-start', fontSize: 13, color: colors.gray700, padding: '2px 0' }}>
                  <input type="checkbox" style={{ marginTop: 3 }} /><span>{c}</span>
                </label>
              ))}
            </div>
          )}
          {task.fail_signals?.length > 0 && <ListField label="Fail signals" items={task.fail_signals} />}
          {task.grounded_session_ids?.length > 0 && (
            <div style={{ marginBottom: 8 }}>
              <div style={labelStyle}>Grounded sessions</div>
              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                {task.grounded_session_ids.map(sid => (
                  <Link key={sid} to={`/session/${encodeURIComponent(sid)}`} style={{ fontSize: 12, color: colors.blue600, fontFamily: 'monospace' }}>{sid.slice(0, 8)}</Link>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

const labelStyle: React.CSSProperties = { fontWeight: 600, fontSize: 11, color: colors.gray400, letterSpacing: '0.02em', marginBottom: 4, textTransform: 'uppercase' };

function Field({ label, body }: { label: string; body: string }) {
  if (!body) return null;
  return (
    <div style={{ marginBottom: 14 }}>
      <div style={labelStyle}>{label}</div>
      <div style={{ fontSize: 14, color: colors.gray800, lineHeight: 1.55, whiteSpace: 'pre-wrap' }}>{body}</div>
    </div>
  );
}

function ListField({ label, items, ordered }: { label: string; items: string[]; ordered?: boolean }) {
  return (
    <div style={{ marginBottom: 14 }}>
      <div style={labelStyle}>{label}</div>
      <ol style={{ margin: 0, paddingLeft: ordered ? 20 : 16, listStyle: ordered ? 'decimal' : 'disc' }}>
        {items.map((it, i) => <li key={i} style={{ fontSize: 13, color: colors.gray700, lineHeight: 1.5, marginBottom: 3 }}>{it}</li>)}
      </ol>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Trend heatmap                                                     */
/* ------------------------------------------------------------------ */

function TrendHeatmap({ trend }: { trend: BenchmarkTrend }) {
  const names = Object.keys(trend.themes);
  if (trend.runs.length < 1 || names.length === 0) {
    return <div style={{ fontSize: 13, color: colors.gray400, padding: 24 }}>Not enough history yet — generate a few weekly benchmarks to see which judgment failures recur.</div>;
  }
  const max = Math.max(1, ...names.flatMap(n => trend.themes[n]));
  const cell = (v: number) => {
    if (!v) return colors.gray50;
    const t = 0.18 + 0.62 * (v / max);
    return `rgba(180,125,8,${t.toFixed(2)})`; // amber scale
  };
  return (
    <div style={{ padding: 20, overflow: 'auto' }}>
      <div style={{ fontSize: 12, color: colors.gray500, marginBottom: 12 }}>Theme recurrence across your stored weekly benchmarks (oldest → newest). Darker = higher frequency.</div>
      <table style={{ borderCollapse: 'separate', borderSpacing: 3, fontFamily }}>
        <thead>
          <tr>
            <th style={{ textAlign: 'left' }}></th>
            {trend.runs.map(run => (
              <th key={run.benchmark_id} style={{ fontSize: 10, color: colors.gray400, fontWeight: 500, padding: '0 2px', whiteSpace: 'nowrap' }}>{shortDate(run.window_end).slice(5)}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {names.map(name => (
            <tr key={name}>
              <td style={{ fontSize: 12, color: colors.gray700, paddingRight: 12, whiteSpace: 'nowrap', maxWidth: 240, overflow: 'hidden', textOverflow: 'ellipsis' }}>{name}</td>
              {trend.themes[name].map((v, i) => (
                <td key={i} title={`${name}: ${v}`} style={{ width: 26, height: 22, background: cell(v), borderRadius: 4, textAlign: 'center', fontSize: 10, color: v ? colors.gray800 : 'transparent' }}>{v || ''}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Main view                                                         */
/* ------------------------------------------------------------------ */

export function Benchmark() {
  const { toast } = useToast();
  const [loading, setLoading] = useState(true);
  const [current, setCurrent] = useState<BM | null>(null);
  const [stale, setStale] = useState(false);
  const [list, setList] = useState<BenchmarkSummary[]>([]);
  const [trend, setTrend] = useState<BenchmarkTrend | null>(null);
  const [tab, setTab] = useState<'tasks' | 'themes' | 'trend'>('tasks');
  const [themeFilter, setThemeFilter] = useState<string | null>(null);
  const [selectedTask, setSelectedTask] = useState<string | null>(null);
  const [generating, setGenerating] = useState<{ id: string; stage: string } | null>(null);
  const [genStart, setGenStart] = useState<number | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const [exportOpen, setExportOpen] = useState(false);
  const pollRef = useRef<number | null>(null);

  // Tick a 1s clock while generating so the banner shows live elapsed time.
  useEffect(() => {
    if (!generating) return;
    setNow(Date.now());
    const t = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, [generating]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [latest, l, t] = await Promise.all([
        api.benchmarks.latest(),
        api.benchmarks.list(),
        api.benchmarks.trend(),
      ]);
      setCurrent(latest.benchmark);
      setStale(latest.stale);
      setList(l.benchmarks);
      setTrend(t);
      setSelectedTask(latest.benchmark?.tasks?.[0]?.id ?? null);
    } catch (e) {
      toast(e instanceof ApiError ? e.message : 'Failed to load benchmarks', 'error');
    } finally {
      setLoading(false);
    }
  }, [toast]);

  useEffect(() => {
    load();
    return () => { if (pollRef.current) window.clearInterval(pollRef.current); };
  }, [load]);

  // Attach the progress banner to a running generation and poll it to completion.
  // Shared by the Regenerate button and by adoption of a server-side run below.
  const beginPolling = useCallback((id: string, stage: string, startMs: number) => {
    if (pollRef.current) window.clearInterval(pollRef.current);
    setGenStart(startMs);
    setGenerating({ id, stage });
    let fails = 0;
    const stop = () => {
      if (pollRef.current) { window.clearInterval(pollRef.current); pollRef.current = null; }
      setGenerating(null);
      setGenStart(null);
    };
    pollRef.current = window.setInterval(async () => {
      try {
        const st = await api.benchmarks.status(id);
        fails = 0;
        if (st.status === 'generating') { setGenerating({ id, stage: st.stage || 'working…' }); return; }
        stop();
        if (st.status === 'failed') toast(`Generation failed: ${st.error || 'unknown'}`, 'error');
        else toast('Benchmark ready', 'success');
        load();
      } catch {
        // Tolerate transient blips, but don't spin forever on a persistent error.
        if (++fails >= 5) { stop(); toast('Lost contact with the generation — check the workbench daemon.', 'error'); }
      }
    }, 2000);
  }, [toast, load]);

  // Adopt a generation already running server-side — kicked off from the CLI,
  // another tab, or surviving a page reload — so the progress bar shows here too.
  useEffect(() => {
    if (generating || pollRef.current) return;
    const inflight = list.find(b => b.status === 'generating');
    if (!inflight) return;
    const parsed = Date.parse(inflight.generated_at);
    beginPolling(inflight.benchmark_id, inflight.stage || 'working…', Number.isNaN(parsed) ? Date.now() : parsed);
  }, [list, generating, beginPolling]);

  const selectWeek = async (id: string) => {
    if (id === current?.benchmark_id) return;
    try {
      const bm = await api.benchmarks.get(id);
      setCurrent(bm);
      setThemeFilter(null);
      setSelectedTask(bm.tasks?.[0]?.id ?? null);
    } catch {
      toast('Failed to load that benchmark', 'error');
    }
  };

  const regenerate = async () => {
    try {
      const res = await api.benchmarks.generate({});
      if (!res.benchmark_id) { toast(res.error || 'Could not start generation', 'error'); return; }
      beginPolling(res.benchmark_id, 'starting…', Date.now());
    } catch (e) {
      const msg = e instanceof ApiError ? (e.status === 409 ? 'A generation is already running' : e.message) : 'Could not start generation';
      toast(msg, 'error');
    }
  };

  const doExport = async (kind: string) => {
    setExportOpen(false);
    if (!current) return;
    try {
      const res = await api.benchmarks.export(current.benchmark_id, kind);
      const ext = kind.endsWith('json') ? 'json' : 'md';
      download(`benchmark-${current.benchmark_id}-${kind}.${ext}`, res.content);
      toast(`Exported (${res.pii_scan_hits} PII hits scanned)`, 'success');
    } catch {
      toast('Export failed', 'error');
    }
  };

  if (loading) return <div style={{ padding: 40 }}><Spinner text="Loading benchmarks…" /></div>;

  if (!current) {
    return (
      <div style={{ padding: 40 }}>
        <Header generating={generating} onRegenerate={regenerate} />
        {generating ? (
          <GeneratingBanner stage={generating.stage} elapsedMs={genStart ? now - genStart : 0} />
        ) : (
          <div style={{ marginTop: 24 }}>
            <EmptyState
              title="No benchmark yet"
              description="Generate a personalized benchmark from your last 7 days of agent failures. It runs the deep pipeline against your own traces — a few minutes, and stored locally."
              action={<button style={btnPrimary} onClick={regenerate} disabled={!!generating}>Generate benchmark</button>}
            />
          </div>
        )}
      </div>
    );
  }

  const themes = current.themes || [];
  const tasks = current.tasks || [];
  const shown = themeFilter ? tasks.filter(t => t.theme === themeFilter) : tasks;
  // Resolve against the FILTERED list so the detail pane never shows a task the
  // left rail isn't displaying (theme filter would otherwise desync the panes).
  const active = shown.find(t => t.id === selectedTask) || shown[0] || null;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div style={{ padding: '20px 24px 0' }}>
        <Header
          current={current} stale={stale} list={list} generating={generating}
          onRegenerate={regenerate} onSelectWeek={selectWeek}
          exportOpen={exportOpen} setExportOpen={setExportOpen} onExport={doExport}
        />
        {/* summary chips */}
        <div style={{ display: 'flex', gap: 14, marginTop: 14, fontSize: 12, color: colors.gray500, flexWrap: 'wrap' }}>
          <span>{themes.length} themes</span>
          <span>{current.n_tasks} tasks</span>
          <span>{current.total_points} pts</span>
          <span style={{ color: colors.green700 }}>{current.ready_count} ready</span>
          <span style={{ color: colors.yellow700 }}>{current.needs_staging_count} need staging</span>
          <span>{current.source_count} sessions deep-read{current.dropped_for_cost ? `, ${current.dropped_for_cost} dropped for cost` : ''}</span>
        </div>
        {generating && <GeneratingBanner stage={generating.stage} elapsedMs={genStart ? now - genStart : 0} />}
        {/* tabs */}
        <div style={{ display: 'flex', gap: 4, marginTop: 16, borderBottom: `1px solid ${colors.gray200}` }}>
          {(['tasks', 'themes', 'trend'] as const).map(t => (
            <button key={t} onClick={() => setTab(t)} style={{
              padding: '8px 14px', fontSize: 14, fontWeight: 600, fontFamily, cursor: 'pointer',
              background: 'none', border: 'none', textTransform: 'capitalize',
              color: tab === t ? colors.gray900 : colors.gray400,
              borderBottom: `2px solid ${tab === t ? colors.primary500 : 'transparent'}`, marginBottom: -1,
            }}>{t}</button>
          ))}
        </div>
      </div>

      <div style={{ flex: 1, overflow: 'hidden', padding: '0 24px 24px' }}>
        {tab === 'tasks' && (
          <div style={{ display: 'flex', gap: 16, height: '100%', paddingTop: 16 }}>
            {/* left rail: themes + tasks */}
            <div style={{ width: 300, flexShrink: 0, overflow: 'auto', ...cardStyle }}>
              <div style={{ padding: '10px 14px', borderBottom: `1px solid ${colors.gray100}` }}>
                <button onClick={() => setThemeFilter(null)} style={{ ...btnGhost, color: themeFilter ? colors.gray500 : colors.gray800, fontWeight: 600 }}>
                  All tasks ({tasks.length})
                </button>
              </div>
              {themes.map(th => (
                <div key={th.name}>
                  <button onClick={() => setThemeFilter(themeFilter === th.name ? null : th.name)} style={{
                    display: 'flex', justifyContent: 'space-between', width: '100%', padding: '8px 14px',
                    background: themeFilter === th.name ? colors.primary50 : 'transparent', border: 'none',
                    cursor: 'pointer', fontFamily, fontSize: 13, fontWeight: 600,
                    color: themeFilter === th.name ? colors.primary700 : colors.gray700, textAlign: 'left',
                  }}>
                    <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{th.name}</span>
                    <span style={{ color: colors.gray400, fontWeight: 500 }}>{th.frequency}</span>
                  </button>
                </div>
              ))}
              <div style={{ borderTop: `1px solid ${colors.gray100}`, marginTop: 4 }}>
                {shown.map(t => {
                  const r = READINESS[t.readiness] || READINESS.needs_review;
                  return (
                    <button key={t.id} onClick={() => setSelectedTask(t.id)} style={{
                      display: 'flex', justifyContent: 'space-between', alignItems: 'center', width: '100%',
                      gap: 8, padding: '9px 14px', background: active?.id === t.id ? colors.gray100 : 'transparent',
                      border: 'none', borderLeft: `2px solid ${active?.id === t.id ? colors.primary500 : 'transparent'}`,
                      cursor: 'pointer', fontFamily, textAlign: 'left',
                    }}>
                      <span style={{ fontSize: 13, color: colors.gray800, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        <span style={{ color: colors.gray400 }}>{t.id} </span>{t.title}
                      </span>
                      <span title={r.label} style={{ width: 8, height: 8, borderRadius: 4, background: r.fg, flexShrink: 0 }} />
                    </button>
                  );
                })}
              </div>
            </div>
            {/* right pane: task detail */}
            <div style={{ flex: 1, overflow: 'auto', ...cardStyle }}>
              {active ? <TaskDetail key={active.id} task={active} /> : <div style={{ padding: 24, color: colors.gray400 }}>No tasks.</div>}
            </div>
          </div>
        )}

        {tab === 'themes' && (
          <div style={{ ...cardStyle, marginTop: 16, overflow: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', fontFamily }}>
              <thead>
                <tr style={{ borderBottom: `1px solid ${colors.gray200}` }}>
                  {['Theme', 'Freq', 'Taxonomy', 'Lesson'].map(h => (
                    <th key={h} style={{ textAlign: 'left', padding: '10px 14px', fontSize: 11, color: colors.gray400, fontWeight: 600 }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {themes.map(th => (
                  <tr key={th.name} style={{ borderBottom: `1px solid ${colors.gray100}`, cursor: 'pointer' }}
                      onClick={() => { setThemeFilter(th.name); setTab('tasks'); }}>
                    <td style={{ padding: '10px 14px', fontSize: 13, color: colors.gray800, fontWeight: 600 }}>{th.name}</td>
                    <td style={{ padding: '10px 14px', fontSize: 13, color: colors.gray600 }}>{th.frequency}</td>
                    <td style={{ padding: '10px 14px', fontSize: 12, color: colors.gray500 }}>{(th.taxonomy || []).join(', ')}</td>
                    <td style={{ padding: '10px 14px', fontSize: 13, color: colors.gray600 }}>{th.lesson}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {tab === 'trend' && (
          <div style={{ ...cardStyle, marginTop: 16, overflow: 'auto' }}>
            {trend ? <TrendHeatmap trend={trend} /> : <div style={{ padding: 24, color: colors.gray400 }}>No trend data.</div>}
          </div>
        )}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  Header (window + controls)                                        */
/* ------------------------------------------------------------------ */

function Header(props: {
  current?: BM | null; stale?: boolean; list?: BenchmarkSummary[];
  generating: { id: string; stage: string } | null;
  onRegenerate: () => void; onSelectWeek?: (id: string) => void;
  exportOpen?: boolean; setExportOpen?: (v: boolean) => void; onExport?: (k: string) => void;
}) {
  const { current, stale, list, generating, onRegenerate, onSelectWeek, exportOpen, setExportOpen, onExport } = props;
  // Only offer ready runs — a generating/failed row would load as a blank benchmark.
  const readyRuns = (list || []).filter(b => b.status === 'ready');
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 16, flexWrap: 'wrap' }}>
      <div>
        <div style={{ fontSize: 22, fontWeight: 700, color: colors.gray900 }}>Benchmark</div>
        <div style={{ fontSize: 13, color: colors.gray500, marginTop: 2 }}>
          {current
            ? <>Week of {shortDate(current.window_start)} → {shortDate(current.window_end)} · generated {timeAgo(current.generated_at)} · {current.backend || 'agent'}</>
            : 'Personalized weekly benchmark from your own agent traces'}
        </div>
      </div>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
        {stale && <Chip fg={colors.yellow700} bg={colors.yellow50}>{'>'}7 days old</Chip>}
        {readyRuns.length > 1 && onSelectWeek && (
          <select value={current?.benchmark_id} onChange={e => onSelectWeek(e.target.value)} style={selectStyle}>
            {readyRuns.map(b => <option key={b.benchmark_id} value={b.benchmark_id}>{b.benchmark_id} ({shortDate(b.window_end)})</option>)}
          </select>
        )}
        <button style={generating ? { ...btnPrimary, opacity: 0.7, cursor: 'default' } : btnPrimary} onClick={onRegenerate} disabled={!!generating}>
          {generating ? '⟳ Generating…' : '⟳ Regenerate (last 7d)'}
        </button>
        {current && onExport && setExportOpen && (
          <div style={{ position: 'relative' }}>
            <button style={btnSecondary} onClick={() => setExportOpen(!exportOpen)}>⬇ Export ▾</button>
            {exportOpen && (
              <div style={{ position: 'absolute', right: 0, top: '110%', zIndex: 10, ...cardStyle, boxShadow: '0 4px 16px rgba(0,0,0,0.12)', minWidth: 180 }}>
                {EXPORT_KINDS.map(k => (
                  <button key={k.kind} onClick={() => onExport(k.kind)} style={{ display: 'block', width: '100%', textAlign: 'left', padding: '8px 14px', background: 'none', border: 'none', cursor: 'pointer', fontFamily, fontSize: 13, color: colors.gray700 }}>{k.label}</button>
                ))}
              </div>
            )}
          </div>
        )}
        <Chip fg={colors.gray500} bg={colors.gray100}>🔒 local only</Chip>
      </div>
    </div>
  );
}
