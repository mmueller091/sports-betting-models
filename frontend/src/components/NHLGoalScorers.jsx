import React from 'react';

function ProbMeter({ prob }) {
  const pct = (prob * 100).toFixed(1);
  const color = prob >= 0.35 ? '#22c55e' : prob >= 0.25 ? '#f59e0b' : '#3b82f6';
  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
        <span style={{ fontSize: '0.75rem', color: '#94a3b8' }}>Goal Probability</span>
        <span style={{ fontSize: '1.1rem', fontWeight: 700, color }}>{pct}%</span>
      </div>
      <div className="prob-bar-wrap" style={{ height: 8 }}>
        <div style={{
          height: '100%',
          width: `${prob * 100 * 2.5}%`,
          maxWidth: '100%',
          borderRadius: 4,
          background: `linear-gradient(90deg, #7c3aed, ${color})`,
          transition: 'width 0.5s',
        }} />
      </div>
    </div>
  );
}

function PositionBadge({ position }) {
  const colors = { C: '#3b82f6', LW: '#22c55e', RW: '#f59e0b', D: '#94a3b8', G: '#ef4444' };
  const bg = colors[position] || '#94a3b8';
  return (
    <span style={{
      background: `${bg}33`,
      color: bg,
      fontSize: '0.68rem',
      fontWeight: 700,
      padding: '2px 7px',
      borderRadius: 4,
      marginLeft: 8,
    }}>{position}</span>
  );
}

function PlayerCard({ player, rank }) {
  const rankClass = rank <= 3 ? `rank-${rank}` : '';
  return (
    <div className="card">
      <div className="card-header">
        <div style={{ display: 'flex', alignItems: 'center' }}>
          <span className={`rank ${rankClass}`}>#{rank}</span>
          <div>
            <div className="card-title" style={{ display: 'flex', alignItems: 'center' }}>
              {player.name}
              <PositionBadge position={player.position} />
            </div>
            <div className="card-subtitle">
              {player.team} &bull; vs {player.opposing_goalie}
              {player.opposing_goalie_sv ? ` (.${Math.round(player.opposing_goalie_sv * 1000)} SV%)` : ''}
            </div>
          </div>
        </div>
        <div style={{ textAlign: 'right', fontSize: '0.78rem', color: '#94a3b8' }}>
          {player.goals}G {player.assists}A {player.points}P
        </div>
      </div>

      <ProbMeter prob={player.goal_probability} />

      <div className="stat-grid" style={{ marginTop: 12 }}>
        <div className="stat-box">
          <div className="stat-label">Goals/G</div>
          <div className="stat-value">{player.goals_per_game?.toFixed(3)}</div>
        </div>
        <div className="stat-box">
          <div className="stat-label">Shots/G</div>
          <div className="stat-value">{player.shots_per_game?.toFixed(2)}</div>
        </div>
        <div className="stat-box">
          <div className="stat-label">Shoot%</div>
          <div className="stat-value">{player.shooting_pct != null ? (player.shooting_pct * 100).toFixed(1) + '%' : '—'}</div>
        </div>
        <div className="stat-box">
          <div className="stat-label">PP Goals</div>
          <div className="stat-value">{player.pp_goals ?? 0}</div>
        </div>
      </div>

      {player.factors && player.factors.length > 0 && (
        <div className="factors">
          {player.factors.map((f, i) => (
            <span key={i} className="factor-tag">{f}</span>
          ))}
        </div>
      )}

      <div style={{ fontSize: '0.75rem', color: '#64748b', marginTop: 8 }}>
        {player.matchup} &bull; {player.venue}
      </div>
    </div>
  );
}

export default function NHLGoalScorers({ data }) {
  const { players = [], date } = data;
  return (
    <div>
      <div className="section-title" style={{ color: '#a78bfa' }}>Most Likely to Score — {date}</div>
      {players.length === 0 ? (
        <div className="card" style={{ textAlign: 'center', color: '#94a3b8', padding: '40px' }}>
          No goal scorer data available yet.
        </div>
      ) : (
        players.map((p, i) => <PlayerCard key={p.player_id || i} player={p} rank={i + 1} />)
      )}
    </div>
  );
}
