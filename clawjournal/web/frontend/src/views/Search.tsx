import { useState, useCallback, useRef, useEffect } from 'react';
import { useSearchParams } from 'react-router-dom';
import type { Session } from '../types.ts';
import { api } from '../api.ts';
import { TraceCard } from '../components/TraceCard.tsx';
import { Spinner } from '../components/Spinner.tsx';
import { EmptyState } from '../components/Spinner.tsx';
import { useToast } from '../components/Toast.tsx';
import { colors } from '../theme.ts';

const SEARCH_SHELL_WIDTH = 1120;

export function Search() {
  const { toast } = useToast();
  const [searchParams] = useSearchParams();
  const initialQuery = searchParams.get('q') ?? '';
  const [query, setQuery] = useState(initialQuery);
  const [results, setResults] = useState<Session[]>([]);
  const [loading, setLoading] = useState(false);
  const [searched, setSearched] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Cleanup debounce timer on unmount
  useEffect(() => () => { if (timerRef.current) clearTimeout(timerRef.current); }, []);

  const doSearch = useCallback(async (q: string) => {
    if (!q.trim()) {
      setResults([]);
      setSearched(false);
      return;
    }
    setLoading(true);
    setSearched(true);
    try {
      const data = await api.search(q.trim());
      setResults(data);
    } catch (e) {
      setResults([]);
      toast(e instanceof Error ? e.message : 'Search failed', 'error');
    } finally {
      setLoading(false);
    }
  }, [toast]);

  const handleChange = (value: string) => {
    setQuery(value);
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => doSearch(value), 300);
  };

  // Run the handed-off query once on mount (e.g. arriving from the Sessions
  // toolbar search box at /search?q=…), so the results are already there.
  useEffect(() => {
    if (initialQuery.trim()) doSearch(initialQuery);
    // Mount-only: the box owns the query after this initial hand-off.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div style={{ maxWidth: SEARCH_SHELL_WIDTH, margin: '0 auto', padding: '32px 24px 48px' }}>
      <h1 style={{ fontSize: 22, fontWeight: 700, margin: '0 0 4px 0', color: colors.gray900 }}>Search</h1>
      <p style={{ fontSize: 14, color: colors.gray500, margin: '0 0 16px 0' }}>Full-text search across all session content</p>

      <div style={{
        marginBottom: 24,
        padding: '18px',
        borderRadius: 14,
        background: colors.gray50,
      }}>
        <input
          type="text"
          value={query}
          onChange={(e) => handleChange(e.target.value)}
          placeholder="Search sessions by content, project, model..."
          style={{
            width: '100%',
            padding: '14px 16px',
            borderRadius: 8,
            border: `1px solid ${colors.gray300}`,
            fontSize: 15,
            outline: 'none',
            boxSizing: 'border-box',
            background: colors.white,
            boxShadow: '0 1px 2px rgba(0,0,0,0.03)',
          }}
        />
      </div>

      {loading && <Spinner text="Searching..." />}

      {!loading && results.length > 0 && (
        <div style={{ border: `1px solid ${colors.gray200}`, borderRadius: 8, overflow: 'hidden', background: colors.white }}>
          <div style={{ padding: '10px 16px', borderBottom: `1px solid ${colors.gray200}`, fontSize: 14, color: colors.gray500 }}>
            {results.length} result{results.length !== 1 ? 's' : ''}
          </div>
          {results.map((s) => (
            <TraceCard
              key={s.session_id}
              session={s}
              showSelection={false}
              showQuickActions={false}
            />
          ))}
        </div>
      )}

      {!loading && searched && results.length === 0 && (
        <EmptyState title={`No results for "${query}"`} description="Try a different search term or broaden your query." />
      )}

      {!loading && !searched && (
        <div style={{
          textAlign: 'center',
          padding: '40px 20px',
          minHeight: 280,
          display: 'flex',
          flexDirection: 'column',
          justifyContent: 'center',
        }}>
          <div style={{ fontSize: 32, marginBottom: 12 }}>&#128269;</div>
          <h3 style={{ margin: '0 0 6px', fontSize: 16, fontWeight: 600, color: colors.gray500 }}>Search sessions</h3>
          <p style={{ margin: '0 0 20px', fontSize: 14, color: colors.gray400 }}>Type to search, or try a suggested query:</p>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', justifyContent: 'center', maxWidth: 720, margin: '0 auto' }}>
            {PRESET_SEARCHES.map(preset => (
              <button
                key={preset.query}
                onClick={() => { handleChange(preset.query); }}
                style={{
                  padding: '6px 14px',
                  borderRadius: 9999,
                  border: `1px solid ${colors.gray300}`,
                  background: colors.white,
                  color: colors.gray600,
                  fontSize: 13,
                  cursor: 'pointer',
                  transition: 'all 0.15s',
                }}
                onMouseEnter={e => { e.currentTarget.style.background = colors.primary50; e.currentTarget.style.borderColor = colors.primary200; e.currentTarget.style.color = colors.primary500; }}
                onMouseLeave={e => { e.currentTarget.style.background = colors.white; e.currentTarget.style.borderColor = colors.gray300; e.currentTarget.style.color = colors.gray600; }}
              >
                {preset.label}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

const PRESET_SEARCHES = [
  { label: 'tests failed', query: 'tests failed' },
  { label: 'debugging', query: 'debugging' },
  { label: 'refactor', query: 'refactor' },
  { label: 'API integration', query: 'API' },
  { label: 'migration', query: 'migration' },
  { label: 'performance', query: 'performance' },
  { label: 'security', query: 'security' },
  { label: 'database', query: 'database' },
  { label: 'authentication', query: 'authentication' },
  { label: 'deployment', query: 'deploy' },
];
