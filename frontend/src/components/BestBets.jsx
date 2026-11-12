import React from 'react';

function formatOdds(n) {
  if (n === null || n === undefined) return 'N/A';
  return n > 0 ? `+${n}` : `${n}`;
}

function OddsLabel({ odds }) {
  const cls = odds > 0 ? 'odds-pos' : 'odds-neg';
  return <span className={cls}>{formatOdds(odds)}</span>;
}

function EdgeBar({ edge }) {
  const pct = Math.min(edge * 100, 30); // cap at 30% for display
  const width = (pct / 30) * 100;
  return (
    <div style={{ marginTop: 4 }}>
      <div style={{ fontSize: '0.7rem', color: '#94a3b8', marginBottom: 2 }}>
        Edge: +{(edge * 100).toFixed(1)}%
      </div>
      <div className="prob-bar-wrap">
        <div className="prob-bar" style={{ width: `${width}%` }} />
      </div>
    </div>
  );
}

function BetCard({ bet, rank }) {
  const rankClass = rank <= 3 ? `rank-${rank}` : '';

  return (
    <div className="card">
      <div className="card-header">
        <div style={{ display: 'flex', alignItems: 'center' }}>
          <span className={`rank ${rankClass}`}>#{rank}</span>
          <div>
            <div className="card-title">{bet.pick}</div>
            <div className="card-subtitle">{bet.matchup}</div>
          </div>
        </div>
        <div style={{ textAlign: 'right' }}>
          <span className={`badge ${bet.bet_type === 'Moneyline' ? 'badge-ml' : 'badge-total'}`}>
            {bet.bet_type}
          </span>
        </div>
      </div>

      <div className="stat-grid">
        <div className="stat-box">
          <div className="stat-label">Odds</div>
          <div className="stat-value"><OddsLabel odds={bet.odds} /></div>
        </div>
        <div className="stat-box">
          <div className="stat-label">Our Prob</div>
          <div className="stat-value green">{(bet.our_prob * 100).toFixed(1)}%</div>
        </div>
        <div className="stat-box">
          <div className="stat-label">Market Prob</div>
          <div className="stat-value">{(bet.market_prob * 100).toFixed(1)}%</div>
        </div>
        <div className="stat-box">
          <div className="stat-label">EV / $100</div>
          <div className={`stat-value ${bet.ev_per_100 > 0 ? 'green' : ''}`}>
            {bet.ev_per_100 > 0 ? '+' : ''}{bet.ev_per_100.toFixed(2)}
          </div>
        </div>
      </div>

      <EdgeBar edge={bet.edge} />

      {bet.reasoning && (
        <div className="reasoning">{bet.reasoning}</div>
      )}

      <div className="factors" style={{ marginTop: 10 }}>
        <span className="factor-tag">SP (Away): {bet.away_pitcher}</span>
        <span className="factor-tag">SP (Home): {bet.home_pitcher}</span>
        {bet.venue && <span className="factor-tag">{bet.venue}</span>}
        {bet.our_estimated_total && (
          <span className="factor-tag">Model total: {bet.our_estimated_total}</span>
        )}
        {bet.umpire && (
          <span className="factor-tag">HP Ump: {bet.umpire}{bet.umpire_tendency ? ` (${bet.umpire_tendency})` : ''}</span>
        )}
        {bet.sharp_signal && (
          <span className="factor-tag" style={{ color: '#fbbf24', borderColor: '#fbbf24' }}>
            ⚡ {bet.sharp_signal}
          </span>
        )}
        {(bet.weather_notes || []).map((n, i) => (
          <span key={i} className="factor-tag">{n}</span>
        ))}
      </div>
    </div>
  );
}

export default function BestBets({ data }) {
  const { bets = [], date } = data;

  if (bets.length === 0) {
    return (
      <div>
        <div className="section-title">Best Bets</div>
        <div className="card" style={{ textAlign: 'center', color: '#94a3b8', padding: '40px' }}>
          No value bets found today — the market may be efficient or lineups aren't posted yet.
          Try refreshing after lineups are confirmed (~3–4 hours before first pitch).
        </div>
      </div>
    );
  }

  return (
    <div>
      <div className="section-title">Best Bets — {date}</div>
          {bets.map((bet, i) => (
        <BetCard key={i} bet={bet} rank={i + 1} />
      ))}
    </div>
  );
}
