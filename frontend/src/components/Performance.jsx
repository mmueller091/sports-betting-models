import React from 'react';

function formatOdds(n) {
  if (n === null || n === undefined) return '—';
  return n > 0 ? `+${n}` : `${n}`;
}

function StatCard({ label, value, color }) {
  return (
    <div className="stat-box" style={{ minWidth: 120 }}>
      <div className="stat-label">{label}</div>
      <div className="stat-value" style={color ? { color } : undefined}>{value}</div>
    </div>
  );
}

function roiColor(roi) {
  if (roi === null || roi === undefined) return undefined;
  return roi > 0 ? '#22c55e' : roi < 0 ? '#ef4444' : undefined;
}

function SummarySection({ title, s, isProps }) {
  if (!s || !s.count) {
    return (
      <div className="card">
        <div className="card-title" style={{ marginBottom: 8 }}>{title}</div>
        <div style={{ color: '#94a3b8' }}>No graded picks yet — check back after games finish.</div>
      </div>
    );
  }
  return (
    <div className="card">
      <div className="card-title" style={{ marginBottom: 12 }}>{title}</div>
      <div className="stat-grid">
        <StatCard label="Record" value={`${s.wins}-${s.losses}${s.pushes ? `-${s.pushes}` : ''}`} />
        <StatCard
          label={isProps ? 'Hit Rate' : 'Win Rate'}
          value={s.win_rate != null ? `${(s.win_rate * 100).toFixed(1)}%` : s.hit_rate != null ? `${(s.hit_rate * 100).toFixed(1)}%` : '—'}
        />
        <StatCard
          label="Profit (flat $100)"
          value={s.profit != null ? `$${s.profit.toFixed(0)}` : '—'}
          color={roiColor(s.profit)}
        />
        <StatCard
          label="ROI"
          value={s.roi_pct != null ? `${s.roi_pct > 0 ? '+' : ''}${s.roi_pct.toFixed(1)}%` : '—'}
          color={roiColor(s.roi_pct)}
        />
        {!isProps && (
          <StatCard
            label="Avg CLV"
            value={s.avg_clv_pct != null ? `${s.avg_clv_pct > 0 ? '+' : ''}${s.avg_clv_pct.toFixed(2)}%` : '—'}
            color={roiColor(s.avg_clv_pct)}
          />
        )}
        {isProps && s.avg_predicted_prob != null && (
          <StatCard label="Avg Predicted" value={`${(s.avg_predicted_prob * 100).toFixed(1)}%`} />
        )}
      </div>

      {s.calibration && s.calibration.length > 0 && (
        <div style={{ marginTop: 14 }}>
          <div className="stat-label" style={{ marginBottom: 6 }}>Calibration (predicted vs actual)</div>
          <table style={{ width: '100%', fontSize: '0.85rem', color: '#cbd5e1', borderCollapse: 'collapse' }}>
            <thead>
              <tr style={{ color: '#94a3b8', textAlign: 'left' }}>
                <th style={{ padding: '4px 8px' }}>Predicted range</th>
                <th style={{ padding: '4px 8px' }}>Picks</th>
                <th style={{ padding: '4px 8px' }}>Avg predicted</th>
                <th style={{ padding: '4px 8px' }}>Actual</th>
              </tr>
            </thead>
            <tbody>
              {s.calibration.map((c, i) => (
                <tr key={i} style={{ borderTop: '1px solid #1e293b' }}>
                  <td style={{ padding: '4px 8px' }}>{c.range}</td>
                  <td style={{ padding: '4px 8px' }}>{c.count}</td>
                  <td style={{ padding: '4px 8px' }}>{(c.predicted * 100).toFixed(1)}%</td>
                  <td style={{ padding: '4px 8px', color: c.actual >= c.predicted ? '#22c55e' : '#f59e0b' }}>
                    {(c.actual * 100).toFixed(1)}%
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {s.by_type && Object.keys(s.by_type).length > 1 && (
        <div style={{ marginTop: 14 }}>
          <div className="stat-label" style={{ marginBottom: 6 }}>By bet type</div>
          <table style={{ width: '100%', fontSize: '0.85rem', color: '#cbd5e1', borderCollapse: 'collapse' }}>
            <thead>
              <tr style={{ color: '#94a3b8', textAlign: 'left' }}>
                <th style={{ padding: '4px 8px' }}>Type</th>
                <th style={{ padding: '4px 8px' }}>Record</th>
                <th style={{ padding: '4px 8px' }}>ROI</th>
                <th style={{ padding: '4px 8px' }}>CLV</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(s.by_type).map(([type, t]) => (
                <tr key={type} style={{ borderTop: '1px solid #1e293b' }}>
                  <td style={{ padding: '4px 8px' }}>{type}</td>
                  <td style={{ padding: '4px 8px' }}>{t.wins}-{t.losses}{t.pushes ? `-${t.pushes}` : ''}</td>
                  <td style={{ padding: '4px 8px', color: roiColor(t.roi_pct) }}>
                    {t.roi_pct != null ? `${t.roi_pct > 0 ? '+' : ''}${t.roi_pct.toFixed(1)}%` : '—'}
                  </td>
                  <td style={{ padding: '4px 8px', color: roiColor(t.avg_clv_pct) }}>
                    {t.avg_clv_pct != null ? `${t.avg_clv_pct > 0 ? '+' : ''}${t.avg_clv_pct.toFixed(2)}%` : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function BTSSummary({ bts }) {
  if (!bts) return null;
  const top = bts.top_pick || {};
  const pair = bts.daily_pair || {};
  const streak = bts.streak || {};
  const trainingProgress = Math.min(
    (bts.training_rows / Math.max(bts.minimum_training_rows, 1)) * 100,
    100,
  );

  return (
    <div className="card">
      <div className="card-title" style={{ marginBottom: 12 }}>Beat the Streak Model</div>
      <div className="stat-grid">
        <StatCard label="Daily #1" value={top.count ? `${top.wins}-${top.losses}` : '-'} />
        <StatCard label="#1 Hit Rate" value={top.hit_rate != null ? `${(top.hit_rate * 100).toFixed(1)}%` : '-'} />
        <StatCard label="Avg Forecast" value={top.avg_predicted_prob != null ? `${(top.avg_predicted_prob * 100).toFixed(1)}%` : '-'} />
        <StatCard label="Brier Score" value={top.brier_score != null ? top.brier_score.toFixed(4) : '-'} />
        <StatCard label="Current Streak" value={streak.current ?? 0} />
        <StatCard label="Longest Streak" value={streak.longest ?? 0} />
        <StatCard label="Both Hit" value={pair.joint_hit_rate != null ? `${(pair.joint_hit_rate * 100).toFixed(1)}%` : '-'} />
        <StatCard label="Training Rows" value={`${bts.training_rows}/${bts.minimum_training_rows}`} />
      </div>

      <div className="prob-bar-wrap" style={{ marginTop: 12 }}>
        <div className="prob-bar" style={{ width: `${trainingProgress}%` }} />
      </div>

      {top.calibration?.length > 0 && (
        <div style={{ marginTop: 14 }}>
          <div className="stat-label" style={{ marginBottom: 6 }}>Daily #1 calibration</div>
          <table style={{ width: '100%', fontSize: '0.85rem', color: '#cbd5e1', borderCollapse: 'collapse' }}>
            <thead>
              <tr style={{ color: '#94a3b8', textAlign: 'left' }}>
                <th style={{ padding: '4px 8px' }}>Forecast</th>
                <th style={{ padding: '4px 8px' }}>Days</th>
                <th style={{ padding: '4px 8px' }}>Predicted</th>
                <th style={{ padding: '4px 8px' }}>Actual</th>
              </tr>
            </thead>
            <tbody>
              {top.calibration.map((bucket) => (
                <tr key={bucket.range} style={{ borderTop: '1px solid #1e293b' }}>
                  <td style={{ padding: '4px 8px' }}>{bucket.range}</td>
                  <td style={{ padding: '4px 8px' }}>{bucket.count}</td>
                  <td style={{ padding: '4px 8px' }}>{(bucket.predicted * 100).toFixed(1)}%</td>
                  <td style={{ padding: '4px 8px' }}>{(bucket.actual * 100).toFixed(1)}%</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export default function Performance({ data }) {
  if (!data) return null;
  const { bets, props, bts, recent = [], pending_count, days_tracked } = data;

  return (
    <div>
      <div className="section-title">Model Performance — {days_tracked} day{days_tracked === 1 ? '' : 's'} tracked</div>
      {pending_count > 0 && (
        <div style={{ color: '#94a3b8', fontSize: '0.85rem', marginBottom: 12 }}>
          {pending_count} pick{pending_count === 1 ? '' : 's'} pending (games not final yet)
        </div>
      )}

      <BTSSummary bts={bts} />
      <SummarySection title="Game Bets" s={bets} isProps={false} />
      <SummarySection title="Hit Props" s={props} isProps={true} />

      {recent.length > 0 && (
        <div className="card">
          <div className="card-title" style={{ marginBottom: 10 }}>Recent Results</div>
          <table style={{ width: '100%', fontSize: '0.85rem', color: '#cbd5e1', borderCollapse: 'collapse' }}>
            <thead>
              <tr style={{ color: '#94a3b8', textAlign: 'left' }}>
                <th style={{ padding: '4px 8px' }}>Date</th>
                <th style={{ padding: '4px 8px' }}>Pick</th>
                <th style={{ padding: '4px 8px' }}>Odds</th>
                <th style={{ padding: '4px 8px' }}>Result</th>
                <th style={{ padding: '4px 8px' }}>Profit</th>
              </tr>
            </thead>
            <tbody>
              {recent.map((r, i) => (
                <tr key={i} style={{ borderTop: '1px solid #1e293b' }}>
                  <td style={{ padding: '4px 8px', whiteSpace: 'nowrap' }}>{r.date}</td>
                  <td style={{ padding: '4px 8px' }}>{r.pick}</td>
                  <td style={{ padding: '4px 8px' }}>{formatOdds(r.odds)}</td>
                  <td style={{ padding: '4px 8px', fontWeight: 700,
                    color: r.result === 'win' ? '#22c55e' : r.result === 'loss' ? '#ef4444' : '#94a3b8' }}>
                    {(r.result || '').toUpperCase()}
                  </td>
                  <td style={{ padding: '4px 8px', color: roiColor(r.profit) }}>
                    {r.profit != null ? `${r.profit > 0 ? '+' : ''}$${r.profit.toFixed(0)}` : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
