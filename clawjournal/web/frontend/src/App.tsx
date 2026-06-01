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
import { ToastProvider } from './components/Toast.tsx';
import { colors, fontFamily } from './theme.ts';
import { api } from './api.ts';

interface SidebarCounts {
  toReview: number;
  approved: number;
  recommendations: number;
}

function Sidebar() {
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

  const NAV_ITEMS = [
    { to: '/dashboard', label: 'Dashboard', badge: null },
    { to: '/insights', label: 'Insights', badge: counts.recommendations > 0 ? counts.recommendations : null },
    { to: '/benchmark', label: 'Benchmark', badge: null },
    { to: '/search', label: 'Search', badge: null },
    { to: '/', label: 'Sessions', badge: counts.toReview > 0 ? counts.toReview : null },
    { to: '/share', label: 'Share', badge: null },
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
          end={item.to === '/'}
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

const SCORING_WARMUP_DECLINED_KEY = 'cj.scoringWarmupDeclined';

export default function App() {
  useEffect(() => {
    let cancelled = false;
    const timer = window.setTimeout(async () => {
      try {
        const warmup = await api.scoringWarmup();
        if (cancelled || warmup.status !== 'needs_confirmation' || !warmup.backend) {
          return;
        }
        // Ask at most once. There is no server-side "declined" flag, so a
        // browser that has already said no must remember it locally —
        // otherwise this blocking dialog re-fires on every reload.
        if (localStorage.getItem(SCORING_WARMUP_DECLINED_KEY)) {
          return;
        }
        const accepted = window.confirm(
          `Use ${warmup.display_name ?? warmup.backend} to score recent failure-corpus traces in the background?`,
        );
        if (cancelled) {
          return;
        }
        if (accepted) {
          await api.scoringWarmup({ confirm_backend: true, backend: warmup.backend });
        } else {
          localStorage.setItem(SCORING_WARMUP_DECLINED_KEY, '1');
        }
      } catch {
        // Background scoring is opportunistic; the workbench stays usable.
      }
    }, 0);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, []);

  return (
    <BrowserRouter>
      <ToastProvider>
        <div style={{
          display: 'flex',
          height: '100vh',
          fontFamily,
          color: colors.gray900,
          WebkitFontSmoothing: 'antialiased',
        }}>
          <Sidebar />
          <main style={{ flex: 1, overflow: 'auto', background: colors.white }}>
            <Routes>
              <Route path="/dashboard" element={<Dashboard />} />
              <Route path="/insights" element={<Insights />} />
              <Route path="/" element={<Inbox />} />
              <Route path="/search" element={<Search />} />
              <Route path="/session/:id" element={<SessionDetail />} />
              <Route path="/bundles" element={<Navigate to="/share" replace />} />
              <Route path="/policies" element={<Navigate to="/share/rules" replace />} />
              <Route path="/benchmark" element={<Benchmark />} />
              <Route path="/share" element={<Share />} />
              <Route path="/share/rules" element={<Policies />} />
            </Routes>
          </main>
        </div>
      </ToastProvider>
    </BrowserRouter>
  );
}
