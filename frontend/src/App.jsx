import React, { useState, useEffect, useCallback } from 'react';
import NHLBestBets from './components/NHLBestBets';
import NHLGoalScorers from './components/NHLGoalScorers';
import NHLGameList from './components/NHLGameList';
import FootballDashboard from './components/FootballDashboard';
import PickHistory from './components/PickHistory';
import './App.css';

const STATIC = process.env.REACT_APP_STATIC_DATA === 'true';
const PUBLIC = process.env.PUBLIC_URL || '';
const TABS = [
  { id: 'nfl-week', label: 'NFL Week', sport: 'nfl', key: 'weekly', component: FootballDashboard },
  { id: 'cfb-week', label: 'College Football', sport: 'cfb', key: 'weekly', component: FootballDashboard },
  { id: 'nhl-bets', label: 'NHL Best Bets', sport: 'nhl', key: 'bets', component: NHLBestBets },
  { id: 'nhl-scorers', label: 'Goal Scorers', sport: 'nhl', key: 'scorers', component: NHLGoalScorers },
  { id: 'nhl-games', label: 'NHL Games', sport: 'nhl', key: 'games', component: NHLGameList },
  { id: 'pick-history', label: 'Pick History', component: PickHistory },
];
const SPORT_ENDPOINTS = {
  nfl: { prefix: '/api/nfl', endpoints: { weekly: 'weekly' } },
  cfb: { prefix: '/api/cfb', endpoints: { weekly: 'weekly' } },
  nhl: { prefix: '/api/nhl', endpoints: { bets: 'best-bets', scorers: 'goal-scorers', games: 'daily' } },
};

async function getJson(url) {
  const response = await fetch(url, { cache: 'no-store' });
  if (!response.ok) throw new Error(`Request failed (${response.status})`);
  return response.json();
}

function timestamp(value) {
  return value ? new Date(value).toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }) : 'Not available';
}

export default function App() {
  const [tab, setTab] = useState(() => {
    const saved = window.location.hash.slice(1);
    if (TABS.some(t => t.id === saved)) return saved;
    const month = new Date().getMonth();
    return month >= 8 || month <= 1 ? 'nfl-week' : 'nhl-bets';
  });
  const [bundle, setBundle] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [refreshing, setRefreshing] = useState(false);
  const active = TABS.find(t => t.id === tab) || TABS[0];

  const load = useCallback(async () => {
    try {
      if (STATIC) {
        setBundle(await getJson(`${PUBLIC}/data/dashboard.json?t=${Date.now()}`));
      } else if (active.sport) {
        const { prefix, endpoints } = SPORT_ENDPOINTS[active.sport];
        const entries = await Promise.all(Object.entries(endpoints).map(async ([key, path]) => [key, await getJson(`${prefix}/${path}`)]));
        const data = Object.fromEntries(entries);
        setBundle(old => ({ ...old, [active.sport]: { status: 'ok', generated_at: new Date().toISOString(), date: data.games?.date || data.weekly?.date, odds: data.weekly?.odds_status, warnings: data.weekly?.warnings || [], data } }));
      } else {
        const audit = await getJson('/api/pick-history');
        setBundle(old => ({ ...old, audit }));
      }
      setError(null);
    } catch {
      setError('The latest results could not be loaded. Check your connection and try again.');
    } finally {
      setLoading(false);
    }
  }, [active.sport]);

  useEffect(() => {
    setLoading(true);
    load();
    const timer = window.setInterval(load, 5 * 60 * 1000);
    return () => window.clearInterval(timer);
  }, [load]);

  async function refresh() {
    setRefreshing(true);
    try {
      if (!STATIC && active.sport) {
        const response = await fetch(`${SPORT_ENDPOINTS[active.sport].prefix}/refresh`, { method: 'POST' });
        if (!response.ok) throw new Error('Refresh failed');
      }
      await load();
    } catch {
      setError('The model refresh failed. Please try again later.');
    } finally {
      setRefreshing(false);
    }
  }

  const sport = bundle?.[active.sport];
  const data = tab === 'pick-history' ? bundle?.audit : sport?.data?.[active.key];
  const age = sport?.generated_at ? Date.now() - Date.parse(sport.generated_at) : Infinity;
  const today = new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York' }).format(new Date());
  const stale = sport && (sport.status === 'stale' || age > 3 * 60 * 60 * 1000 || sport.date !== today);
  const Component = active.component;

  return (
    <div className="app">
      <header className="header">
        <div className="header-inner">
          <div>
            <h1 className="header-title">Sports Betting Models</h1>
            <p className="header-date">Weekly NFL and college football picks · NHL analysis and pick history</p>
          </div>
          <button className="btn-refresh" onClick={refresh} disabled={refreshing || loading}>
            {refreshing ? 'Checking...' : STATIC ? 'Check for updates' : 'Refresh models'}
          </button>
        </div>
      </header>
      <nav className="tabs" aria-label="Sports and models">
        {TABS.map(t => <button key={t.id} aria-current={tab === t.id ? 'page' : undefined}
          className={`tab ${t.sport === 'nhl' ? 'tab-nhl' : ''} ${tab === t.id ? 'active' : ''}`}
          onClick={() => { setTab(t.id); window.history.replaceState(null, '', `#${t.id}`); }}>{t.label}</button>)}
      </nav>
      <main className="main">
        {STATIC && <div className="update-note">Models refresh about hourly. Market prices are cached for up to {bundle?.odds_cache_hours || 24} hours to conserve API credits. A pick applies only at its quoted line and price.</div>}
        {sport && <div className="freshness" role="status">
          <span>Model update: <strong>{timestamp(sport.generated_at)}</strong></span>
          <span>Market odds: <strong>{timestamp(sport.odds?.updated_at)}</strong></span>
        </div>}
        {stale && <div className="data-warning" role="status">These results are from an older update ({sport.date || 'date unavailable'}). They may not reflect today’s games or current lineups.</div>}
        {sport?.warnings?.length > 0 && (!data || sport.status === 'stale' || !['nfl', 'cfb'].includes(active.sport)) && <details className="data-warning"><summary>Some data sources could not be refreshed ({sport.warnings.length})</summary><ul>{sport.warnings.map(w => <li key={w}>{w}</li>)}</ul></details>}
        {error && <div className="error" role="alert">{error}</div>}
        {loading && !data && <div className="loading">Loading the latest model results...</div>}
        {Component && data && <Component data={data} />}
        {!loading && Component && !data && <div className="card">Results are not available yet. The next successful model update will appear here.</div>}
      </main>
      <footer className="footer">Model estimates are uncertain. Historical performance uses simulated stakes.
        {STATIC && <> · <a href="https://github.com/mmueller091/sports-betting-models/actions/workflows/publish.yml" target="_blank" rel="noreferrer">Update status</a></>}
      </footer>
    </div>
  );
}
