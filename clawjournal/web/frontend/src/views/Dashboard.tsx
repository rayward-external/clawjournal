import { useState, useEffect, useCallback } from 'react';
import type { DashboardData, InsightsData } from '../types.ts';
import { api } from '../api.ts';
import { LABELS } from '../components/BadgeChip.tsx';
import { Spinner } from '../components/Spinner.tsx';
import { useToast } from '../components/Toast.tsx';
import { colors } from '../theme.ts';

function formatNumber(n: number): string {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M';
  if (n >= 1_000) return (n / 1_000).toFixed(1) + 'K';
  return String(n);
}

function titleCase(s: string): string {
  return s.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
}

function labelFor(key: string): string {
  return LABELS[key] ?? titleCase(key);
}

/** Normalize raw model names to openrouter-style slugs: provider/model (lowercase). */
function shortModel(model: string): string {
  if (!model) return '';
  if (model === '<synthetic>') return 'synthetic';
  let raw = model.toLowerCase().trim();
  raw = raw.replace(/-\d{8}$/, '');  // strip date suffix like -20251001
  if (raw.includes('/')) return raw;  // already prefixed
  // Collapse trailing -N-M version segments into -N.M (claude-opus-4-6 → claude-opus-4.6)
  const claudeMatch = raw.match(/^(claude-[a-z]+)-(\d+)-(\d+)(.*)$/);
  if (claudeMatch) {
    const [, base, major, minor, rest] = claudeMatch;
    return `anthropic/${base}-${major}.${minor}${rest}`;
  }
  if (raw.startsWith('claude-')) return `anthropic/${raw}`;
  if (raw.startsWith('gpt-') || /^o\d/.test(raw)) return `openai/${raw}`;
  if (raw.startsWith('gemini-')) return `google/${raw}`;
  if (raw.startsWith('deepseek-')) return `deepseek/${raw}`;
  if (raw.startsWith('llama-')) return `meta-llama/${raw}`;
  if (raw.startsWith('mistral-')) return `mistralai/${raw}`;
  return raw;
}

function BarRow({ label, title, value, max, total, color = colors.blue400, fmt }: {
  label: string;
  title?: string;
  value: number;
  max: number;
  total?: number;
  color?: string;
  fmt?: (n: number) => string;
}) {
  const pct = max > 0 ? (value / max) * 100 : 0;
  const pctOfTotal = total && total > 0 ? ((value / total) * 100).toFixed(0) : null;
  const display = fmt ? fmt(value) : formatNumber(value);
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '2px 0' }}>
      <div style={{ width: 180, fontSize: 13, color: colors.gray700, flexShrink: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={title ?? label}>
        {label}
      </div>
      <div style={{ flex: 1, background: colors.gray100, borderRadius: 3, height: 14 }}>
        <div style={{ width: `${pct}%`, background: color, borderRadius: 3, height: 14, minWidth: pct > 0 ? 2 : 0, transition: 'width 0.3s ease' }} />
      </div>
      <div style={{ width: 72, fontSize: 13, color: colors.gray500, textAlign: 'right', flexShrink: 0 }}>
        {display}{pctOfTotal ? <span style={{ color: colors.gray400, fontSize: 11 }}> ({pctOfTotal}%)</span> : ''}
      </div>
    </div>
  );
}

function Section({ title, subtitle, children }: { title: string; subtitle?: string; children: React.ReactNode }) {
  return (
    <div style={{ border: `1px solid ${colors.gray200}`, borderRadius: 10, padding: '14px 16px', background: colors.white }}>
      <div style={{ marginBottom: 10 }}>
        <h3 style={{ fontSize: 13, fontWeight: 600, color: colors.gray700, margin: 0 }}>{title}</h3>
        {subtitle && <div style={{ fontSize: 11, color: colors.gray400, marginTop: 2 }}>{subtitle}</div>}
      </div>
      {children}
    </div>
  );
}

function StatCard({ label, value, sub, color = colors.gray800 }: { label: string; value: string; sub?: string; color?: string }) {
  return (
    <div style={{
      background: colors.white,
      border: `1px solid ${colors.gray200}`,
      borderRadius: 10,
      padding: '16px 20px',
      flex: 1,
      minWidth: 120,
    }}>
      <div style={{ fontSize: 24, fontWeight: 600, color, letterSpacing: '-0.02em' }}>{value}</div>
      <div style={{ fontSize: 13, color: colors.gray500, marginTop: 3 }}>{label}</div>
      {sub && <div style={{ fontSize: 11, color: colors.gray400, marginTop: 2 }}>{sub}</div>}
    </div>
  );
}

interface TriageStats {
  total: number;
  by_status: Record<string, number>;
}

const SCORE_COLORS = ['', colors.gray400, colors.yellow400, colors.blue400, colors.red400, colors.red500];
const SCORE_LABELS = ['', 'No signal', 'Weak signal', 'Usable signal', 'Strong pattern', 'Canonical'];
const PRODUCTIVITY_COLORS = ['', colors.red400, colors.yellow400, colors.gray400, colors.blue400, colors.green400];

// Keep as many trailing dash-segments as fit in the label column; full id stays in the hover title.
// Path separators and dashes-inside-folder-names look identical here, so we err on the side of
// showing more context rather than collapsing to a generic leaf like "page" or "pipeline".
const PROJECT_LABEL_BUDGET = 22;
function displayProject(project: string): string {
  const afterSource = project.includes(':') ? project.slice(project.indexOf(':') + 1) : project;
  if (afterSource.length <= PROJECT_LABEL_BUDGET) return afterSource;
  const segments = afterSource.split('-');
  let acc = segments[segments.length - 1];
  for (let i = segments.length - 2; i >= 0; i--) {
    const next = segments[i] + '-' + acc;
    if (next.length > PROJECT_LABEL_BUDGET) break;
    acc = next;
  }
  return acc;
}

function formatCost(c: number | null | undefined): string {
  if (c == null || c === 0) return '$0';
  if (c < 0.01) return `$${c.toFixed(4)}`;
  return `$${c.toFixed(2)}`;
}

function localDate(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

function getDateRange(range: string): { start: string | null; end: string | null } {
  if (range === 'all') {
    return { start: null, end: null };
  }
  const now = new Date();
  const end = localDate(now);
  if (range === '1d') {
    return { start: end, end };
  }
  if (range === '1w') {
    const d = new Date(now);
    d.setDate(d.getDate() - 7);
    return { start: localDate(d), end };
  }
  // 1m
  const d = new Date(now);
  d.setMonth(d.getMonth() - 1);
  return { start: localDate(d), end };
}

const HEATMAP_COLORS = ['#f3f0eb', '#e8d5b8', '#d4b88a', '#c49a5c', '#a87d3e', '#8a6020'];
function heatColor(count: number, max: number): string {
  if (count === 0 || max === 0) return HEATMAP_COLORS[0];
  const idx = Math.min(Math.ceil((count / max) * 5), 5);
  return HEATMAP_COLORS[idx];
}

export function Dashboard() {
  const { toast } = useToast();
  const [data, setData] = useState<DashboardData | null>(null);
  const [triage, setTriage] = useState<TriageStats | null>(null);
  const [insights, setInsights] = useState<InsightsData | null>(null);
  const [timeRange, setTimeRange] = useState<string>('1w');
  const [showAllMetrics, setShowAllMetrics] = useState(false);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const { start, end } = getDateRange(timeRange);
      const dashParams = start && end ? { start, end } : {};
      const [d, s] = await Promise.all([
        api.dashboard(dashParams),
        api.stats(dashParams),
      ]);
      setData(d);
      setTriage(s);
      setInsights(null);
      // Fetch insights separately so a failure doesn't block core dashboard
      try {
        setInsights(await api.insights(dashParams));
      } catch {
        setInsights(null);
      }
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Failed to load dashboard', 'error');
    } finally {
      setLoading(false);
    }
  }, [toast, timeRange]);

  useEffect(() => { load(); }, [load]);

  if (loading && !data) {
    return (
      <div style={{ padding: '16px 20px' }}>
        <Spinner text="Loading dashboard..." />
      </div>
    );
  }

  if (!data) return null;

  const { summary, weekly_activity, by_outcome_label, by_value_label, by_risk_level, by_task_type, by_model, by_agent, tokens_by_source, by_quality_score, by_failure_value_score = [], resolve_rate, resolve_rate_previous, read_edit_ratio, top_tools, avg_interrupts } = data;

  // Sort outcomes by count descending
  const sortedOutcomes = [...by_outcome_label].sort((a, b) => b.count - a.count);
  const sortedValues = [...by_value_label].sort((a, b) => b.count - a.count);
  const sortedRisks = [...by_risk_level].sort((a, b) => b.count - a.count);

  const weeklyMax = Math.max(...(weekly_activity || []).map(w => w.count), 0);
  const modelMax = Math.max(...by_model.map(m => m.count), 0);
  const agentMax = Math.max(...(by_agent || []).map(a => a.count), 0);
  const taskMax = Math.max(...by_task_type.map(t => t.count), 0);
  const totalSessions = summary.total_sessions;
  const outcomeMax = Math.max(...sortedOutcomes.map(b => b.count), 0);
  const valueMax = Math.max(...sortedValues.map(b => b.count), 0);
  const riskMax = Math.max(...sortedRisks.map(b => b.count), 0);
  const tokenSources = tokens_by_source.filter(s => s.input_tokens > 0 || s.output_tokens > 0);
  const tokenSourceMax = Math.max(...tokenSources.map(s => Math.max(s.input_tokens, s.output_tokens)), 0);

  const toReview = triage ? (triage.by_status['new'] ?? 0) + (triage.by_status['shortlisted'] ?? 0) : 0;
  const approved = triage?.by_status['approved'] ?? 0;
  const skipped = triage?.by_status['blocked'] ?? 0;

  const totalProductivityScored = by_quality_score.reduce((s, q) => s + q.count, 0);
  const productivityMax = Math.max(...by_quality_score.map(q => q.count), 0);
  const avgProductivity = totalProductivityScored > 0
    ? (by_quality_score.reduce((s, q) => s + q.score * q.count, 0) / totalProductivityScored).toFixed(1)
    : null;
  const productivityUnscored = Math.max(0, summary.total_sessions - totalProductivityScored);

  // Failure-value score stats
  const totalFailureScored = by_failure_value_score.reduce((s, q) => s + q.count, 0);
  const failureMax = Math.max(...by_failure_value_score.map(q => q.count), 0);
  const avgFailureValue = totalFailureScored > 0
    ? (by_failure_value_score.reduce((s, q) => s + q.score * q.count, 0) / totalFailureScored).toFixed(1)
    : null;
  const failureUnscored = Math.max(0, summary.total_sessions - totalFailureScored);

  return (
    <div style={{ padding: '16px 20px', maxWidth: 1200 }}>
      {/* Header */}
      <h1 style={{ fontSize: 20, fontWeight: 700, margin: '0 0 14px', color: colors.gray900 }}>Dashboard</h1>

      {/* Stat cards */}
      <div style={{ display: 'flex', gap: 10, marginBottom: 14, flexWrap: 'wrap' }}>
        <StatCard label="Sessions" value={formatNumber(summary.total_sessions)} sub={`${approved} approved`} />
        <StatCard label="API-equivalent cost" value={formatCost(summary.total_cost)} sub={summary.unpriced_sessions ? `${summary.unique_projects} projects · ${summary.unpriced_sessions} unpriced` : `${summary.unique_projects} projects`} color={colors.emerald400} />
        {resolve_rate != null && (() => {
          const pct = Math.round(resolve_rate * 100);
          const trend = resolve_rate_previous != null
            ? resolve_rate > resolve_rate_previous + 0.05 ? 'up'
            : resolve_rate < resolve_rate_previous - 0.05 ? 'down'
            : 'stable'
            : null;
          const arrow = trend === 'up' ? ' \u2191' : trend === 'down' ? ' \u2193' : trend === 'stable' ? ' \u2192' : '';
          const trendColor = trend === 'up' ? colors.green500 : trend === 'down' ? colors.red400 : colors.gray500;
          return <StatCard label="Resolve Rate" value={`${pct}%${arrow}`} sub={totalProductivityScored > 0 ? `${totalProductivityScored} scored` : 'not scored'} color={trendColor} />;
        })()}
        {showAllMetrics && (
          <>
            <StatCard label="Tokens" value={formatNumber(summary.total_tokens)} sub={`${tokens_by_source.length} sources`} />
            <StatCard label="Avg Failure Value" value={avgFailureValue ?? '--'} sub={totalFailureScored > 0 ? `${totalFailureScored} scored` : 'none scored'} color={avgFailureValue && parseFloat(avgFailureValue) >= 4 ? colors.red500 : avgFailureValue ? colors.yellow400 : colors.gray400} />
          </>
        )}
        <button
          onClick={() => setShowAllMetrics(v => !v)}
          style={{
            alignSelf: 'center', marginLeft: 2, padding: '4px 10px', borderRadius: 6,
            border: `1px solid ${colors.gray200}`, background: colors.white,
            color: colors.gray500, fontSize: 12.5, cursor: 'pointer', whiteSpace: 'nowrap',
          }}
        >
          {showAllMetrics ? 'Fewer metrics' : 'More metrics'}
        </button>
      </div>

      {/* Time range selector */}
      <div style={{ display: 'flex', gap: 6, marginBottom: 14 }}>
        {[['1d', 'Today'], ['1w', 'This Week'], ['1m', 'This Month'], ['all', 'Lifetime']].map(([key, label]) => (
          <button
            key={key}
            onClick={() => setTimeRange(key)}
            style={{
              padding: '4px 12px', borderRadius: 6, border: `1px solid ${timeRange === key ? colors.primary500 : colors.gray200}`,
              background: timeRange === key ? colors.primary500 : colors.white, color: timeRange === key ? colors.white : colors.gray700,
              fontSize: 13, cursor: 'pointer', fontWeight: timeRange === key ? 600 : 400,
            }}
          >{label}</button>
        ))}
      </div>


      {/* Outcomes + Agent Behavior — promoted for visibility */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginBottom: 12 }}>
        {/* Outcomes distribution */}
        {sortedOutcomes.length > 0 && (
          <Section title="Outcomes" subtitle="How sessions ended">
            {sortedOutcomes.map(b => (
              <BarRow key={b.outcome_label} label={labelFor(b.outcome_label)} value={b.count} max={outcomeMax} total={totalSessions} color={colors.indigo400} />
            ))}
          </Section>
        )}

        {/* Agent Behavior */}
        {(read_edit_ratio != null || avg_interrupts != null || (top_tools && top_tools.length > 0)) && (
          <Section title="Agent Behavior" subtitle="Tool usage patterns">
            {read_edit_ratio != null && (
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
                <span style={{ fontSize: 13, color: colors.gray600 }}>Read:Edit Ratio</span>
                <span style={{
                  fontSize: 18, fontWeight: 700,
                  color: read_edit_ratio >= 4 ? colors.green500 : read_edit_ratio >= 2 ? colors.yellow400 : colors.red400,
                }}>{read_edit_ratio.toFixed(1)}</span>
                <span style={{ fontSize: 11, color: colors.gray400 }}>
                  {read_edit_ratio >= 4 ? 'thorough' : read_edit_ratio >= 2 ? 'moderate' : 'edit-heavy'}
                </span>
              </div>
            )}
            {avg_interrupts != null && avg_interrupts > 0 && (
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
                <span style={{ fontSize: 13, color: colors.gray600 }}>Avg Interrupts</span>
                <span style={{
                  fontSize: 18, fontWeight: 700,
                  color: avg_interrupts >= 3 ? colors.red400 : avg_interrupts >= 1 ? colors.yellow400 : colors.gray500,
                }}>{avg_interrupts.toFixed(1)}</span>
                <span style={{ fontSize: 11, color: colors.gray400 }}>
                  {avg_interrupts >= 3 ? 'high steering' : avg_interrupts >= 1 ? 'some steering' : 'per session'}
                </span>
              </div>
            )}
            {top_tools && top_tools.length > 0 && (() => {
              const maxCalls = Math.max(...top_tools.map(t => t.calls), 1);
              return top_tools.slice(0, 6).map(t => (
                <BarRow key={t.tool} label={t.tool} value={t.calls} max={maxCalls} color={colors.teal400} />
              ));
            })()}
          </Section>
        )}
      </div>

      {/* Insights: Heatmap + Focus */}
      {insights && (
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginBottom: 12 }}>
          {/* Activity heatmap */}
          <Section title="Activity Heatmap" subtitle={timeRange === '1d' ? 'Sessions by hour' : 'Sessions by day'}>
            {insights.heatmap.length > 0 ? (() => {
              const maxSessions = Math.max(...insights.heatmap.map(x => x.sessions), 1);
              return (
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 2 }}>
                {insights.heatmap.map((h, i) => {
                  return (
                    <div
                      key={i}
                      title={`${h.day} ${String(h.hour).padStart(2, '0')}:00 — ${h.sessions} sessions, ${formatCost(h.cost)}`}
                      style={{
                        width: 16, height: 16, borderRadius: 2,
                        background: heatColor(h.sessions, maxSessions),
                      }}
                    />
                  );
                })}
              </div>
              );
            })() : (
              <div style={{ fontSize: 13, color: colors.gray400, padding: '8px 0' }}>No activity in this period</div>
            )}
          </Section>

          {/* Focus map */}
          <Section title="Focus Map" subtitle="Where your agent time went">
            {insights.focus.length > 0 ? (() => {
              const maxFocus = Math.max(...insights.focus.map(x => x.sessions), 1);
              return (
              <>
                {insights.focus.slice(0, 8).map(f => {
                  return (
                    <div key={f.project} style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '2px 0' }}>
                      <div style={{ width: 180, fontSize: 13, color: colors.gray700, flexShrink: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={f.project}>
                        {f.project}
                      </div>
                      <div style={{ flex: 1, background: colors.gray100, borderRadius: 3, height: 14 }}>
                        <div style={{ width: `${(f.sessions / maxFocus) * 100}%`, background: colors.primary400, borderRadius: 3, height: 14, minWidth: 2, transition: 'width 0.3s ease' }} />
                      </div>
                      <div style={{ width: 80, fontSize: 12, color: colors.gray500, textAlign: 'right', flexShrink: 0 }}>
                        {f.sessions} sess · {formatCost(f.cost)}
                      </div>
                    </div>
                  );
                })}
              </>
              );
            })() : (
              <div style={{ fontSize: 13, color: colors.gray400, padding: '8px 0' }}>No data in this period</div>
            )}
          </Section>
        </div>
      )}

      {/* Model effectiveness */}
      {insights && insights.model_effectiveness.length > 0 && (
        <div style={{ marginBottom: 12 }}>
          <Section title="Model Effectiveness" subtitle="Failure value and cost comparison across models">
            <div style={{ overflowX: 'auto' }}>
              <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
                <thead>
                  <tr style={{ borderBottom: `1px solid ${colors.gray200}`, color: colors.gray500, fontSize: 12 }}>
                    <th style={{ textAlign: 'left', padding: '6px 8px', fontWeight: 600 }}>Model</th>
                    <th style={{ textAlign: 'right', padding: '6px 8px', fontWeight: 600 }}>Sessions</th>
                    <th style={{ textAlign: 'right', padding: '6px 8px', fontWeight: 600 }} title="Average failure value">Avg failure value</th>
                    <th style={{ textAlign: 'right', padding: '6px 8px', fontWeight: 600 }}>Resolve Rate</th>
                    <th style={{ textAlign: 'right', padding: '6px 8px', fontWeight: 600 }} title="Average API-equivalent cost per session (total ÷ sessions)">Avg cost / session</th>
                    <th style={{ textAlign: 'right', padding: '6px 8px', fontWeight: 600 }}>Total API-equivalent</th>
                  </tr>
                </thead>
                <tbody>
                  {insights.model_effectiveness.slice(0, 10).map(m => {
                    const avgFailure = m.avg_failure_value_score ?? 0;
                    const failureColor = avgFailure >= 4 ? colors.red500 : avgFailure >= 3 ? colors.yellow400 : colors.gray500;
                    return (
                      <tr key={m.model} style={{ borderBottom: `1px solid ${colors.gray100}` }}>
                        <td style={{ padding: '6px 8px', color: colors.gray700, fontWeight: 500 }}>{shortModel(m.model)}</td>
                        <td style={{ padding: '6px 8px', textAlign: 'right', color: colors.gray600 }}>{m.sessions}</td>
                        <td style={{ padding: '6px 8px', textAlign: 'right', color: failureColor, fontWeight: 600 }}>{avgFailure > 0 ? avgFailure.toFixed(1) : '--'}</td>
                        <td style={{ padding: '6px 8px', textAlign: 'right', color: colors.gray600 }}>{((m.resolve_rate ?? 0) * 100).toFixed(0)}%</td>
                        <td style={{ padding: '6px 8px', textAlign: 'right', color: colors.gray600 }}>{formatCost(m.avg_cost)}</td>
                        <td style={{ padding: '6px 8px', textAlign: 'right', color: colors.gray700, fontWeight: 500 }}>{formatCost(m.total_cost)}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </Section>
        </div>
      )}

      {/* Cost breakdown */}
      {insights && (insights.cost_by_model.length > 0 || insights.cost_by_project.length > 0) && (
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginBottom: 12 }}>
          {insights.cost_by_model.length > 0 && (() => {
            const filtered = insights.cost_by_model.filter(c => c.cost > 0).slice(0, 8);
            const maxCost = Math.max(...filtered.map(x => x.cost || 0), 0.01);
            return (
            <Section title="Cost by Model" subtitle="API-equivalent cost across models">
              {filtered.map(c => (
                <BarRow key={c.model} label={shortModel(c.model)} value={c.cost || 0} max={maxCost} color={colors.emerald400} fmt={formatCost} />
              ))}
            </Section>
            );
          })()}
          {insights.cost_by_project.length > 0 && (() => {
            const filtered = insights.cost_by_project.filter(c => c.cost > 0).slice(0, 8);
            const maxCost = Math.max(...filtered.map(x => x.cost || 0), 0.01);
            return (
            <Section title="Cost by Project" subtitle="Which projects cost the most">
              {filtered.map(c => (
                <BarRow key={c.project} label={displayProject(c.project)} title={c.project} value={c.cost || 0} max={maxCost} color={colors.yellow400} fmt={formatCost} />
              ))}
            </Section>
            );
          })()}
        </div>
      )}

      {/* Triage progress bar */}
      {triage && (
        <div style={{ marginBottom: 16 }}>
          <div style={{ display: 'flex', gap: 14, fontSize: 13, color: colors.gray500, marginBottom: 4 }}>
            <span style={{ color: colors.green500 }} title="Sessions you've approved and are cleared for export/share">{approved} approved</span>
            <span style={{ color: colors.yellow400 }} title="Unreviewed (new) and shortlisted sessions — waiting for your triage decision">{toReview} to review</span>
            <span title="Sessions you've blocked — they stay local and are excluded from bundles and shares">{skipped} skipped</span>
          </div>
          <div style={{ display: 'flex', height: 6, borderRadius: 3, overflow: 'hidden', background: colors.gray100 }}>
            {approved > 0 && summary.total_sessions > 0 && <div style={{ width: `${(approved / summary.total_sessions) * 100}%`, background: colors.green400, transition: 'width 0.3s' }} />}
            {skipped > 0 && summary.total_sessions > 0 && <div style={{ width: `${(skipped / summary.total_sessions) * 100}%`, background: colors.gray300, transition: 'width 0.3s' }} />}
          </div>
        </div>
      )}

      {/* Row 1: Activity + Failure Value + Models */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 12, marginBottom: 12 }}>
        {/* Weekly activity */}
        <Section title="Activity" subtitle="Sessions per week">
          {(weekly_activity || []).map(w => (
            <BarRow key={w.week} label={w.week_start?.slice(5) ?? w.week} value={w.count} max={weeklyMax} color={colors.blue400} />
          ))}
          {(!weekly_activity || weekly_activity.length === 0) && (
            <div style={{ fontSize: 13, color: colors.gray400, padding: '8px 0' }}>No activity data</div>
          )}
        </Section>

        {/* Failure-value score distribution */}
        <Section title="Failure Value" subtitle={totalFailureScored > 0 ? `${totalFailureScored} scored, ${failureUnscored} pending` : 'No sessions scored yet'}>
          {totalFailureScored > 0 ? (
            <>
              {[5, 4, 3, 2, 1].map(score => {
                const entry = by_failure_value_score.find(q => q.score === score);
                const count = entry?.count ?? 0;
                const pct = failureMax > 0 ? (count / failureMax) * 100 : 0;
                const pctTotal = totalFailureScored > 0 ? ((count / totalFailureScored) * 100).toFixed(0) : '0';
                return (
                  <div key={score} style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '3px 0' }}>
                    <div style={{ width: 104, fontSize: 13, color: colors.gray700, flexShrink: 0 }}>
                      <span style={{ color: SCORE_COLORS[score], fontWeight: 700 }}>Failure value {score}</span>
                    </div>
                    <div style={{ flex: 1, background: colors.gray100, borderRadius: 3, height: 16 }}>
                      <div style={{
                        width: `${pct}%`, background: SCORE_COLORS[score], borderRadius: 3, height: 16,
                        minWidth: count > 0 ? 2 : 0, transition: 'width 0.3s ease',
                        display: 'flex', alignItems: 'center', justifyContent: 'flex-end', paddingRight: pct > 15 ? 6 : 0,
                      }}>
                        {pct > 15 && <span style={{ fontSize: 11, color: colors.white, fontWeight: 600 }}>{count}</span>}
                      </div>
                    </div>
                    <div style={{ width: 56, fontSize: 12, color: colors.gray500, textAlign: 'right', flexShrink: 0 }}>
                      {count} <span style={{ color: colors.gray400, fontSize: 11 }}>({pctTotal}%)</span>
                    </div>
                  </div>
                );
              })}
              <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 6, fontSize: 12, color: colors.gray400, borderTop: `1px solid ${colors.gray100}`, paddingTop: 6 }}>
                <span>Avg: <strong style={{ color: colors.gray700 }}>{avgFailureValue}</strong>/5</span>
                <span>{SCORE_LABELS[Math.round(parseFloat(avgFailureValue!))]}</span>
              </div>
            </>
          ) : (
            <div style={{ fontSize: 13, color: colors.gray400, padding: '16px 0', textAlign: 'center' }}>
              Run <code style={{ background: colors.gray100, padding: '2px 6px', borderRadius: 3 }}>clawjournal score</code> to score sessions
            </div>
          )}
        </Section>

        {/* Models */}
        {by_model.length > 0 && (
          <Section title="Models" subtitle="Sessions by model">
            {by_model.map(m => (
              <BarRow key={m.model} label={shortModel(m.model)} value={m.count} max={modelMax} total={summary.total_sessions} color={colors.emerald400} />
            ))}
          </Section>
        )}
      </div>

      {/* Row 1b: Agents */}
      {by_agent && by_agent.length > 0 && (
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 12, marginBottom: 12 }}>
          <Section title="Agents" subtitle="Sessions by AI agent">
            {by_agent.map(a => (
              <BarRow key={a.agent} label={a.agent} value={a.count} max={agentMax} total={summary.total_sessions} color={colors.primary500} />
            ))}
          </Section>
        </div>
      )}

      {/* Row 2: Task Types + Outcomes + Tokens */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 12, marginBottom: 12 }}>
        {/* Task types */}
        {by_task_type.length > 0 && (
          <Section title="Task Types" subtitle="Agent-classified work categories">
            {by_task_type.map(t => (
              <BarRow key={t.task_type} label={labelFor(t.task_type)} value={t.count} max={taskMax} total={summary.total_sessions} color={colors.yellow400} />
            ))}
          </Section>
        )}

        {/* Productivity score distribution */}
        <Section title="Productivity" subtitle={totalProductivityScored > 0 ? `${totalProductivityScored} scored, ${productivityUnscored} pending` : 'No productivity scores yet'}>
          {totalProductivityScored > 0 ? (
            <>
              {[5, 4, 3, 2, 1].map(score => {
                const entry = by_quality_score.find(q => q.score === score);
                const count = entry?.count ?? 0;
                const pct = productivityMax > 0 ? (count / productivityMax) * 100 : 0;
                const pctTotal = totalProductivityScored > 0 ? ((count / totalProductivityScored) * 100).toFixed(0) : '0';
                return (
                  <div key={score} style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '3px 0' }}>
                    <div style={{ width: 100, fontSize: 13, color: colors.gray700, flexShrink: 0 }}>
                      <span style={{ color: PRODUCTIVITY_COLORS[score], fontWeight: 700 }}>Productivity {score}</span>
                    </div>
                    <div style={{ flex: 1, background: colors.gray100, borderRadius: 3, height: 14 }}>
                      <div style={{
                        width: `${pct}%`, background: PRODUCTIVITY_COLORS[score], borderRadius: 3, height: 14,
                        minWidth: count > 0 ? 2 : 0, transition: 'width 0.3s ease',
                      }} />
                    </div>
                    <div style={{ width: 56, fontSize: 12, color: colors.gray500, textAlign: 'right', flexShrink: 0 }}>
                      {count} <span style={{ color: colors.gray400, fontSize: 11 }}>({pctTotal}%)</span>
                    </div>
                  </div>
                );
              })}
              <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 6, fontSize: 12, color: colors.gray400, borderTop: `1px solid ${colors.gray100}`, paddingTop: 6 }}>
                <span>Avg: <strong style={{ color: colors.gray700 }}>{avgProductivity}</strong>/5</span>
                <span>secondary lens</span>
              </div>
            </>
          ) : (
            <div style={{ fontSize: 13, color: colors.gray400, padding: '16px 0', textAlign: 'center' }}>
              Run <code style={{ background: colors.gray100, padding: '2px 6px', borderRadius: 3 }}>clawjournal score</code> to score sessions
            </div>
          )}
        </Section>

        {/* Tokens by source */}
        {tokenSources.length > 0 && (
          <Section title="Token Usage" subtitle="Input and output by source">
            {tokenSources.map(s => (
              <div key={s.source} style={{ marginBottom: 6 }}>
                <div style={{ fontSize: 13, fontWeight: 600, color: colors.gray700, marginBottom: 3 }}>{s.source}</div>
                <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                  <div style={{ width: 28, fontSize: 11, color: colors.gray400, textAlign: 'right', flexShrink: 0 }}>In</div>
                  <div style={{ flex: 1, background: colors.gray100, borderRadius: 3, height: 12 }}>
                    <div style={{ width: `${tokenSourceMax > 0 ? (s.input_tokens / tokenSourceMax) * 100 : 0}%`, background: colors.blue400, borderRadius: 3, height: 12, minWidth: s.input_tokens > 0 ? 2 : 0 }} />
                  </div>
                  <div style={{ width: 52, fontSize: 12, color: colors.gray500, textAlign: 'right', flexShrink: 0 }}>{formatNumber(s.input_tokens)}</div>
                </div>
                <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 2 }}>
                  <div style={{ width: 28, fontSize: 11, color: colors.gray400, textAlign: 'right', flexShrink: 0 }}>Out</div>
                  <div style={{ flex: 1, background: colors.gray100, borderRadius: 3, height: 12 }}>
                    <div style={{ width: `${tokenSourceMax > 0 ? (s.output_tokens / tokenSourceMax) * 100 : 0}%`, background: '#93c5fd', borderRadius: 3, height: 12, minWidth: s.output_tokens > 0 ? 2 : 0 }} />
                  </div>
                  <div style={{ width: 52, fontSize: 12, color: colors.gray500, textAlign: 'right', flexShrink: 0 }}>{formatNumber(s.output_tokens)}</div>
                </div>
              </div>
            ))}
          </Section>
        )}
      </div>

      {/* Row 3: Value Tags + Risk Flags */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
        {sortedValues.length > 0 && (
          <Section title="Session Tags" subtitle="How sessions are categorized">
            {sortedValues.map(b => (
              <BarRow key={b.badge} label={labelFor(b.badge)} value={b.count} max={valueMax} total={totalSessions} color={colors.teal400} />
            ))}
          </Section>
        )}

      </div>
    </div>
  );
}
