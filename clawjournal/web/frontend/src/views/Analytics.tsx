import { NavLink, Outlet } from 'react-router-dom';
import { colors, fontFamily } from '../theme.ts';

// "Analytics" is a thin layout: a segmented sub-tab bar over the three
// read-only views (Dashboard / Insights / Benchmark) that all answer the same
// job — "understand my sessions". Folding them under one nav item keeps the
// sidebar to the three things a user actually does (browse, understand, share).
const TABS: { to: string; label: string; end: boolean; flag?: 'benchmark' }[] = [
  { to: '/analytics', label: 'Dashboard', end: true },
  { to: '/analytics/insights', label: 'Insights', end: false },
  { to: '/analytics/benchmark', label: 'Benchmark', end: false, flag: 'benchmark' },
];

export function Analytics({ benchmarkEnabled }: { benchmarkEnabled: boolean }) {
  const tabs = TABS.filter((t) => t.flag !== 'benchmark' || benchmarkEnabled);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', fontFamily }}>
      <div
        style={{
          display: 'flex',
          gap: 4,
          padding: '12px 20px 0',
          borderBottom: `1px solid ${colors.gray200}`,
          flexShrink: 0,
        }}
      >
        {tabs.map((tab) => (
          <NavLink
            key={tab.to}
            to={tab.to}
            end={tab.end}
            style={({ isActive }) => ({
              padding: '8px 14px',
              fontSize: 14,
              fontWeight: isActive ? 600 : 500,
              color: isActive ? colors.gray900 : colors.gray500,
              textDecoration: 'none',
              borderBottom: isActive ? `2px solid ${colors.gray800}` : '2px solid transparent',
              marginBottom: -1,
              transition: 'color 0.15s ease',
            })}
          >
            {tab.label}
          </NavLink>
        ))}
      </div>
      <div style={{ flex: 1, minHeight: 0, overflow: 'auto' }}>
        <Outlet />
      </div>
    </div>
  );
}
