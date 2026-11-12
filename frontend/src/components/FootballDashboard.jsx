import React, { useEffect, useState } from 'react';
import './FootballDashboard.css';

const MARKET_NAMES = { spread: 'Point spread', total: 'Game total', moneyline: 'Moneyline' };
const VARIABLE_STATUS = { used: 'In the model', context: 'For review', unavailable: 'Not measured yet' };
const GAME_STATUS = {
  qualified: 'Qualifying pick', watch: 'Watch list', no_odds: 'Waiting for odds',
  insufficient_data: 'Limited history',
};

function number(value, digits = 1) {
  return typeof value === 'number' && Number.isFinite(value) ? value.toFixed(digits) : '—';
}

function signed(value, digits = 1) {
  return typeof value === 'number' && Number.isFinite(value)
    ? `${value > 0 ? '+' : ''}${value.toFixed(digits)}` : '—';
}

function percent(value, digits = 1) {
  return typeof value === 'number' && Number.isFinite(value) ? `${(value * 100).toFixed(digits)}%` : '—';
}

function odds(value) {
  return typeof value === 'number' && Number.isFinite(value) ? `${value > 0 ? '+' : ''}${value}` : '—';
}

function timestamp(value) {
  const date = value && new Date(value);
  return date && !Number.isNaN(date.getTime())
    ? date.toLocaleString('en-US', { weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZoneName: 'short' })
    : 'Time not available';
}

function dateRange(start, end) {
  const format = value => {
    const date = value && new Date(`${value.slice(0, 10)}T12:00:00`);
    return date && !Number.isNaN(date.getTime())
      ? date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) : null;
  };
  const first = format(start), last = format(end);
  return first && last ? `${first} – ${last}` : first || '';
}

function sportsbook(value) {
  if (!value) return 'Book unavailable';
  const known = { draftkings: 'DraftKings', fanduel: 'FanDuel', betmgm: 'BetMGM', espnbet: 'ESPN BET', caesars: 'Caesars', williamhill_us: 'Caesars', betrivers: 'BetRivers', betonlineag: 'BetOnline', bovada: 'Bovada', fanatics: 'Fanatics' };
  return known[value] || value.replace(/_/g, ' ').replace(/\b\w/g, character => character.toUpperCase());
}

function stringList(value) {
  if (!Array.isArray(value)) return [];
  return value.map(item => typeof item === 'string' ? item : item?.description || item?.detail || item?.note || item?.name).filter(Boolean);
}

function externalUrl(value) {
  return typeof value === 'string' && /^https?:\/\//i.test(value) ? value : undefined;
}

function Stat({ label, value, note, accent }) {
  return <div className={`fb-stat ${accent ? 'fb-stat-accent' : ''}`}>
    <dt>{label}</dt><dd>{value}</dd>{note && <small>{note}</small>}
  </div>;
}

function PickCard({ pick, rank, watch = false }) {
  const push = Number.isFinite(pick.push_probability) ? pick.push_probability : 0;
  const conditionalWin = Number.isFinite(pick.model_probability) && push < 1
    ? pick.model_probability / (1 - push) : null;
  const factors = stringList(pick.factors);
  const cautions = stringList(pick.cautions);
  const reasons = stringList(pick.reasons);
  return <article className={`fb-pick ${watch ? 'fb-pick-watch' : ''}`}>
    <div className="fb-pick-top">
      <div className="fb-pick-title-row">
        {!watch && <span className="fb-rank" aria-label={`Rank ${rank}`}>{String(rank).padStart(2, '0')}</span>}
        <div>
          <p className="fb-eyebrow">{MARKET_NAMES[pick.market] || pick.market || 'Model candidate'}</p>
          <h3>{pick.pick || 'Pick unavailable'}</h3>
          <p className="fb-matchup">{pick.matchup}</p>
        </div>
      </div>
      <div className="fb-price">
        <strong>{odds(pick.odds)}</strong>
        <span>{sportsbook(pick.book)}</span>
      </div>
    </div>
    <p className="fb-kickoff">Kickoff · {timestamp(pick.game_time)}</p>
    <p className="fb-price-time">Price captured · {pick.odds_updated_at ? timestamp(pick.odds_updated_at) : 'Timestamp unavailable'}</p>
    <dl className="fb-pick-stats">
      <Stat label="Estimated edge" value={Number.isFinite(pick.edge) ? `${signed(pick.edge * 100)} pp` : '—'} note="Above the price’s break-even rate" accent={!watch} />
      <Stat label="Win, excluding pushes" value={percent(conditionalWin)} note={`Break-even: ${percent(pick.market_probability)}`} />
      <Stat label="Model EV / $100" value={Number.isFinite(pick.expected_value) ? `${pick.expected_value >= 0 ? '+' : '−'}$${Math.abs(pick.expected_value).toFixed(2)}` : '—'} note="Theoretical; not a return forecast" />
    </dl>
    <div className="fb-pick-notes">
      <span className="fb-badge fb-badge-muted">Provisional model</span>
      {Number.isFinite(pick.coverage) && <span>Core model coverage: {number(pick.coverage, 0)}%</span>}
      {push > 0.001 && <span>Push estimate: {percent(push)}</span>}
    </div>
    {watch && reasons.length > 0 && <div className="fb-watch-reason"><strong>Why it stays on watch</strong><ul>{reasons.map((reason, i) => <li key={i}>{reason}</li>)}</ul></div>}
    {factors.length > 0 && <div className="fb-factor-list"><h4>What drives the estimate</h4><ul>{factors.slice(0, 4).map((factor, i) => <li key={i}>{factor}</li>)}</ul></div>}
    {(cautions.length > 0 || pick.odds_updated_at) && <details className="fb-pick-details">
      <summary>Price timing and cautions</summary>
      {pick.odds_updated_at && <p>Odds captured {timestamp(pick.odds_updated_at)}. The quoted line and price must still be available for this estimate to apply.</p>}
      {cautions.length > 0 && <ul>{cautions.map((caution, i) => <li key={i}>{caution}</li>)}</ul>}
    </details>}
  </article>;
}

function InjuryList({ title, injuries }) {
  if (!Array.isArray(injuries) || injuries.length === 0) return null;
  return <div className="fb-injury-team"><h5>{title}</h5><ul>{injuries.slice(0, 8).map((injury, i) => <li key={i}>
    {typeof injury === 'string' ? injury : [injury.name || injury.player || injury.athlete, injury.position, injury.status || injury.description].filter(item => typeof item === 'string').join(' · ')}
  </li>)}</ul>{injuries.length > 8 && <p>{injuries.length - 8} additional listed players.</p>}</div>;
}

function metricValue(value, unit) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—';
  if (unit === '%' || unit === 'percent') return `${number(value)}%`;
  return number(value, /epa/i.test(unit || '') ? 3 : 2);
}

function MatchupInputs({ game, away, home }) {
  const metrics = Array.isArray(game.matchup_metrics) ? game.matchup_metrics : [];
  if (!metrics.length) return null;
  return <details className="fb-pick-details fb-input-comparison">
    <summary>Compare key matchup inputs</summary>
    <p>In-model inputs influence the estimate. Context inputs help review the matchup. A dash means the value is unavailable.</p>
    <div className="fb-input-table-scroll" role="region" aria-label={`${away} and ${home} input comparison`} tabIndex={0}>
      <table className="fb-input-table">
        <thead><tr><th scope="col">Input</th><th scope="col">{away}</th><th scope="col">{home}</th></tr></thead>
        <tbody>{metrics.map((metric, i) => <tr key={`${metric.name}-${i}`}>
          <th scope="row"><span>{metric.name}</span>{metric.unit && metric.unit !== '%' && metric.unit !== 'percent' && <small>{metric.unit}</small>}<span className={`fb-input-use ${metric.status === 'used' ? 'is-used' : ''}`}>{VARIABLE_STATUS[metric.status] || 'For review'}</span></th>
          <td>{metricValue(metric.away, metric.unit)}</td><td>{metricValue(metric.home, metric.unit)}</td>
        </tr>)}</tbody>
      </table>
    </div>
  </details>;
}

function GameCard({ game }) {
  const hasProjection = Number.isFinite(game.projected_away_score) && Number.isFinite(game.projected_home_score);
  const reasons = stringList(game.reasons);
  const away = game.away_team || 'Away', home = game.home_team || 'Home';
  return <article className="fb-game">
    <div className="fb-game-meta"><time dateTime={game.game_time}>{timestamp(game.game_time)}</time><span className={`fb-badge ${game.status === 'qualified' ? 'fb-badge-good' : 'fb-badge-muted'}`}>{GAME_STATUS[game.status] || 'Model review'}</span></div>
    <h3>{game.matchup || `${away} at ${home}`}</h3>
    <div className="fb-score-grid" aria-label="Projected score">
      <div><span>{away}</span><strong>{hasProjection ? number(game.projected_away_score) : '—'}</strong></div>
      <div><span>{home}</span><strong>{hasProjection ? number(game.projected_home_score) : '—'}</strong></div>
    </div>
    <p className="fb-score-caption">{hasProjection ? 'Projected score · model estimate' : 'Projection unavailable with the current data'}</p>
    <dl className="fb-game-lines">
      <div><dt>Fair home spread</dt><dd>{signed(game.fair_home_spread)}</dd></div>
      <div><dt>Fair total</dt><dd>{number(game.fair_total)}</dd></div>
      <div><dt>Home win estimate</dt><dd>{percent(game.home_win_probability)}</dd></div>
    </dl>
    <p className="fb-game-history">Prior games: {away} {game.history_games?.away ?? '—'} · {home} {game.history_games?.home ?? '—'}</p>
    {(game.venue?.name || game.neutral_site) && <p className="fb-game-context">{game.neutral_site ? 'Neutral site' : 'Home venue'}{game.venue?.name ? ` · ${game.venue.name}` : ''}{game.venue?.indoor === true ? ' · Indoors' : ''}</p>}
    {(Number.isFinite(game.rest?.away_days) || Number.isFinite(game.rest?.home_days)) && <p className="fb-game-context">Days since last game: away {number(game.rest?.away_days)} · home {number(game.rest?.home_days)}</p>}
    {(game.recent_primary_passers?.away || game.recent_primary_passers?.home) && <p className="fb-game-context">Recent primary passers: away {game.recent_primary_passers.away || 'unavailable'} · home {game.recent_primary_passers.home || 'unavailable'}. Confirm this week’s starters.</p>}
    {reasons.length > 0 && <p className="fb-game-reason">{reasons.join(' ')}</p>}
    <MatchupInputs game={game} away={away} home={home} />
    {(game.injuries?.home?.length > 0 || game.injuries?.away?.length > 0) && <details className="fb-pick-details">
      <summary>Listed player availability</summary>
      <p>Listings are context for review, not a verified starting-lineup report.</p>
      <InjuryList title={away} injuries={game.injuries.away} /><InjuryList title={home} injuries={game.injuries.home} />
    </details>}
  </article>;
}

function Validation({ validation = {} }) {
  const hasMarket = Number.isFinite(validation.closing_margin_mae) || Number.isFinite(validation.closing_total_mae);
  return <section className="fb-section fb-validation" aria-label="Model validation">
    <div className="fb-section-heading"><div><p className="fb-eyebrow">Evidence so far</p><h3>{validation.label || 'Model validation'}</h3></div><span className="fb-badge fb-badge-muted">Provisional estimates</span></div>
    <dl className="fb-validation-stats">
      <Stat label="Training games" value={validation.training_games ?? '—'} />
      <Stat label="Later test games" value={validation.holdout_games ?? '—'} />
      <Stat label="Margin error" value={Number.isFinite(validation.margin_mae) ? `${number(validation.margin_mae)} pts` : 'Not available'} note="Mean absolute error" />
      <Stat label="Total error" value={Number.isFinite(validation.total_mae) ? `${number(validation.total_mae)} pts` : 'Not available'} note="Mean absolute error" />
    </dl>
    <div className="fb-benchmark">
      <h4>Against a simple baseline</h4><p>Mean absolute error across {validation.holdout_games ?? 'the'} held-out games. Lower is better.</p>
      <div className="fb-input-table-scroll" role="region" aria-label="Model and baseline error comparison" tabIndex={0}>
        <table className="fb-input-table fb-benchmark-table"><thead><tr><th scope="col">Forecast</th><th scope="col">Margin error</th><th scope="col">Total error</th></tr></thead>
          <tbody><tr><th scope="row">This model</th><td>{number(validation.margin_mae, 2)}</td><td>{number(validation.total_mae, 2)}</td></tr><tr><th scope="row">Simple baseline</th><td>{number(validation.baseline_margin_mae, 2)}</td><td>{number(validation.baseline_total_mae, 2)}</td></tr></tbody>
        </table>
      </div>
      {Number.isFinite(validation.margin_mae) && Number.isFinite(validation.baseline_margin_mae) && validation.margin_mae >= validation.baseline_margin_mae && <p className="fb-benchmark-caution">The margin model has not improved on this baseline. Its spread and winner estimates have weaker supporting evidence.</p>}
    </div>
    <div className="fb-benchmark">
      <h4>Against closing market lines</h4>
      {hasMarket ? <>
        <p>The model and market rows use the same available games: {validation.closing_margin_games ?? '—'} for margin, {validation.closing_total_games ?? '—'} for totals. Closing prices are used only for this comparison.</p>
        <div className="fb-input-table-scroll" role="region" aria-label="Model and closing market error comparison" tabIndex={0}>
          <table className="fb-input-table fb-benchmark-table"><thead><tr><th scope="col">Forecast</th><th scope="col">Margin error</th><th scope="col">Total error</th></tr></thead>
            <tbody><tr><th scope="row">This model, matched sample</th><td>{number(validation.model_margin_mae_with_market, 2)}</td><td>{number(validation.model_total_mae_with_market, 2)}</td></tr><tr><th scope="row">Closing market</th><td>{number(validation.closing_margin_mae, 2)}</td><td>{number(validation.closing_total_mae, 2)}</td></tr></tbody>
          </table>
        </div>
        {Number.isFinite(validation.model_margin_mae_with_market) && Number.isFinite(validation.closing_margin_mae) && validation.model_margin_mae_with_market > validation.closing_margin_mae && <p className="fb-benchmark-caution">Closing lines predicted margins more accurately than this model on the matched sample.</p>}
        {Number.isFinite(validation.model_total_mae_with_market) && Number.isFinite(validation.closing_total_mae) && validation.model_total_mae_with_market > validation.closing_total_mae && <p className="fb-benchmark-caution">Closing lines predicted totals more accurately than this model on the matched sample.</p>}
      </> : <p>Historical closing lines are unavailable for a matched comparison. This model has not demonstrated an advantage over the market.</p>}
    </div>
    <p className="fb-validation-note">Scoring accuracy alone does not demonstrate profitable betting or calibrated win probabilities.</p>
    {stringList(validation.notes).map((note, i) => <p className="fb-muted-copy" key={i}>{note}</p>)}
    {typeof validation.notes === 'string' && <p className="fb-muted-copy">{validation.notes}</p>}
  </section>;
}

function Methodology({ data }) {
  const method = data.methodology || {};
  const variables = Array.isArray(method.variables) ? method.variables : [];
  const sources = Array.isArray(method.sources) ? method.sources : [];
  const health = Array.isArray(data.source_health) ? data.source_health : [];
  return <div className="fb-methodology">
    <section className="fb-section">
      <p className="fb-eyebrow">How the weekly list is built</p><h3>Measure the matchup, then evaluate the price</h3>
      <p className="fb-method-summary">{method.summary || 'Method details will appear after the next successful model update.'}</p>
      <p className="fb-muted-copy">Core model coverage counts historical games, listed season efficiencies, odds, and available NFL play-by-play data. It does not measure injury or weather coverage, and a high score is not a measure of prediction confidence.</p>
      {stringList(method.selection_rules).length > 0 && <ol className="fb-rules">{stringList(method.selection_rules).map((rule, i) => <li key={i}>{rule}</li>)}</ol>}
    </section>
    <section aria-label="Football model variables">
      <div className="fb-section-heading"><div><p className="fb-eyebrow">Variables that matter</p><h3>Variable coverage</h3></div></div>
      <div className="fb-variable-grid">{variables.map((variable, i) => <article className="fb-variable" key={`${variable.name}-${i}`}>
        <div className="fb-variable-top"><span className={`fb-badge ${variable.status === 'used' ? 'fb-badge-good' : variable.status === 'unavailable' ? 'fb-badge-warn' : 'fb-badge-muted'}`}>{VARIABLE_STATUS[variable.status] || 'For review'}</span>{variable.importance && <span className="fb-priority">{variable.importance}</span>}</div>
        <h4>{variable.name}</h4><p>{variable.description}</p>
        {externalUrl(variable.source_url) && <a href={variable.source_url} target="_blank" rel="noreferrer">Source reference <span aria-hidden="true">↗</span></a>}
      </article>)}</div>
      {variables.length === 0 && <div className="fb-empty">Variable coverage is not available yet.</div>}
    </section>
    <Validation validation={data.validation} />
    {stringList(method.limitations).length > 0 && <section className="fb-section fb-limitations"><h3>Limits to keep in view</h3><ul>{stringList(method.limitations).map((limitation, i) => <li key={i}>{limitation}</li>)}</ul></section>}
    {(sources.length > 0 || health.length > 0) && <section className="fb-section"><h3>Sources and data availability</h3>
      {health.length > 0 && <ul className="fb-source-health">{health.map((source, i) => <li key={`${source.name}-${i}`}><div><strong>{source.name}</strong>{source.note && <p>{source.note}</p>}</div><span className="fb-badge fb-badge-muted">{source.status || 'Unknown'}</span></li>)}</ul>}
      <div className="fb-source-links">{sources.filter(source => externalUrl(source.url)).map((source, i) => <a key={`${source.name}-${i}`} href={source.url} target="_blank" rel="noreferrer">{source.name} <span aria-hidden="true">↗</span></a>)}</div>
    </section>}
  </div>;
}

export default function FootballDashboard({ data = {} }) {
  const [view, setView] = useState('picks');
  const [query, setQuery] = useState('');
  const [market, setMarket] = useState('all');
  const [now, setNow] = useState(Date.now);
  const league = data.league === 'cfb' ? 'CFB' : 'NFL';
  const isUpcoming = game => Number.isFinite(Date.parse(game.game_time)) && Date.parse(game.game_time) > now;
  const originalPicks = Array.isArray(data.picks) ? data.picks : [];
  const originalGames = Array.isArray(data.games) ? data.games : [];
  const picks = originalPicks.filter(isUpcoming);
  const watchlist = (Array.isArray(data.watchlist) ? data.watchlist : []).filter(isUpcoming);
  const games = originalGames.filter(isUpcoming);
  const warnings = stringList(data.warnings);
  const searchedGames = games.filter(game => `${game.matchup || ''} ${game.away_team || ''} ${game.home_team || ''}`.toLowerCase().includes(query.toLowerCase().trim()));
  const selectedPicks = market === 'all' ? picks : picks.filter(pick => pick.market === market);
  const markets = [...new Set(picks.map(pick => pick.market).filter(Boolean))];
  useEffect(() => { setQuery(''); setMarket('all'); }, [data.league]);
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 60 * 1000);
    return () => window.clearInterval(timer);
  }, []);
  const dateLabel = dateRange(data.week_start, data.week_end);

  return <div className={`football-dashboard ${data.league === 'cfb' ? 'fb-college' : 'fb-nfl'}`}>
    <section className="fb-hero" aria-label={`${league} weekly overview`}>
      <div className="fb-hero-top"><div><p className="fb-eyebrow">{league === 'CFB' ? 'College football · FBS' : 'National Football League'}</p><h2>Best bets of the week</h2><p className="fb-week-label">{data.week_label || `${league} weekly board`}{dateLabel && <span> · {dateLabel}</span>}</p></div><span className="fb-hero-tag">{league}</span></div>
      <p className="fb-hero-copy">A shortlist based on the matchup, the available price, and the quality of the evidence. A week with no qualifying picks is a valid result.</p>
      <dl className="fb-overview">
        <div><dt>Qualifying picks</dt><dd>{picks.length}<small>{picks.length === 1 ? 'model candidate' : 'model candidates'}</small></dd></div>
        <div><dt>Upcoming games</dt><dd>{games.length}<small>on this week’s board</small></dd></div>
        <div><dt>Games with odds</dt><dd>{data.coverage?.games_with_odds ?? '—'}<small>at the last model update</small></dd></div>
        <div><dt>Historical games</dt><dd>{data.validation?.training_games ?? '—'}<small>informing the ratings</small></dd></div>
      </dl>
    </section>
    <nav className="fb-view-nav" aria-label={`${league} dashboard views`}>
      {[['picks', 'Best bets'], ['slate', 'Week slate'], ['method', 'Model inputs']].map(([id, label]) => <button key={id} type="button" aria-pressed={view === id} className={view === id ? 'is-active' : ''} onClick={() => setView(id)}>{label}{id === 'picks' && <span>{picks.length}</span>}{id === 'slate' && <span>{games.length}</span>}</button>)}
    </nav>
    {originalGames.length > games.length && <p className="fb-expiry-note" role="status">{originalGames.length - games.length} {originalGames.length - games.length === 1 ? 'game has' : 'games have'} been removed from this published board because kickoff has passed or its time is unavailable.</p>}
    {warnings.length > 0 && <details className="fb-warning"><summary>Data notes for this update ({warnings.length})</summary><ul>{warnings.map((warning, i) => <li key={i}>{warning}</li>)}</ul></details>}

    {view === 'picks' && <section aria-label={`${league} best bets`}>
      <div className="fb-section-heading"><div><h3>This week’s qualifying picks</h3><p>At most one recommendation per game. These are provisional model estimates.</p></div>{markets.length > 1 && <label className="fb-filter">Market<select value={market} onChange={event => setMarket(event.target.value)}><option value="all">All markets</option>{markets.map(value => <option key={value} value={value}>{MARKET_NAMES[value] || value}</option>)}</select></label>}</div>
      {selectedPicks.length > 0 ? <div className="fb-picks">{selectedPicks.map(pick => <PickCard key={`${pick.game_id}-${pick.market}`} pick={pick} rank={picks.indexOf(pick) + 1} />)}</div> : <div className="fb-empty"><span className="fb-empty-label">{picks.length ? 'No matches' : 'No qualifying bets'}</span><h4>{picks.length ? 'No picks in this market' : 'No bets meet the current rules'}</h4><p>{picks.length ? 'Choose another market to see the weekly shortlist.' : originalPicks.length ? 'The previously listed picks have reached kickoff or no longer have a valid start time. They have been removed from the shortlist.' : data.no_bet_reason || 'The current matchups and available prices do not meet the model’s evidence and edge requirements.'}</p><button type="button" onClick={() => setView('method')}>See the selection rules <span aria-hidden="true">→</span></button></div>}
      {watchlist.length > 0 && <section className="fb-watchlist"><div className="fb-section-heading"><div><p className="fb-eyebrow">Not recommendations</p><h3>Watch list</h3><p>These candidates did not clear every selection rule. Review the reason before considering the estimate.</p></div></div><div className="fb-picks">{watchlist.map((pick, i) => <PickCard key={`${pick.game_id}-${pick.market}-${i}`} pick={pick} watch />)}</div></section>}
      <div className="fb-inline-note">The listed price is part of the pick. A different spread, total, or payout changes its value. <button type="button" onClick={() => setView('method')}>Inspect the model inputs</button></div>
    </section>}

    {view === 'slate' && <section aria-label={`${league} weekly game slate`}>
      <div className="fb-section-heading"><div><h3>The remaining week</h3><p>Games that have already started are excluded. Fair lines below are model estimates.</p></div><label className="fb-search"><span>Find a team</span><input type="search" value={query} onChange={event => setQuery(event.target.value)} placeholder="Search teams" /></label></div>
      <p className="fb-results-count" role="status">{searchedGames.length} {searchedGames.length === 1 ? 'game' : 'games'}{query.trim() ? ` matching “${query.trim()}”` : ' remaining'}</p>
      {searchedGames.length > 0 ? <div className="fb-game-grid">{searchedGames.map(game => <GameCard key={game.game_id} game={game} />)}</div> : <div className="fb-empty"><h4>{games.length ? 'No matching teams' : 'No upcoming games on this board'}</h4><p>{games.length ? 'Try another team name or clear the search.' : data.no_bet_reason || 'The next available weekly slate will appear after a successful data update.'}</p>{query && <button type="button" onClick={() => setQuery('')}>Clear search</button>}</div>}
    </section>}
    {view === 'method' && <Methodology data={data} />}
  </div>;
}
