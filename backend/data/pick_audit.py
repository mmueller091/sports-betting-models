"""Durable, first-seen recommendation ledger. No paid odds or baseball calls."""
from __future__ import annotations
import asyncio
import hashlib
import json
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import httpx
from data.storage import STORE_DIR

AUDIT_PATH = STORE_DIR / 'pick_audit.json'
EASTERN = ZoneInfo('America/New_York')


def timestamp(value):
    try:
        t = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t
    except (ValueError, TypeError):
        return None


def load_ledger(path=None):
    p = Path(path or AUDIT_PATH)
    if not p.exists():
        return {'version': 1, 'started_at': None, 'records': {}}
    # A damaged ledger must stop publication rather than silently erase history.
    data = json.loads(p.read_text(encoding='utf-8'))
    if data.get('version') != 1 or not isinstance(data.get('records'), dict):
        raise ValueError('Invalid audit ledger')
    return data


def save_ledger(data, path=None):
    p = Path(path or AUDIT_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    tmp.replace(p)


def _normalized(sport, category, pick, games, generated):
    game = next((g for g in games if str(g.get('game_id')) == str(pick.get('game_id'))
                 and pick.get('game_id') is not None), None)
    if game is None:
        game = next((g for g in games if g.get('matchup') == pick.get('matchup')
                     and timestamp(g.get('game_time')) == timestamp(pick.get('game_time'))), {})
    game_id = pick.get('game_id') or game.get('game_id')
    kickoff = timestamp(pick.get('game_time'))
    observed = timestamp(generated)
    if not game_id or not kickoff or not observed or observed >= kickoff:
        return None  # Never manufacture a pregame record after kickoff.
    label = pick.get('pick') or f"{pick.get('name')} to score a goal"
    market = pick.get('market') or {'Moneyline': 'moneyline', 'Spread': 'spread', 'Total': 'total'}.get(pick.get('bet_type'), 'goal')
    side = pick.get('side') or ('over' if label.startswith('Over ') else 'under' if label.startswith('Under ') else pick.get('team'))
    if side is None:
        side = next((s for s in ('home', 'away') if game.get(s + '_team') and label.startswith(game[s + '_team'] + ' ')), None)
    line = pick.get('line', pick.get('market_line', pick.get('prop_line')))
    if line is None and market == 'spread':
        m = re.search(r' ([+-]\d+(?:\.\d+)?)\s*(?:\(|$)', label)
        line = float(m.group(1)) if m else None
    book = pick.get('book')
    if not book:
        m = re.search(r'\(([^()]+)\)$', label)
        book = m.group(1) if m else None
    key = [sport, str(game_id), category, market, str(pick.get('player_id') or side)]
    record_id = hashlib.sha256(json.dumps(key).encode()).hexdigest()[:24]
    return {'id': record_id, 'sport': sport, 'category': category, 'game_id': str(game_id),
            'game_time': kickoff.isoformat(), 'game_date': kickoff.astimezone(EASTERN).date().isoformat(),
            'matchup': pick.get('matchup'), 'home_team': game.get('home_team'), 'away_team': game.get('away_team'),
            'market': market, 'side': side, 'player_id': pick.get('player_id'),
            'pick': label, 'line': line, 'odds': pick.get('odds'), 'book': book,
            'quote_at': pick.get('odds_updated_at'),
            'probability': pick.get('model_probability', pick.get('our_prob', pick.get('goal_probability'))),
            'edge': pick.get('edge'), 'model_version': pick.get('model_version'),
            'details': pick, 'observed_at': observed.isoformat()}


def record_bundle(bundle, ledger, source_url=None):
    for sport in ('nfl', 'cfb', 'nhl'):
        block = bundle.get(sport, {})
        if block.get('status') not in ('ok', 'partial'):
            continue
        data = block.get('data', {})
        weekly = data.get('weekly', {})
        games = weekly.get('games', []) if sport != 'nhl' else data.get('games', {}).get('games', [])
        groups = [('bet', weekly.get('picks', []))] if sport != 'nhl' else [
            ('bet', data.get('bets', {}).get('bets', [])), ('scorer', data.get('scorers', {}).get('players', []))]
        for category, picks in groups:
            for pick in picks:
                row = _normalized(sport, category, pick, games, block.get('generated_at'))
                if not row:
                    continue
                if source_url:
                    row['snapshot_source_url'] = source_url
                row['model_version'] = weekly.get('model_version') or 'nhl-existing-model'
                records = ledger['records']
                existing = records.get(row['id'])
                if existing:
                    if timestamp(row['observed_at']) <= timestamp(existing['last_seen']):
                        continue
                    # Original quote and projection stay immutable. Only append changes.
                    previous = existing['revisions'][-1] if existing['revisions'] else existing['original']
                    fields = ('pick', 'line', 'odds', 'book', 'probability', 'edge', 'model_version', 'details')
                    if any(previous.get(k) != row.get(k) for k in fields):
                        existing['revisions'].append(row)
                    existing['last_seen'] = row['observed_at']
                    existing['observations'] += 1
                else:
                    # One first selection per game/market in the summary. Opposing
                    # later selections stay visible, but cannot inflate the record.
                    primary = not any(r['original']['sport'] == sport and r['original']['game_id'] == row['game_id']
                                      and r['original']['market'] == row['market'] and category == 'bet'
                                      for r in records.values()) if category == 'bet' else False
                    records[row['id']] = {'original': row, 'last_seen': row['observed_at'], 'observations': 1,
                                          'revisions': [], 'primary': primary, 'settlement': None}
                    if not ledger['started_at'] or row['observed_at'] < ledger['started_at']:
                        ledger['started_at'] = row['observed_at']


def settle(row, result):
    if result.get('canceled'):
        return {'result': 'VOID', 'profit': 0.0, 'note': 'Official source marks game canceled.'}
    if not result.get('completed'):
        return None
    market = row['market']
    if market == 'goal':
        players = result.get('players')
        if players is None:
            return None
        player = players.get(str(row['player_id']))
        if player is None or player.get('did_play') is False:
            return {'result': 'VOID', 'profit': None, 'note': 'Player did not appear in the final box score lineup.'}
        if player.get('did_play') is None or player.get('goals') is None:
            return None
        outcome = 'WIN' if player['goals'] > 0 else 'LOSS'
    else:
        home, away = result.get('home_score'), result.get('away_score')
        if home is None or away is None:
            return None
        if market == 'total':
            if row['line'] is None:
                return None
            margin = home + away - row['line']
            margin *= -1 if row['side'] == 'under' else 1
        else:
            home_team, away_team = row.get('home_team'), row.get('away_team')
            # NHL legacy summaries expose names in matchup rather than separate fields.
            if not home_team and ' @ ' in (row.get('matchup') or ''):
                away_team, home_team = row['matchup'].split(' @ ', 1)
            selected_home = row['side'] == 'home' or (home_team is not None and row['side'] == home_team)
            selected_away = row['side'] == 'away' or (away_team is not None and row['side'] == away_team)
            if not selected_home and not selected_away:
                return None
            margin = (home - away) * (1 if selected_home else -1)
            if market == 'spread':
                if row['line'] is None:
                    return None
                margin += row['line']
            elif market != 'moneyline':
                return None
        outcome = 'PUSH' if abs(margin) < 1e-8 else 'WIN' if margin > 0 else 'LOSS'
    odds = row.get('odds')
    profit = None
    if odds is not None and abs(odds) >= 100:
        profit = 0.0 if outcome == 'PUSH' else -100.0 if outcome == 'LOSS' else float(odds) if odds > 0 else 10000 / abs(odds)
    return {'result': outcome, 'profit': round(profit, 2) if profit is not None else None,
            'home_score': result.get('home_score'), 'away_score': result.get('away_score'),
            'note': ('Goal prediction only; no quoted price or simulated return. Full-game goals including overtime.' if market == 'goal' else
                     'Original quote; simulated $100 stake. Full game including overtime; NHL final scoreboard includes shootout deciding goal.')}


async def fetch_result(client, sport, game_id):
    if sport == 'nhl':
        url = f'https://api-web.nhle.com/v1/gamecenter/{game_id}/boxscore'
        response = await client.get(url)
        response.raise_for_status()
        data = response.json()
        final = data.get('gameState') in ('OFF', 'FINAL')
        result = {'completed': final, 'home_score': data.get('homeTeam', {}).get('score'),
                  'away_score': data.get('awayTeam', {}).get('score'), 'source_url': url}
        groups = data.get('playerByGameStats', {})
        if final and all(isinstance(groups.get(side), dict) and groups[side].get('forwards')
                         and isinstance(groups[side].get('defense'), list) for side in ('homeTeam', 'awayTeam')):
            players = {}
            for side in groups.values():
                for position in ('forwards', 'defense', 'goalies'):
                    for player in side.get(position, []):
                        toi = player.get('toi')
                        players[str(player['playerId'])] = {'goals': player.get('goals'), 'did_play': None if toi is None else toi not in ('0:00', '00:00')}
            if players:
                result['players'] = players
        return result
    league = {'nfl': 'nfl', 'cfb': 'college-football'}[sport]
    url = f'https://site.api.espn.com/apis/site/v2/sports/football/{league}/summary?event={game_id}'
    response = await client.get(url)
    response.raise_for_status()
    competition = response.json()['header']['competitions'][0]
    status = competition['status']['type']
    teams = {c['homeAway']: c for c in competition['competitors']}
    return {'completed': status.get('completed', False), 'canceled': status.get('name') in ('STATUS_CANCELED', 'STATUS_CANCELLED'),
            'home_score': float(teams['home']['score']) if status.get('completed') else None,
            'away_score': float(teams['away']['score']) if status.get('completed') else None, 'source_url': url}


async def grade_ledger(ledger, now=None, fetcher=fetch_result):
    now = now or datetime.now(timezone.utc)
    pending = {}
    for record in ledger['records'].values():
        row = record['original']
        checked = timestamp(record.get('last_checked'))
        if record['settlement'] or timestamp(row['game_time']) > now - timedelta(hours=2):
            continue
        if checked and now - checked < timedelta(hours=2):
            continue
        pending.setdefault((row['sport'], row['game_id']), []).append(record)
    semaphore = asyncio.Semaphore(6)
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        async def grade(key, records):
            async with semaphore:
                try:
                    result = await fetcher(client, *key)
                    for record in records:
                        settlement = settle(record['original'], result)
                        if settlement:
                            record['settlement'] = {**settlement, 'graded_at': now.isoformat(), 'source_url': result['source_url']}
                        record['grade_note'] = None if settlement else 'Awaiting a verified final score or complete box score.'
                except Exception:
                    for record in records:
                        record['grade_note'] = 'Result source unavailable; left pending.'
                for record in records:
                    record['last_checked'] = now.isoformat()
        # Old unresolved games rotate through checks instead of blocking newer ones.
        ordered = sorted(pending.items(), key=lambda kv: kv[1][0].get('last_checked', ''))[:50]
        await asyncio.gather(*(grade(key, rows) for key, rows in ordered))


def audit_summary(ledger=None):
    ledger = ledger if ledger is not None else load_ledger()
    records = list(ledger['records'].values())
    counted = [r for r in records if r['primary'] and r['settlement'] and r['settlement']['profit'] is not None
               and r['settlement']['result'] != 'VOID']
    wins = sum(r['settlement']['result'] == 'WIN' for r in counted)
    losses = sum(r['settlement']['result'] == 'LOSS' for r in counted)
    profit = sum(r['settlement']['profit'] for r in counted)
    return {'started_at': ledger['started_at'], 'generated_at': datetime.now(timezone.utc).isoformat(),
            'rules': 'Saved recommendations, not placed wagers. Original selection and price are frozen. Repeated updates append revisions. Summary counts the first saved selection per game and market, at $100 per priced selection; later opposing selections and unpriced goal predictions are excluded. Pushes return the stake; voids are excluded. Missing or postponed results stay pending. No historical closing-line value is claimed.',
            'summary': {'records': len(records), 'pending': sum(not r['settlement'] for r in records), 'wins': wins,
                        'losses': losses, 'pushes': sum(r['settlement']['result'] == 'PUSH' for r in counted),
                        'profit': round(profit, 2), 'roi': round(profit / (100 * len(counted)) * 100, 2) if counted else None},
            'records': sorted(records, key=lambda r: r['original']['observed_at'], reverse=True)}
