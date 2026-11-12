import React from 'react';

function ProbMeter({ prob }) {
  const pct = (prob * 100).toFixed(1);
  const width = Math.min(prob * 100, 100);
  const color = prob >= 0.75 ? '#22c55e' : prob >= 0.65 ? '#f59e0b' : '#3b82f6';

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
        <span style={{ fontSize: '0.75rem', color: '#94a3b8' }}>Hit Probability</span>
        <span style={{ fontSize: '1.1rem', fontWeight: 700, color }}>{pct}%</span>
      </div>
      <div className="prob-bar-wrap" style={{ height: 8 }}>
        <div style={{
          height: '100%',
          width: `${width}%`,
          borderRadius: 4,
          background: color,
          transition: 'width 0.5s',
        }} />
      </div>
    </div>
  );
}

function DataQualityPip({ quality }) {
  const level = quality >= 80 ? 'HIGH' : quality >= 60 ? 'MED' : 'LOW';
  const color = level === 'HIGH' ? '#22c55e' : level === 'MED' ? '#f59e0b' : '#94a3b8';
  return (
    <span style={{
      fontSize: '0.7rem',
      fontWeight: 700,
      color,
      background: `${color}22`,
      padding: '2px 8px',
      borderRadius: 4,
    }}>
      {level} DATA
    </span>
  );
}

function PlayerCard({ player, rank }) {
  const rankClass = rank <= 3 ? `rank-${rank}` : '';
  const avg = player.adjusted_avg ? player.adjusted_avg.toFixed(3) : 'N/A';

  return (
    <div className="card">
      <div className="card-header">
        <div style={{ display: 'flex', alignItems: 'center' }}>
          <span className={`rank ${rankClass}`}>#{rank}</span>
          <div>
            <div className="card-title">{player.name}</div>
            <div className="card-subtitle">
              {player.team} &bull; Bats #{player.batting_order} &bull; vs {player.facing_pitcher}
            </div>
          </div>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          {player.lineup_projected && (
            <span style={{
              fontSize: '0.7rem',
              fontWeight: 700,
              color: '#f59e0b',
              background: '#f59e0b22',
              padding: '2px 8px',
              borderRadius: 4,
            }}>
              PROJECTED LINEUP
            </span>
          )}
          <DataQualityPip quality={player.data_quality ?? player.confidence} />
        </div>
      </div>

      <ProbMeter prob={player.hit_probability} />

      <div className="stat-grid" style={{ marginTop: 12 }}>
        <div className="stat-box">
          <div className="stat-label">Matchup Hit Rate</div>
          <div className="stat-value">{avg}</div>
        </div>
        <div className="stat-box">
          <div className="stat-label">Expected PA</div>
          <div className="stat-value">{player.n_pa_estimated}</div>
        </div>
        <div className="stat-box">
          <div className="stat-label">AB per PA</div>
          <div className="stat-value">{player.ab_per_pa?.toFixed(3) ?? 'N/A'}</div>
        </div>
        <div className="stat-box">
          <div className="stat-label">BvP</div>
          <div className="stat-value" style={{ fontSize: '0.85rem' }}>
            {player.vs_pitcher_pa > 0
              ? `${player.vs_pitcher_avg?.toFixed(3) ?? 'N/A'} (${player.vs_pitcher_pa} PA)`
              : '-'}
          </div>
        </div>
      </div>

      {player.factors?.length > 0 && (
        <div className="factors">
          {player.factors.map((factor, index) => (
            <span key={index} className="factor-tag">{factor}</span>
          ))}
        </div>
      )}
    </div>
  );
}

function PairSummary({ pair }) {
  if (!pair?.players?.length) return null;
  return (
    <div className="bts-pair">
      <div>
        <div className="stat-label">Best Two-Player Combination</div>
        <div className="card-title">
          {pair.players.map(player => player.name).join(' + ')}
        </div>
      </div>
      <div style={{ textAlign: 'right' }}>
        <div className="stat-label">Both Get a Hit</div>
        <div className="stat-value green">{(pair.joint_probability * 100).toFixed(1)}%</div>
      </div>
    </div>
  );
}

export default function HitProps({ data }) {
  const { players = [], date, best_pair: bestPair, model, candidate_count: candidateCount } = data;

  if (players.length === 0) {
    return (
      <div>
        <div className="section-title">Beat the Streak</div>
        <div className="card" style={{ textAlign: 'center', color: '#94a3b8', padding: '40px' }}>
          No eligible lineup data is available yet.
        </div>
      </div>
    );
  }

  return (
    <div>
      <div className="section-heading-row">
        <div className="section-title">Most Likely to Get a Hit - {date}</div>
        <div className="model-status">
          {candidateCount} players scored &bull; {model?.source === 'trained' ? 'Trained model' : 'Bayesian baseline'}
        </div>
      </div>
      <PairSummary pair={bestPair} />
      {players.map((player, index) => (
        <PlayerCard
          key={`${player.game_id || 'game'}-${player.batter_id || index}`}
          player={player}
          rank={index + 1}
        />
      ))}
    </div>
  );
}
