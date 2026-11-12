import React from 'react';

function formatOdds(n) {
  if (n === null || n === undefined) return '—';
  return n > 0 ? `+${n}` : `${n}`;
}

function formatTime(isoStr) {
  if (!isoStr) return 'TBD';
  try {
    return new Date(isoStr).toLocaleTimeString('en-US', {
      hour: 'numeric', minute: '2-digit', timeZoneName: 'short',
    });
  } catch { return isoStr; }
}

function WinPct({ pct, l10 }) {
  if (!pct) return null;
  const color = pct >= 0.55 ? '#22c55e' : pct >= 0.48 ? '#f59e0b' : '#ef4444';
  return (
    <span style={{ color, fontWeight: 600 }}>
      {(pct * 100).toFixed(0)}% {l10 != null ? `(L10: ${l10}-${10 - l10})` : ''}
    </span>
  );
}

function GoalieLine({ name, sv, gaa }) {
  if (!name || name === 'TBD') return <span style={{ color: '#64748b' }}>TBD</span>;
  const svStr = sv ? `.${Math.round(sv * 1000)}` : '';
  const gaaStr = gaa ? `${gaa.toFixed(2)} GAA` : '';
  const svColor = sv >= 0.920 ? '#22c55e' : sv >= 0.905 ? '#f59e0b' : '#ef4444';
  return (
    <span>
      {name}{' '}
      {svStr && <span style={{ color: svColor, fontWeight: 600 }}>{svStr}</span>}
      {gaaStr && <span style={{ color: '#94a3b8' }}> / {gaaStr}</span>}
    </span>
  );
}

function GameCard({ game }) {
  const [parts] = React.useState(game.matchup.split(' @ '));
  const away = parts[0] || '';
  const home = parts[1] || '';

  return (
    <div className="card">
      <div className="card-header">
        <div>
          <div className="game-matchup">
            {away} <span style={{ color: '#94a3b8' }}>@</span> {home}
          </div>
          <div className="card-subtitle">{game.venue} &bull; {formatTime(game.game_time)}</div>
        </div>
        {game.consensus_total && (
          <span className="badge badge-total">O/U {game.consensus_total}</span>
        )}
      </div>

      <div className="game-meta">
        <div className="game-meta-item">
          Away Win%: <WinPct pct={game.away_win_pct} l10={game.away_l10} />
        </div>
        <div className="game-meta-item">
          Home Win%: <WinPct pct={game.home_win_pct} l10={game.home_l10} />
        </div>
        <div className="game-meta-item">
          ML:{' '}
          <span style={{ color: game.moneyline_away > 0 ? '#22c55e' : '#ef4444' }}>
            {formatOdds(game.moneyline_away)}
          </span>{' '}/{' '}
          <span style={{ color: game.moneyline_home > 0 ? '#22c55e' : '#ef4444' }}>
            {formatOdds(game.moneyline_home)}
          </span>
        </div>
      </div>

      <div className="factors" style={{ marginTop: 12 }}>
        <span className="factor-tag" style={{ minWidth: 180 }}>
          G (Away): <GoalieLine name={game.away_goalie} sv={game.away_goalie_sv} gaa={game.away_goalie_gaa} />
        </span>
        <span className="factor-tag" style={{ minWidth: 180 }}>
          G (Home): <GoalieLine name={game.home_goalie} sv={game.home_goalie_sv} gaa={game.home_goalie_gaa} />
        </span>
        {game.away_back_to_back && (
          <span className="factor-tag" style={{ color: '#f59e0b', borderColor: '#f59e0b55' }}>
            Away B2B
          </span>
        )}
        {game.home_back_to_back && (
          <span className="factor-tag" style={{ color: '#f59e0b', borderColor: '#f59e0b55' }}>
            Home B2B
          </span>
        )}
      </div>
    </div>
  );
}

export default function NHLGameList({ data }) {
  const { games = [], date, games_count } = data;
  return (
    <div>
      <div className="section-title" style={{ color: '#a78bfa' }}>
        NHL Games — {date}
        <span style={{ fontSize: '0.85rem', fontWeight: 400, color: '#94a3b8', marginLeft: 10 }}>
          ({games_count} games)
        </span>
      </div>
      {games.length === 0 ? (
        <div className="card" style={{ textAlign: 'center', color: '#94a3b8', padding: '40px' }}>
          No NHL games scheduled today.
        </div>
      ) : (
        games.map((g) => <GameCard key={g.game_id} game={g} />)
      )}
    </div>
  );
}
