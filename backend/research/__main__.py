"""Reproducible offline research commands. Run from backend: python -m research."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from research.datasets import backfill_schedules, read_normalized_games, attach_nfl_play_metrics
from research.evaluation import evaluate_forecasts, compare_candidates
from research.models import CANDIDATES
from research.replay import run_replay, ReplayConfig
from research.nhl_replay import NHL_CANDIDATES, NHLReplayConfig, run_nhl_replay, train_nhl_artifact
from research.odds_history import (
    plan_historical_requests, write_request_manifest,
    fetch_historical_requests, normalize_fetched_manifest,
)


def read_rows(path):
    if not path:
        return []
    source = Path(path)
    if source.suffix == '.jsonl':
        return [json.loads(line) for line in source.read_text().splitlines() if line.strip()]
    data = json.loads(source.read_text())
    if isinstance(data, dict):
        return data.get('quotes', data.get('rows', []))
    return data


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False)+'\n')


def save_replay(rows, output, config):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    content = ''.join(json.dumps(row, sort_keys=True, allow_nan=False)+'\n' for row in rows)
    (output/'forecasts.jsonl').write_text(content)
    write_json(output/'run.json', {
        'created_at': datetime.now(timezone.utc).isoformat(),
        'config': asdict(config), 'rows': len(rows),
        'forecast_sha256': hashlib.sha256(content.encode()).hexdigest(),
        'execution': 'offline replay; no production settings or live ledgers modified',
    })


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest='command', required=True)
    backfill = sub.add_parser('backfill', help='Download free schedules and final scores with a source manifest.')
    backfill.add_argument('--league', choices=('nfl', 'cfb', 'nhl', 'all'), required=True)
    backfill.add_argument('--seasons', nargs='+', type=int, required=True)
    backfill.add_argument('--output', default='../research-data')
    backfill.add_argument('--refresh', action='store_true')
    plays = sub.add_parser('nfl-plays', help='Add per-game NFL play metrics from free nflverse archives.')
    plays.add_argument('--games', required=True)
    plays.add_argument('--output', default='../research-data')
    plays.add_argument('--seasons', nargs='+', type=int)
    plays.add_argument('--refresh', action='store_true')
    replay = sub.add_parser('replay', help='Fit only prior games, forecast every target game and evaluate.')
    replay.add_argument('--league', choices=('nfl', 'cfb', 'nhl'), required=True)
    replay.add_argument('--candidate', choices=tuple(CANDIDATES)+NHL_CANDIDATES, required=True)
    replay.add_argument('--games', required=True)
    replay.add_argument('--quotes', help='Optional timestamped entry quote JSONL, using canonical game IDs.')
    replay.add_argument('--closing-quotes', help='Optional same-contract closing quote JSONL.')
    replay.add_argument('--output', required=True)
    replay.add_argument('--start-season', type=int)
    replay.add_argument('--end-season', type=int)
    replay.add_argument('--training-scope', choices=('season', 'expanding'), default='season', help='Football training scope; NHL uses its declared 730-day window.')
    replay.add_argument('--decision-minutes', type=int, default=60)
    replay.add_argument('--max-quote-age-minutes', type=int)
    replay.add_argument('--books', nargs='*', default=[])
    replay.add_argument('--bootstrap-samples', type=int, default=500)
    compare = sub.add_parser('compare', help='Paired forecast comparison on common games, clustered by week.')
    compare.add_argument('--left', required=True)
    compare.add_argument('--right', required=True)
    compare.add_argument('--output', required=True)
    compare.add_argument('--bootstrap-samples', type=int, default=500)
    fit = sub.add_parser('train-nhl', help='Save learned NHL attack/defense coefficients at an explicit cutoff.')
    fit.add_argument('--games', required=True)
    fit.add_argument('--cutoff', required=True, help='ISO UTC cutoff; future/unavailable outcomes are excluded.')
    fit.add_argument('--output', required=True)
    plan = sub.add_parser('odds-plan', help='Build an offline, deduplicated request and credit manifest.')
    plan.add_argument('--games', required=True)
    plan.add_argument('--league', choices=('nfl', 'cfb', 'nhl'))
    plan.add_argument('--seasons', nargs='+', type=int)
    plan.add_argument('--decision-minutes', type=int, default=60)
    plan.add_argument('--closing-minutes', type=int, default=5)
    plan.add_argument('--regions', default='us')
    plan.add_argument('--markets', nargs='+', choices=('moneyline', 'spread', 'total'), default=['moneyline', 'spread', 'total'])
    plan.add_argument('--output', required=True)
    fetch = sub.add_parser('odds-fetch', help='Explicit paid historical request execution with a hard credit cap.')
    fetch.add_argument('--manifest', required=True)
    fetch.add_argument('--output', required=True)
    fetch.add_argument('--max-credits', type=int, required=True)
    fetch.add_argument('--allow-paid', action='store_true', help='Explicitly authorize using ODDS_API_KEY for these paid requests.')
    normalize = sub.add_parser('odds-normalize', help='Match cached historical responses to exact games and contracts, offline.')
    normalize.add_argument('--manifest', required=True)
    normalize.add_argument('--games', required=True)
    normalize.add_argument('--output', required=True)
    normalize.add_argument('--aliases', help='Optional verified team-name mapping JSON.')
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == 'backfill':
        leagues = ('nfl', 'cfb', 'nhl') if args.league == 'all' else (args.league,)
        for league in leagues:
            print(json.dumps(backfill_schedules(league, args.seasons, args.output, refresh=args.refresh), indent=2))
    elif args.command == 'nfl-plays':
        games = read_normalized_games(args.games, league='nfl', seasons=args.seasons)
        print(json.dumps(attach_nfl_play_metrics(games, args.output, seasons=args.seasons, refresh=args.refresh), indent=2))
    elif args.command == 'replay':
        games = read_normalized_games(args.games, league=args.league)
        kwargs = {'decision_minutes': args.decision_minutes, 'start_season': args.start_season,
                  'end_season': args.end_season, 'allowed_books': tuple(args.books)}
        if args.max_quote_age_minutes is not None:
            kwargs['max_quote_age_minutes'] = args.max_quote_age_minutes
        if args.league == 'nhl':
            if args.candidate not in NHL_CANDIDATES:
                raise SystemExit('NHL requires nhl_baseline or nhl_poisson.')
            if args.training_scope != 'season':
                raise SystemExit('--training-scope expanding applies only to football; NHL uses its declared 730-day window.')
            config = NHLReplayConfig(**kwargs)
            rows = run_nhl_replay(games, candidate=args.candidate, quotes=read_rows(args.quotes),
                                  closing_quotes=read_rows(args.closing_quotes), config=config)
        else:
            if args.candidate not in CANDIDATES:
                raise SystemExit('Football requires a football candidate.')
            config = ReplayConfig(training_scope=args.training_scope, **kwargs)
            rows = run_replay(games, league=args.league, candidate=args.candidate,
                              quotes=read_rows(args.quotes), closing_quotes=read_rows(args.closing_quotes), config=config)
        save_replay(rows, args.output, config)
        metrics = evaluate_forecasts(rows, bootstrap_samples=args.bootstrap_samples)
        write_json(Path(args.output)/'metrics.json', metrics)
        print(json.dumps(metrics, indent=2))
    elif args.command == 'compare':
        report = compare_candidates(read_rows(args.left), read_rows(args.right), bootstrap_samples=args.bootstrap_samples)
        write_json(args.output, report)
        print(json.dumps(report, indent=2))
    elif args.command == 'train-nhl':
        artifact = train_nhl_artifact(read_normalized_games(args.games, league='nhl'), args.cutoff)
        write_json(args.output, artifact)
        print(json.dumps({'path': args.output, 'training_games': artifact['training_games'],
                          'training_source_hash': artifact['training_source_hash']}, indent=2))
    elif args.command == 'odds-plan':
        games = read_normalized_games(args.games, league=args.league, seasons=args.seasons)
        manifest = plan_historical_requests(games, decision_minutes=args.decision_minutes,
                                            closing_minutes=args.closing_minutes, regions=args.regions, markets=args.markets)
        write_request_manifest(manifest, args.output)
        print(json.dumps({key: manifest[key] for key in ('manifest_id', 'request_count', 'estimated_credits', 'paid_fetch_performed')}, indent=2))
    elif args.command == 'odds-fetch':
        manifest = json.loads(Path(args.manifest).read_text())
        report = fetch_historical_requests(manifest, args.output, max_credits=args.max_credits, allow_paid=args.allow_paid)
        print(json.dumps(report, indent=2))
    elif args.command == 'odds-normalize':
        manifest = json.loads(Path(args.manifest).read_text())
        aliases = json.loads(Path(args.aliases).read_text()) if args.aliases else None
        print(json.dumps(normalize_fetched_manifest(manifest, read_normalized_games(args.games),
                                                    args.output, aliases=aliases), indent=2))


if __name__ == '__main__':
    main()
