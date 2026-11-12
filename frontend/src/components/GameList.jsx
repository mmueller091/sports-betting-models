import React, { useState } from 'react';

function formatOdds(n) {
  if (n === null || n === undefined) return '—';
  return n > 0 ? `+${n}` : `${n}`;
}

function formatTime(isoStr) {
  if (!isoStr) return 'TBD';
  try {
    const d = new Date(isoStr);
    return d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', timeZoneName: 'short' });
  } catch { return isoStr; }
}

function GameCard({ game }) {
  const [parts] = useState(game.matchup.split(' @ '));
  const away = parts[0] || '';
  const home = parts[1] || '';
  const weather = game.weather || {};

  return (
    <div className="card">
      <div className="card-header">
        <div>
          <div className="game-matchup">{away} <span style={{ color: '#94a3b8' }}>@</span> {home}</div>
          <div className="card-subtitle">{game.venue} &bull; {formatTime(game.game_time)}</div>
        </div>
        {game.consensus_total && (
          <span className="badge badge-total">O/U {game.consensus_total}</span>
        )}
      </div>

      <div className="game-meta">
        <div className="game-meta-item">
          SP (Away): <span>{game.away_pitcher}</span>
        </div>
        <div className="game-meta-item">
          SP (Home): <span>{game.home_pitcher}</span>
        </div>
        <div className="game-meta-item">
          ML: <span style={{ color: game.moneyline_away > 0 ? '#22c55e' : '#ef4444' }}>
            {formatOdds(game.moneyline_away)}
          </span> / <span style={{ color: game.moneyline_home > 0 ? '#22c55e' : '#ef4444' }}>
            {formatOdds(game.moneyline_home)}
          </span>
        </div>
        <div className="game-meta-item">
          Park Factor: <span>{game.park_factor}</span>
        </div>
      </div>

      {!weather.is_indoor && weather.temp_f && (
        <div className="factors" style={{ marginTop: 10 }}>
          <span className="factor-tag">{weather.temp_f}°F</span>
          <span className="factor-tag">Wind: {weather.wind}</span>
          {(weather.notes || []).map((n, i) => (
            <span key={i} className="factor-tag">{n}</span>
          ))}
        </div>
      )}
      {weather.is_indoor && (
        <div className="factors" style={{ marginTop: 10 }}>
          <span className="factor-tag">Indoor/Retractable — weather neutral</span>
        </div>
      )}
    </div>
  );
}

export default function GameList({ data }) {
  const { games = [], date, games_count } = data;

  return (
    <div>
      <div className="section-title">
        Today's Games — {date}
        <span style={{ fontSize: '0.85rem', fontWeight: 400, color: '#94a3b8', marginLeft: 10 }}>
          ({games_count} games)
        </span>
      </div>
      {games.length === 0 ? (
        <div className="card" style={{ textAlign: 'center', color: '#94a3b8', padding: '40px' }}>
          No games scheduled today.
        </div>
      ) : (
        games.map((g) => <GameCard key={g.game_id} game={g} />)
      )}
    </div>
  );
}
