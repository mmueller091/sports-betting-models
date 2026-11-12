import React, { useMemo, useState } from 'react';
import './PickHistory.css';

const money = n => n == null ? '—' : `${n < 0 ? '-' : ''}$${Math.abs(n).toFixed(2)}`;
const time = s => s ? new Date(s).toLocaleString() : 'Unavailable';
const price = n => n == null ? 'Unpriced' : `${n > 0 ? '+' : ''}${n}`;
const result = r => r.settlement?.result || 'PENDING';
function download(name, text, type) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = document.createElement('a'); a.href = url; a.download = name; a.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function csvValue(value) {
  let s = value == null ? '' : String(value);
  if (typeof value === 'string' && /^[=+\-@]/.test(s)) s = `'${s}`;
  return `"${s.replaceAll('"', '""')}"`;
}
export default function PickHistory({ data }) {
  const [sport, setSport] = useState('all');
  const [status, setStatus] = useState('all');
  const [category, setCategory] = useState('all');
  const [from, setFrom] = useState(''); const [to, setTo] = useState('');
  const [search, setSearch] = useState('');
  const rows = useMemo(() => (data.records || []).filter(r => {
    const o = r.original;
    return (sport === 'all' || o.sport === sport) && (status === 'all' || result(r) === status)
      && (category === 'all' || o.category === category) && (!from || o.game_date >= from) && (!to || o.game_date <= to)
      && `${o.matchup} ${o.pick} ${o.book || ''}`.toLowerCase().includes(search.toLowerCase());
  }), [data, sport, status, category, from, to, search]);
  const graded = rows.filter(r => r.primary && r.settlement?.profit != null && result(r) !== 'VOID');
  const wins = graded.filter(r => result(r) === 'WIN').length;
  const losses = graded.filter(r => result(r) === 'LOSS').length;
  const pushes = graded.filter(r => result(r) === 'PUSH').length;
  const profit = graded.reduce((n, r) => n + r.settlement.profit, 0);
  const roi = graded.length ? `${(profit / (100 * graded.length) * 100).toFixed(1)}%` : '—';
  function exportCsv() {
    const headers = ['id','sport','category','game_date','game_time','matchup','pick','market','side','line','odds','book','quote_at','snapshot_at','model_version','probability','edge','counted_in_summary','result','simulated_profit_100','graded_at','result_source','revisions'];
    const values = rows.map(r => { const o = r.original; const s = r.settlement || {}; return [o.id,o.sport,o.category,o.game_date,o.game_time,o.matchup,o.pick,o.market,o.side,o.line,o.odds,o.book,o.quote_at,o.observed_at,o.model_version,o.probability,o.edge,r.primary,s.result || 'PENDING',s.profit,s.graded_at,s.source_url,r.revisions.length]; });
    download('pick-history.csv', [headers, ...values].map(row => row.map(csvValue).join(',')).join('\r\n'), 'text/csv;charset=utf-8');
  }
  return <section className="audit" aria-label="Pick history audit">
    <div className="audit-heading"><div><p className="audit-eyebrow">RECOMMENDATION LEDGER</p><h2>Pick history &amp; audit</h2>
      <p>Frozen original selections, transparent changes, and verified results.</p></div>
      <div className="audit-actions"><button onClick={exportCsv}>Download CSV</button><button onClick={() => download('pick-history.json', JSON.stringify({ ...data, records: rows }, null, 2), 'application/json')}>Download JSON</button></div></div>
    <p className="audit-note">Tracking began with the earliest retained snapshot: <strong>{time(data.started_at)}</strong>. Earlier recommendations are not reconstructed. These are model recommendations with simulated stakes; they do not represent wagers placed.</p>
    <div className="audit-filters">
      <label>Sport<select value={sport} onChange={e => setSport(e.target.value)}><option value="all">All active sports</option><option value="nfl">NFL</option><option value="cfb">College football</option><option value="nhl">NHL</option></select></label>
      <label>Type<select value={category} onChange={e => setCategory(e.target.value)}><option value="all">All selections</option><option value="bet">Game bets</option><option value="scorer">Goal predictions</option></select></label>
      <label>Result<select value={status} onChange={e => setStatus(e.target.value)}>{['all','PENDING','WIN','LOSS','PUSH','VOID'].map(s => <option key={s} value={s}>{s === 'all' ? 'All results' : s}</option>)}</select></label>
      <label>Game date from<input type="date" value={from} onChange={e => setFrom(e.target.value)} /></label>
      <label>Through<input type="date" value={to} onChange={e => setTo(e.target.value)} /></label>
      <label>Search<input value={search} onChange={e => setSearch(e.target.value)} placeholder="Team, player, or book" /></label>
    </div>
    <div className="audit-stats">{[['Saved selections',rows.length],['Pending',rows.filter(r => !r.settlement).length],['Record',`${wins}–${losses}–${pushes}`],['Simulated profit',money(profit)],['Simulated ROI',roi]].map(([label,value]) => <div key={label}><span>{label}</span><strong>{value}</strong></div>)}</div>
    <details className="audit-rules"><summary>How records, changes, and returns are counted</summary><p>{data.rules}</p><p>Summary statistics use only first game-market selections within the filters above. All selections remain in the table. Goal predictions have no quoted price and are excluded from returns. Each record shows its result source. Delayed or failed result checks never become automatic losses.</p></details>
    <div className="audit-table-wrap"><table className="audit-table"><caption>{rows.length} matching saved selections. Dates use the game’s Eastern calendar date.</caption><thead><tr><th>Game / snapshot</th><th>Original selection</th><th>Original price</th><th>Model</th><th>Result</th><th>Audit trail</th></tr></thead><tbody>
      {rows.map(r => { const o = r.original; const s = r.settlement; return <tr key={o.id}>
        <td><span className="audit-sport">{o.sport.toUpperCase()}</span><strong>{o.matchup}</strong><small>Game: {time(o.game_time)}</small><small>Snapshot: {time(o.observed_at)}</small></td>
        <td><strong>{o.pick}</strong><small>{o.market} · Line: {o.line ?? '—'}</small><small>{r.primary ? 'First selection in game / market' : o.category === 'scorer' ? 'Unpriced goal prediction' : 'Later opposing selection · excluded from returns'}</small></td>
        <td><strong>{price(o.odds)}</strong><small>{o.book || 'Book unavailable in original snapshot'}</small><small>Quoted: {time(o.quote_at)}</small></td>
        <td><strong>{o.probability == null ? '—' : `${(o.probability * 100).toFixed(1)}%`}</strong><small>{o.model_version}</small><small>{o.edge == null ? '' : `Edge: ${(o.edge * 100).toFixed(1)} pp`}</small></td>
        <td><span className={`audit-result audit-${result(r).toLowerCase()}`}>{result(r)}</span><strong>{money(s?.profit)}</strong>{s?.home_score != null && <small>Final away/home: {s.away_score} / {s.home_score}</small>}<small>{s?.note || r.grade_note || 'Awaiting final result.'}</small>{s?.source_url && <a href={s.source_url} target="_blank" rel="noreferrer">Verify result source</a>}</td>
        <td><details><summary>{r.revisions.length} change{r.revisions.length === 1 ? '' : 's'} · Details</summary><small>ID: {o.id}</small><small>Seen {r.observations} update(s); last {time(r.last_seen)}</small><small>Result checked: {time(r.last_checked)}</small>{s && <small>Graded: {time(s.graded_at)}</small>}
          {o.snapshot_source_url && <a href={o.snapshot_source_url} target="_blank" rel="noreferrer">Verify saved snapshot</a>}<p>Original reasoning and inputs</p><pre>{JSON.stringify(o.details, null, 2)}</pre>
          {r.revisions.map((v,i) => <div className="audit-revision" key={i}><strong>{time(v.observed_at)}</strong><small>{v.pick} · {price(v.odds)} · {v.book || 'Book unavailable'}</small><small>Probability: {v.probability == null ? '—' : `${(v.probability*100).toFixed(1)}%`}; quote {time(v.quote_at)}</small></div>)}
        </details></td>
      </tr>; })}
    </tbody></table>{rows.length === 0 && <div className="card">No saved selections match these filters. Qualifying recommendations are saved automatically on each model update.</div>}</div>
  </section>;
}
