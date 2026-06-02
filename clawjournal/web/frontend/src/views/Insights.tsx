import { useState, useEffect, useCallback } from 'react';
import { Link } from 'react-router-dom';
import type {
  AdvisorData,
  HighlightItem,
  HighlightsData,
  InsightsData,
  InsightsTrendRow,
  InsightsDurationVsScoreRow,
} from '../types.ts';
import { api } from '../api.ts';
import { LABELS } from '../components/BadgeChip.tsx';
import { Spinner } from '../components/Spinner.tsx';
import { useToast } from '../components/Toast.tsx';
import { colors } from '../theme.ts';

function formatCost(c: number | null | undefined): string {
  if (c == null || c === 0) return '$0';
  if (c < 0.01) return `$${c.toFixed(4)}`;
  return `$${c.toFixed(2)}`;
}

function localDate(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

function getDateRange(days: number): { start: string; end: string } {
  const now = new Date();
  const end = localDate(now);
  const d = new Date(now);
  d.setDate(d.getDate() - days);
  return { start: localDate(d), end };
}

const PRIORITY_COLORS: Record<string, string> = {
  high: colors.red400,
  medium: colors.yellow400,
  low: colors.blue400,
};

const PRIORITY_BG: Record<string, string> = {
  high: '#fef2f2',
  medium: '#fffbeb',
  low: '#eff6ff',
};

/* ---------- SVG Trends Chart ---------- */

const CHART_W = 760;
const CHART_H = 160;
const PAD = { top: 8, right: 50, bottom: 24, left: 36 };

function TrendsChart({ trends }: { trends: InsightsTrendRow[] }) {
  if (trends.length < 2) {
    return <div style={{ fontSize: 13, color: colors.gray400, padding: '12px 0' }}>Not enough data for trends</div>;
  }

  const sorted = [...trends].sort((a, b) => a.day.localeCompare(b.day));
  const maxSessions = Math.max(...sorted.map(t => t.sessions), 1);
  const plotW = CHART_W - PAD.left - PAD.right;
  const plotH = CHART_H - PAD.top - PAD.bottom;

  const xStep = plotW / Math.max(sorted.length - 1, 1);

  // Sessions area + line
  const sessionPoints = sorted.map((t, i) => ({
    x: PAD.left + i * xStep,
    y: PAD.top + plotH - (t.sessions / maxSessions) * plotH,
  }));
  const sessionLine = sessionPoints.map(p => `${p.x},${p.y}`).join(' ');
  const areaPath = `M${sessionPoints[0].x},${PAD.top + plotH} ` +
    sessionPoints.map(p => `L${p.x},${p.y}`).join(' ') +
    ` L${sessionPoints[sessionPoints.length - 1].x},${PAD.top + plotH} Z`;

  // Resolve rate line (0-100% on right axis)
  const rrPoints = sorted.map((t, i) => ({
    x: PAD.left + i * xStep,
    y: PAD.top + plotH - (t.resolve_rate ?? 0) * plotH,
  }));
  const rrLine = rrPoints.map(p => `${p.x},${p.y}`).join(' ');

  // X-axis labels (show ~5 evenly spaced)
  const labelStep = Math.max(1, Math.floor(sorted.length / 5));
  const xLabels = sorted.filter((_, i) => i % labelStep === 0 || i === sorted.length - 1);

  return (
    <svg width={CHART_W} height={CHART_H} style={{ display: 'block' }}>
      {/* Grid lines */}
      {[0, 0.25, 0.5, 0.75, 1].map(frac => (
        <line key={frac} x1={PAD.left} x2={CHART_W - PAD.right}
          y1={PAD.top + plotH * (1 - frac)} y2={PAD.top + plotH * (1 - frac)}
          stroke={colors.gray100} strokeWidth={1} />
      ))}

      {/* Sessions area fill */}
      <path d={areaPath} fill={colors.primary200} opacity={0.35} />
      {/* Sessions line */}
      <polyline points={sessionLine} fill="none" stroke={colors.primary500} strokeWidth={2} />

      {/* Resolve rate line */}
      <polyline points={rrLine} fill="none" stroke={colors.green500} strokeWidth={2} strokeDasharray="4,3" />

      {/* Left axis label */}
      <text x={PAD.left - 4} y={PAD.top + 4} textAnchor="end" fontSize={10} fill={colors.gray400}>{maxSessions}</text>
      <text x={PAD.left - 4} y={PAD.top + plotH} textAnchor="end" fontSize={10} fill={colors.gray400}>0</text>

      {/* Right axis labels */}
      <text x={CHART_W - PAD.right + 4} y={PAD.top + 4} textAnchor="start" fontSize={10} fill={colors.green500}>100%</text>
      <text x={CHART_W - PAD.right + 4} y={PAD.top + plotH} textAnchor="start" fontSize={10} fill={colors.green500}>0%</text>

      {/* X-axis labels */}
      {xLabels.map(t => {
        const i = sorted.indexOf(t);
        const x = PAD.left + i * xStep;
        const label = t.day.slice(5); // MM-DD
        return <text key={t.day} x={x} y={CHART_H - 2} textAnchor="middle" fontSize={10} fill={colors.gray400}>{label}</text>;
      })}

      {/* Dots on hover area */}
      {sessionPoints.map((p, i) => (
        <circle key={i} cx={p.x} cy={p.y} r={3} fill={colors.primary500} opacity={0.7}>
          <title>{sorted[i].day}: {sorted[i].sessions} sessions, {Math.round((sorted[i].resolve_rate ?? 0) * 100)}% resolved</title>
        </circle>
      ))}
    </svg>
  );
}

/* ---------- SVG Scatter Plot ---------- */

const SCATTER_W = 760;
const SCATTER_H = 200;
const SPAD = { top: 16, right: 16, bottom: 28, left: 50 };

// Colors keyed on the normalized outcome vocabulary emitted by the
// backend (see workbench/index.py._OUTCOME_NORMALIZE_SQL). Anything
// unrecognized falls through to the default gray in the caller.
const RESOLUTION_COLORS: Record<string, string> = {
  resolved:     colors.green500,
  partial:      colors.yellow400,
  interrupted:  colors.yellow400,
  failed:       colors.red400,
  abandoned:    colors.red500,
  exploratory:  colors.gray400,
  inconclusive: colors.gray400,
  trivial:      colors.gray400,
};

function ScatterPlot({ data }: { data: InsightsDurationVsScoreRow[] }) {
  const scored = data.filter(d => d.ai_failure_value_score != null);
  if (scored.length < 3) {
    return <div style={{ fontSize: 13, color: colors.gray400, padding: '12px 0' }}>Not enough scored sessions for scatter plot</div>;
  }

  const plotW = SCATTER_W - SPAD.left - SPAD.right;
  const plotH = SCATTER_H - SPAD.top - SPAD.bottom;

  // X axis: duration in minutes, capped at 95th percentile for legibility
  const durations = scored.map(d => (d.duration_seconds || 0) / 60).sort((a, b) => a - b);
  const p95Idx = Math.floor(durations.length * 0.95);
  const maxMinutes = Math.max(durations[p95Idx] || 60, 10);

  // Y axis: failure-value score 1-5
  const minScore = 1;
  const maxScore = 5;

  const dots = scored.map(d => {
    const mins = Math.min((d.duration_seconds || 0) / 60, maxMinutes);
    const x = SPAD.left + (mins / maxMinutes) * plotW;
    const score = d.ai_failure_value_score ?? 1;
    const y = SPAD.top + plotH - ((score - minScore) / (maxScore - minScore)) * plotH;
    const color = RESOLUTION_COLORS[d.resolution ?? ''] ?? colors.gray400;
    return { x, y, color, score, d };
  });

  return (
    <svg width={SCATTER_W} height={SCATTER_H} style={{ display: 'block' }}>
      {/* Grid */}
      {[1, 2, 3, 4, 5].map(score => {
        const y = SPAD.top + plotH - ((score - minScore) / (maxScore - minScore)) * plotH;
        return (
          <g key={score}>
            <line x1={SPAD.left} x2={SCATTER_W - SPAD.right} y1={y} y2={y} stroke={colors.gray100} strokeWidth={1} />
            <text x={SPAD.left - 6} y={y + 4} textAnchor="end" fontSize={10} fill={colors.gray400}>{score}</text>
          </g>
        );
      })}

      {/* X-axis ticks */}
      {[0, 0.25, 0.5, 0.75, 1].map(frac => {
        const x = SPAD.left + frac * plotW;
        const mins = Math.round(frac * maxMinutes);
        return <text key={frac} x={x} y={SCATTER_H - 4} textAnchor="middle" fontSize={10} fill={colors.gray400}>{mins}m</text>;
      })}

      {/* Dots */}
      {dots.map((dot, i) => (
        <circle key={i} cx={dot.x} cy={dot.y} r={4} fill={dot.color} opacity={0.6}>
          <title>{Math.round(dot.d.duration_seconds / 60)}min, failure value {dot.score}, {dot.d.resolution ?? 'unknown'}{dot.d.cost ? `, ${formatCost(dot.d.cost)}` : ''}</title>
        </circle>
      ))}

      {/* Axis labels */}
      <text x={SPAD.left + plotW / 2} y={SCATTER_H - 14} textAnchor="middle" fontSize={10} fill={colors.gray500}>Duration (minutes)</text>
      <text x={12} y={SPAD.top + plotH / 2} textAnchor="middle" fontSize={10} fill={colors.gray500} transform={`rotate(-90, 12, ${SPAD.top + plotH / 2})`}>Failure Value</text>
    </svg>
  );
}

/* ---------- Highlights helpers ---------- */

function shortDuration(seconds: number | null | undefined): string {
  if (!seconds || seconds <= 0) return '—';
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (hours && minutes) return `${hours}h ${minutes}m`;
  if (hours) return `${hours}h`;
  if (minutes) return `${minutes}m`;
  return `${seconds}s`;
}

function sourceLabel(source: string | null | undefined): string {
  if (!source) return '';
  return source.charAt(0).toUpperCase() + source.slice(1);
}

function outcomeColor(outcome: string | null | undefined): string {
  // Normalized vocabulary from workbench/index.py._OUTCOME_NORMALIZE_SQL.
  if (!outcome) return colors.gray400;
  if (outcome === 'resolved') return colors.green500;
  if (outcome === 'failed' || outcome === 'abandoned') return colors.red400;
  if (outcome === 'partial' || outcome === 'interrupted') return colors.yellow400;
  return colors.gray400;  // exploratory / inconclusive / trivial / unknown
}

function displayProject(project: string): string {
  // Strip the "source:" prefix so the bullet reads cleanly.
  const idx = project.indexOf(':');
  return idx >= 0 ? project.slice(idx + 1) : project;
}

function labelFor(outcome: string): string {
  return LABELS[outcome] ?? outcome.replace(/_/g, ' ');
}

function HighlightCard({ item }: { item: HighlightItem }) {
  const scoreColor = item.ai_failure_value_score && item.ai_failure_value_score >= 5
    ? colors.red500
    : item.ai_failure_value_score && item.ai_failure_value_score >= 4
    ? colors.red400
    : colors.gray400;

  return (
    <Link
      to={`/session/${item.session_id}`}
      style={{
        display: 'block',
        textDecoration: 'none',
        color: 'inherit',
        border: `1px solid ${colors.gray200}`,
        borderRadius: 8,
        padding: 12,
        background: '#fff',
        minHeight: 156,
        flex: '1 1 260px',
        maxWidth: 360,
        transition: 'border-color 0.15s, box-shadow 0.15s',
      }}
      onMouseEnter={(e) => {
        e.currentTarget.style.borderColor = colors.blue400;
        e.currentTarget.style.boxShadow = '0 2px 6px rgba(0,0,0,0.06)';
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.borderColor = colors.gray200;
        e.currentTarget.style.boxShadow = 'none';
      }}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: 6, marginBottom: 6 }}>
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
          {item.ai_failure_value_score != null && (
            <span style={{
              fontSize: 11, fontWeight: 600, color: scoreColor,
              background: `${scoreColor}18`, padding: '2px 7px', borderRadius: 10,
            }}>
              Failure value {item.ai_failure_value_score}/5
            </span>
          )}
          {item.outcome && (
            <span style={{
              fontSize: 11, fontWeight: 500, color: outcomeColor(item.outcome),
              background: `${outcomeColor(item.outcome)}15`, padding: '2px 7px', borderRadius: 10,
            }}>
              {labelFor(item.outcome)}
            </span>
          )}
        </div>
        {item.source && (
          <span style={{
            fontSize: 10, fontWeight: 600, color: colors.gray600,
            textTransform: 'uppercase', letterSpacing: 0.3,
          }}>
            {sourceLabel(item.source)}
          </span>
        )}
      </div>

      <div style={{
        fontSize: 13, fontWeight: 600, color: colors.gray900, marginBottom: 6,
        display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical',
        overflow: 'hidden', lineHeight: 1.3,
      }}>
        {item.title}
      </div>

      {item.summary_teaser && (
        <div style={{
          fontSize: 12, color: colors.gray700, marginBottom: 8, lineHeight: 1.45,
          display: '-webkit-box', WebkitLineClamp: 3, WebkitBoxOrient: 'vertical',
          overflow: 'hidden',
        }}>
          {item.summary_teaser}
        </div>
      )}

      <div style={{
        display: 'flex', justifyContent: 'space-between', alignItems: 'center',
        gap: 6, fontSize: 11, color: colors.gray500,
      }}>
        <span style={{
          overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1,
        }} title={item.project ?? ''}>
          {displayProject(item.project ?? '—')} · {shortDuration(item.duration_seconds)}
        </span>
        <span style={{ color: colors.gray500, fontStyle: 'italic', flexShrink: 0 }}>
          {item.rationale}
        </span>
      </div>
    </Link>
  );
}

/* ---------- Section wrapper ---------- */

function Section({ title, subtitle, children }: { title: string; subtitle?: string; children: React.ReactNode }) {
  return (
    <div style={{
      background: colors.white, border: `1px solid ${colors.gray200}`, borderRadius: 10,
      padding: '14px 18px', marginBottom: 12,
    }}>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginBottom: 10 }}>
        <h3 style={{ margin: 0, fontSize: 14, fontWeight: 700, color: colors.gray800 }}>{title}</h3>
        {subtitle && <span style={{ fontSize: 12, color: colors.gray400 }}>{subtitle}</span>}
      </div>
      {children}
    </div>
  );
}

/* ---------- Legend ---------- */

function TrendsLegend() {
  return (
    <div style={{ display: 'flex', gap: 16, fontSize: 11, color: colors.gray500, marginTop: 4 }}>
      <span><span style={{ display: 'inline-block', width: 12, height: 3, background: colors.primary500, marginRight: 4, verticalAlign: 'middle' }} />Sessions/day</span>
      <span><span style={{ display: 'inline-block', width: 12, height: 3, background: colors.green500, borderTop: '1px dashed', marginRight: 4, verticalAlign: 'middle' }} />Resolve rate</span>
    </div>
  );
}

function ScatterLegend() {
  const items = [
    { label: 'Resolved', color: colors.green500 },
    { label: 'Partial', color: colors.yellow400 },
    { label: 'Failed', color: colors.red400 },
    { label: 'Other', color: colors.gray400 },
  ];
  return (
    <div style={{ display: 'flex', gap: 14, fontSize: 11, color: colors.gray500, marginTop: 4 }}>
      {items.map(it => (
        <span key={it.label}>
          <span style={{ display: 'inline-block', width: 8, height: 8, borderRadius: '50%', background: it.color, marginRight: 4, verticalAlign: 'middle' }} />
          {it.label}
        </span>
      ))}
    </div>
  );
}

/* ---------- Main Component ---------- */

export function Insights() {
  const { toast } = useToast();
  const [advisor, setAdvisor] = useState<AdvisorData | null>(null);
  const [insights, setInsights] = useState<InsightsData | null>(null);
  const [highlights, setHighlights] = useState<HighlightsData | null>(null);
  const [days, setDays] = useState(7);
  const [showDetails, setShowDetails] = useState(false);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const { start, end } = getDateRange(days);
      const [adv, ins, hl] = await Promise.all([
        api.advisor({ days }),
        api.insights({ start, end }),
        api.highlights({ days, top: 3, min_failure_value: 4 }).catch(() => null),
      ]);
      setAdvisor(adv);
      setInsights(ins);
      setHighlights(hl);
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Failed to load insights', 'error');
    } finally {
      setLoading(false);
    }
  }, [toast, days]);

  useEffect(() => { load(); }, [load]);

  if (loading && !advisor) {
    return (
      <div style={{ padding: '16px 20px' }}>
        <Spinner text="Analyzing usage patterns..." />
      </div>
    );
  }

  if (!advisor) return null;

  const { headline, recommendations, summary_stats: stats } = advisor;

  return (
    <div style={{ padding: '16px 20px', maxWidth: 840 }}>
      <h1 style={{ fontSize: 20, fontWeight: 700, margin: '0 0 14px', color: colors.gray900 }}>
        Insights
      </h1>

      {/* Period selector */}
      <div style={{ display: 'flex', gap: 6, marginBottom: 14 }}>
        {[[7, 'Week'], [14, '2 Weeks'], [30, 'Month']].map(([d, label]) => (
          <button
            key={d as number}
            onClick={() => setDays(d as number)}
            style={{
              padding: '4px 12px', borderRadius: 6,
              border: `1px solid ${days === d ? colors.primary500 : colors.gray200}`,
              background: days === d ? colors.primary500 : colors.white,
              color: days === d ? colors.white : colors.gray700,
              fontSize: 13, cursor: 'pointer', fontWeight: days === d ? 600 : 400,
            }}
          >{label as string}</button>
        ))}
        <button
          onClick={load}
          disabled={loading}
          style={{
            marginLeft: 'auto', padding: '4px 12px', borderRadius: 6,
            border: `1px solid ${colors.gray200}`, background: colors.white,
            color: colors.gray700, fontSize: 13, cursor: 'pointer',
          }}
        >{loading ? 'Analyzing...' : 'Refresh'}</button>
      </div>

      {/* Headline card */}
      <div style={{
        background: colors.gray800, color: colors.gray50, borderRadius: 10,
        padding: '18px 22px', marginBottom: 16,
      }}>
        <div style={{ fontSize: 15, lineHeight: 1.6 }}>{headline}</div>
        {stats.potential_savings_usd > 0 && (
          <div style={{ marginTop: 8, fontSize: 13, color: colors.primary200 }}>
            Est. potential savings: {formatCost(stats.potential_savings_usd)}/period
          </div>
        )}
      </div>

      {/* HERO: actionable recommendations first */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, margin: '0 0 10px' }}>
        <h2 style={{ fontSize: 14, fontWeight: 700, color: colors.gray800, margin: 0, textTransform: 'uppercase', letterSpacing: '0.04em' }}>Recommendations</h2>
        {recommendations.length > 0 && (
          <span style={{ fontSize: 12, fontWeight: 700, color: colors.white, background: colors.primary500, borderRadius: 10, padding: '1px 8px' }}>{recommendations.length}</span>
        )}
      </div>
      {recommendations.length > 0 ? (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10, marginBottom: 18 }}>
          {recommendations.map((rec, i) => (
            <div key={i} style={{
              border: `1px solid ${colors.gray200}`,
              borderLeft: `4px solid ${PRIORITY_COLORS[rec.priority] || colors.gray400}`,
              borderRadius: 8, padding: '12px 16px',
              background: PRIORITY_BG[rec.priority] || colors.white,
            }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
                <span style={{ fontSize: 11, fontWeight: 700, textTransform: 'uppercase', color: PRIORITY_COLORS[rec.priority] || colors.gray500 }}>{rec.priority}</span>
                <span style={{ fontSize: 14, fontWeight: 600, color: colors.gray800 }}>{rec.title}</span>
              </div>
              <div style={{ fontSize: 13, color: colors.gray600, lineHeight: 1.5 }}>{rec.detail}</div>
              {rec.estimated_savings_usd != null && rec.estimated_savings_usd > 0 && (
                <div style={{ marginTop: 6, fontSize: 12, fontWeight: 600, color: colors.green500 }}>Est. savings: {formatCost(rec.estimated_savings_usd)}/period</div>
              )}
            </div>
          ))}
        </div>
      ) : (
        <div style={{ border: `1px solid ${colors.gray200}`, borderRadius: 8, padding: '20px', textAlign: 'center', color: colors.gray400, fontSize: 13, marginBottom: 18 }}>
          No specific optimization suggestions for this period. Your usage looks efficient.
        </div>
      )}

      {/* Passive analytics, collapsed by default */}
      <button onClick={() => setShowDetails(v => !v)} style={{
        display: 'flex', alignItems: 'center', gap: 6, width: '100%', textAlign: 'left',
        background: colors.gray50, border: `1px solid ${colors.gray200}`, borderRadius: 8,
        padding: '10px 14px', cursor: 'pointer', fontSize: 13, fontWeight: 600, color: colors.gray700, marginBottom: 12,
      }}>
        <span style={{ color: colors.gray400 }}>{showDetails ? '▾' : '▸'}</span> Analytics &amp; trends — cost, efficiency, highlights, trends
      </button>
      {showDetails && (<>

      {/* Summary stats */}
      <div style={{ display: 'flex', gap: 10, marginBottom: 16, flexWrap: 'wrap' }}>
        {([
          { label: 'API-equivalent cost', value: formatCost(stats.total_cost_usd) },
          { label: 'Sessions', value: String(stats.total_sessions) },
          {
            label: 'API-equivalent / session', value: formatCost(stats.cost_per_session),
            // The average is over priced sessions only; disclose how many were excluded.
            sub: stats.unpriced_sessions ? `${stats.unpriced_sessions} unpriced excluded` : '',
          },
        ] as { label: string; value: string; sub?: string }[]).map(s => (
          <div key={s.label} style={{
            flex: 1, minWidth: 100, background: colors.white,
            border: `1px solid ${colors.gray200}`, borderRadius: 8,
            padding: '12px 14px',
          }}>
            <div style={{ fontSize: 22, fontWeight: 700, color: colors.gray900 }}>{s.value}</div>
            <div style={{ fontSize: 12, color: colors.gray500, marginTop: 2 }}>{s.label}</div>
            {s.sub && <div style={{ fontSize: 11, color: colors.gray400, marginTop: 2 }}>{s.sub}</div>}
          </div>
        ))}
      </div>

      {/* Model info */}
      <div style={{ display: 'flex', gap: 10, marginBottom: 16, flexWrap: 'wrap' }}>
        {stats.most_efficient_model && (
          <div style={{
            flex: 1, background: colors.green50, border: `1px solid ${colors.green200}`, borderRadius: 8,
            padding: '10px 14px',
          }}>
            <div style={{ fontSize: 12, color: colors.green700, fontWeight: 600 }}>Most Efficient</div>
            <div style={{ fontSize: 14, color: colors.green500, marginTop: 2 }}>{stats.most_efficient_model}</div>
          </div>
        )}
      </div>

      {/* Highlights — top 3 recent high-failure-value sessions across different agents,
          matched to the tab's `days` window. */}
      {highlights && highlights.highlights.length > 0 && (
        <Section
          title="Highlights"
          subtitle={`Top ${highlights.highlights.length} recent high-failure-value sessions across agents`}
        >
          <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
            {highlights.highlights.map((item) => (
              <HighlightCard key={item.session_id} item={item} />
            ))}
          </div>
        </Section>
      )}
      {highlights && highlights.highlights.length === 0 && (
        <Section title="Highlights" subtitle={`No 4+ failure-value sessions in the last ${highlights.window_days} days`}>
          <div style={{ fontSize: 12, color: colors.gray500, padding: 8 }}>
            Run <code style={{ background: colors.gray100, padding: '1px 5px', borderRadius: 3 }}>clawjournal score</code> on recent sessions to populate this panel.
          </div>
        </Section>
      )}

      {/* Trends chart */}
      {insights && insights.trends.length >= 2 && (
        <Section title="Trends" subtitle="Sessions per day and resolve rate over time">
          <TrendsChart trends={insights.trends} />
          <TrendsLegend />
        </Section>
      )}

      {/* Duration vs Failure Value scatter */}
      {insights && insights.duration_vs_score && insights.duration_vs_score.length >= 3 && (
        <Section title="Duration vs Failure Value" subtitle="Longer sessions are not automatically higher value">
          <ScatterPlot data={insights.duration_vs_score} />
          <ScatterLegend />
        </Section>
      )}

      </>)}

      {/* Footer */}
      <div style={{ marginTop: 16, fontSize: 11, color: colors.gray400, textAlign: 'right' }}>
        Generated {advisor.generated_at ? new Date(advisor.generated_at).toLocaleString() : '--'}
      </div>
    </div>
  );
}
