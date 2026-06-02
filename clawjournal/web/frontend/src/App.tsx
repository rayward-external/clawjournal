import { useState, useEffect } from 'react';
import { BrowserRouter, Routes, Route, NavLink, Navigate } from 'react-router-dom';
import { Inbox } from './views/Inbox.tsx';
import { Search } from './views/Search.tsx';
import SessionDetail from './views/SessionDetail.tsx';
import { Share } from './views/Share/index.tsx';
import { Policies } from './views/Policies.tsx';
import { Dashboard } from './views/Dashboard.tsx';
import { Insights } from './views/Insights.tsx';
import { Benchmark } from './views/Benchmark.tsx';
import { Settings } from './views/Settings.tsx';
import { ToastProvider } from './components/Toast.tsx';
import { ConfirmDialog } from './components/ConfirmDialog.tsx';
import { colors, fontFamily } from './theme.ts';
import { api, ApiError } from './api.ts';
import type { Features } from './types.ts';

interface SidebarCounts {
  toReview: number;
  approved: number;
  recommendations: number;
}

function Sidebar({ benchmarkEnabled }: { benchmarkEnabled: boolean }) {
  const [counts, setCounts] = useState<SidebarCounts>({ toReview: 0, approved: 0, recommendations: 0 });

  useEffect(() => {
    const loadStats = () => api.stats()
      .then(s => setCounts(c => ({
        ...c,
        toReview: (s.by_status['new'] ?? 0) + (s.by_status['shortlisted'] ?? 0),
        approved: s.by_status['approved'] ?? 0,
      })))
      .catch(() => {});
    // The advisor's recommendation count nudges the Insights tab — fetched once
    // (it changes slowly), while the cheap session stats refresh periodically.
    api.advisor({ days: 7 })
      .then(a => setCounts(c => ({ ...c, recommendations: a.recommendations.length })))
      .catch(() => {});
    loadStats();
    const iv = setInterval(loadStats, 30_000);
    return () => clearInterval(iv);
  }, []);

  const NAV_ITEMS: { to: string; label: string; badge: number | null; end?: boolean }[] = [
    { to: '/dashboard', label: 'Dashboard', badge: null },
    { to: '/insights', label: 'Insights', badge: counts.recommendations > 0 ? counts.recommendations : null },
    ...(benchmarkEnabled ? [{ to: '/benchmark', label: 'Benchmark', badge: null }] : []),
    { to: '/search', label: 'Search', badge: null },
    { to: '/', label: 'Sessions', badge: counts.toReview > 0 ? counts.toReview : null },
    // `end` on Share so the /share/rules sub-route highlights Rules, not Share.
    { to: '/share', label: 'Share', badge: null, end: true },
    { to: '/share/rules', label: 'Rules', badge: null },
    { to: '/settings', label: 'Settings', badge: null },
  ];

  return (
    <nav style={{
      width: 190,
      background: colors.gray50,
      borderRight: `1px solid ${colors.gray200}`,
      display: 'flex',
      flexDirection: 'column',
      padding: '16px 0',
      flexShrink: 0,
      fontFamily,
    }}>
      <div style={{
        padding: '0 16px 18px',
        fontSize: 17,
        fontWeight: 700,
        color: colors.gray800,
        letterSpacing: '-0.02em',
      }}>
        ClawJournal
      </div>
      {NAV_ITEMS.map((item) => (
        <NavLink
          key={item.to}
          to={item.to}
          end={item.to === '/' || item.end === true}
          style={({ isActive }) => ({
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            padding: '8px 16px',
            fontSize: 14,
            fontWeight: isActive ? 600 : 400,
            color: isActive ? colors.gray800 : colors.gray500,
            background: isActive ? colors.gray200 : 'transparent',
            textDecoration: 'none',
            borderLeft: isActive ? `3px solid ${colors.gray700}` : '3px solid transparent',
            borderRadius: '0 6px 6px 0',
            marginRight: 8,
            transition: 'background 0.15s ease',
          })}
        >
          <span>{item.label}</span>
          {item.badge != null && (
            <span style={{
              minWidth: 20,
              height: 20,
              padding: '0 6px',
              borderRadius: 10,
              background: colors.gray700,
              color: colors.gray50,
              fontSize: 11,
              fontWeight: 600,
              display: 'inline-flex',
              alignItems: 'center',
              justifyContent: 'center',
              lineHeight: 1,
            }}>
              {item.badge > 99 ? '99+' : item.badge}
            </span>
          )}
        </NavLink>
      ))}
      <div style={{ flex: 1 }} />
      <div style={{ padding: '8px 16px', fontSize: 12, color: colors.gray400 }}>
        Workbench v0.1
      </div>
    </nav>
  );
}

export default function App() {
  // Feature flags + the persisted auto-scorer decline come from /api/features.
  // benchmark_tab_enabled is initialised true so the common case never flashes;
  // only an explicitly-disabled install briefly shows the tab before it resolves.
  const [features, setFeatures] = useState<Features>({
    benchmark_tab_enabled: true,
    scoring_warmup_declined: false,
  });
  const benchmarkEnabled = features.benchmark_tab_enabled;

  // The same probe doubles as a connectivity check: the loopback daemon going
  // away (e.g. `clawjournal serve` stopped) otherwise just renders zero counts
  // silently. A persistent banner makes that state visible.
  const [daemonReachable, setDaemonReachable] = useState(true);
  useEffect(() => {
    let cancelled = false;
    const probe = () => api.features()
      .then(f => { if (!cancelled) { setFeatures(f); setDaemonReachable(true); } })
      // An ApiError means the daemon answered (any HTTP status) — connectivity is
      // fine. Only a fetch-level failure (network/daemon down) flips the banner.
      .catch(err => { if (!cancelled) setDaemonReachable(err instanceof ApiError); });
    probe();
    const iv = setInterval(probe, 20_000);
    return () => { cancelled = true; clearInterval(iv); };
  }, []);

  // Deferred, non-blocking warmup prompt (replaces the old mount-time
  // window.confirm). The server gates this: a previously-declined install
  // returns status 'declined', so we never re-prompt.
  const [warmupPrompt, setWarmupPrompt] = useState<{ backend: string; displayName: string } | null>(null);
  useEffect(() => {
    let cancelled = false;
    const timer = window.setTimeout(async () => {
      try {
        const warmup = await api.scoringWarmup();
        if (cancelled || warmup.status !== 'needs_confirmation' || !warmup.backend) {
          return;
        }
        setWarmupPrompt({ backend: warmup.backend, displayName: warmup.display_name ?? warmup.backend });
      } catch {
        // Background scoring is opportunistic; the workbench stays usable.
      }
    }, 0);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, []);

  const confirmWarmup = async () => {
    const prompt = warmupPrompt;
    setWarmupPrompt(null);
    if (!prompt) return;
    try {
      await api.scoringWarmup({ confirm_backend: true, backend: prompt.backend });
    } catch { /* opportunistic */ }
  };

  const declineWarmup = async () => {
    setWarmupPrompt(null);
    setFeatures(f => ({ ...f, scoring_warmup_declined: true }));
    try {
      await api.scoringWarmup({ decline: true });
    } catch { /* opportunistic */ }
  };

  return (
    <BrowserRouter>
      <ToastProvider>
        <div style={{
          display: 'flex',
          flexDirection: 'column',
          height: '100vh',
          fontFamily,
          color: colors.gray900,
          WebkitFontSmoothing: 'antialiased',
        }}>
          {!daemonReachable && (
            <div style={{
              background: colors.red50,
              color: colors.red700,
              padding: '8px 16px',
              fontSize: 13,
              fontWeight: 500,
              borderBottom: `1px solid ${colors.red200}`,
              flexShrink: 0,
            }}>
              Can’t reach the ClawJournal workbench. Is <code>clawjournal serve</code> still running?
            </div>
          )}
          <div style={{ display: 'flex', flex: 1, minHeight: 0 }}>
            <Sidebar benchmarkEnabled={benchmarkEnabled} />
            <main style={{ flex: 1, overflow: 'auto', background: colors.white }}>
              <Routes>
                <Route path="/dashboard" element={<Dashboard />} />
                <Route path="/insights" element={<Insights />} />
                <Route path="/" element={<Inbox />} />
                <Route path="/search" element={<Search />} />
                <Route path="/session/:id" element={<SessionDetail />} />
                <Route path="/bundles" element={<Navigate to="/share" replace />} />
                <Route path="/policies" element={<Navigate to="/share/rules" replace />} />
                <Route path="/benchmark" element={benchmarkEnabled ? <Benchmark /> : <Navigate to="/dashboard" replace />} />
                <Route path="/share" element={<Share />} />
                <Route path="/share/rules" element={<Policies />} />
                <Route path="/settings" element={<Settings />} />
              </Routes>
            </main>
          </div>
        </div>
        <ConfirmDialog
          open={warmupPrompt !== null}
          title="Turn on background AI scoring?"
          message={warmupPrompt
            ? `Run ${warmupPrompt.displayName} on your recent traces in the background to grade them? Each trace is anonymized on this machine (home-dir paths and usernames removed) before it is sent to your configured AI backend (${warmupPrompt.displayName}), which runs locally as a subprocess but may call its provider and incur usage cost.`
            : ''}
          confirmLabel="Turn on"
          variant="primary"
          onConfirm={confirmWarmup}
          onCancel={declineWarmup}
        />
      </ToastProvider>
    </BrowserRouter>
  );
}
