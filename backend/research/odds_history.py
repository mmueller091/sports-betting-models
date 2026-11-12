"""Historical odds planning and exact-contract extraction.

Planning and normalization are offline. Fetching is an explicit, budget-capped
operation and is never triggered by schedule ingestion or a model replay.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import unicodedata
import urllib.parse
from collections import Counter
from datetime import timedelta
from pathlib import Path

from .datasets import parse_time
from .downloads import CachedDownloader, utc_now

SPORT_KEYS = {"nfl": "americanfootball_nfl", "cfb": "americanfootball_ncaaf", "nhl": "icehockey_nhl"}
MARKET_KEYS = {"moneyline": "h2h", "spread": "spreads", "total": "totals", "h2h": "h2h", "spreads": "spreads", "totals": "totals"}
NORMALIZED_MARKETS = {"h2h": "moneyline", "spreads": "spread", "totals": "total"}


def _iso(value):
    parsed = parse_time(value)
    if parsed is None:
        raise ValueError("A timezone-qualified timestamp is required")
    return parsed.isoformat().replace("+00:00", "Z")


def plan_historical_requests(games, *, decision_minutes=60, closing_minutes=5,
                             regions="us", markets=("h2h", "spreads", "totals")):
    """Deduplicate league/timestamp requests, each returning a whole league slate.

    T-5 is a specified closing benchmark, not proof of the final tradable quote.
    The requested timestamp is not rounded forward to a provider snapshot.
    """
    if decision_minutes <= closing_minutes or closing_minutes < 0:
        raise ValueError("Decision cutoff must precede the nonnegative closing cutoff")
    region_values = sorted(set(item.strip() for item in regions.split(",") if item.strip()))
    if not region_values:
        raise ValueError("At least one bookmaker region is required")
    try:
        market_values = sorted(set(MARKET_KEYS[market] for market in markets))
    except KeyError:
        raise ValueError("Only full-game moneyline, spread and total markets are supported") from None
    if not market_values:
        raise ValueError("At least one market is required")
    cost = 10 * len(region_values) * len(market_values)
    requests, excluded = {}, []
    for game in games:
        league, start = game.get("league"), parse_time(game.get("game_time"))
        if game.get("completed") is False:
            excluded.append({"game_id": game.get("game_id"), "reason": "historical_game_not_completed"})
            continue
        if league not in SPORT_KEYS or start is None or game.get("start_time_tbd"):
            excluded.append({"game_id": game.get("game_id"), "reason": "missing_or_unconfirmed_identity_or_start"})
            continue
        for role, minutes in (("decision", decision_minutes), ("closing_benchmark", closing_minutes)):
            when = _iso((start - timedelta(minutes=minutes)).isoformat())
            key = (league, when)
            if key not in requests:
                params = {"regions": ",".join(region_values), "markets": ",".join(market_values),
                          "oddsFormat": "american", "date": when}
                url = "https://api.the-odds-api.com/v4/historical/sports/" + SPORT_KEYS[league] + "/odds?" + urllib.parse.urlencode(params)
                request_id = hashlib.sha256(url.encode()).hexdigest()[:24]
                requests[key] = {"request_id": request_id, "league": league, "sport_key": SPORT_KEYS[league],
                                 "requested_at": when, "url": url, "estimated_credits": cost, "targets": []}
            target = {"game_id": game["game_id"], "decision_time": when, "role": role,
                      "is_closing": role == "closing_benchmark"}
            if target not in requests[key]["targets"]:
                requests[key]["targets"].append(target)
    ordered = sorted(requests.values(), key=lambda item: (item["requested_at"], item["league"]))
    specification = {"decision_minutes": decision_minutes, "closing_minutes": closing_minutes,
                     "regions": region_values, "markets": market_values, "requests": ordered}
    manifest_hash = hashlib.sha256(json.dumps(specification, sort_keys=True).encode()).hexdigest()
    return {"schema_version": 1, "manifest_id": manifest_hash, "created_at": utc_now(),
            "decision_minutes": decision_minutes, "closing_minutes": closing_minutes,
            "closing_definition": "latest_snapshot_at_or_before_T_minus_cutoff_not_verified_final_close",
            "regions": region_values, "markets": market_values,
            "request_count": len(ordered), "estimated_credits": sum(item["estimated_credits"] for item in ordered),
            "pricing_basis": "10_credits_per_region_per_market_historical_league_snapshot",
            "requests": ordered, "excluded_games": excluded, "paid_fetch_performed": False}


def write_request_manifest(manifest, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _verify_manifest_hash(manifest):
    manifest_id = str(manifest.get("manifest_id", ""))
    if not re.fullmatch(r"[a-f0-9]{64}", manifest_id):
        raise ValueError("A generated historical request manifest is required")
    try:
        specification = {field: manifest[field] for field in ("decision_minutes", "closing_minutes", "regions", "markets", "requests")}
    except KeyError:
        raise ValueError("Historical request manifest is incomplete") from None
    if hashlib.sha256(json.dumps(specification, sort_keys=True).encode()).hexdigest() != manifest_id:
        raise ValueError("Historical request manifest hash mismatch")
    return manifest_id


def _name(value):
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(character for character in text if not unicodedata.combining(character))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _identity_matches(event, game, aliases, tolerance_seconds):
    explicit_id = game.get("odds_event_id") or game.get("provider_ids", {}).get("odds_api")
    start, candidate = parse_time(game.get("game_time")), parse_time(event.get("commence_time"))
    if start is None or candidate is None or abs((start - candidate).total_seconds()) > tolerance_seconds:
        return False
    if explicit_id:
        return str(event.get("id")) == str(explicit_id)
    for side in ("home", "away"):
        expected = [game.get(side + "_team", "")] + list(game.get(side + "_team_aliases", []))
        expected = {_name(aliases.get(_name(value), value)) for value in expected if value}
        observed = _name(aliases.get(_name(event.get(side + "_team")), event.get(side + "_team")))
        if observed not in expected:
            return False
    return True


def normalize_historical_quotes(response, games, decision_time, *, is_closing=False,
                                retrieved_at=None, aliases=None, time_tolerance_seconds=300,
                                diagnostics=None):
    """Return exact offered contracts from one provider snapshot.

    Explicit cross-provider game IDs are preferred. Otherwise both team names
    must match verified aliases and start times must agree within five minutes.
    No fuzzy name matching, cross-book pairing, or fabricated -110 prices.
    """
    cutoff = parse_time(decision_time)
    snapshot = parse_time(response.get("timestamp"))
    if cutoff is None or snapshot is None or snapshot > cutoff:
        raise ValueError("Historical snapshot is missing or later than the decision cutoff")
    if not isinstance(response.get("data"), list):
        raise ValueError("Historical odds response lacks its event list")
    diagnostics = diagnostics if diagnostics is not None else []
    aliases = {_name(key): value for key, value in (aliases or {}).items()}
    quotes = []
    for game in games:
        kickoff = parse_time(game.get("game_time"))
        if kickoff is None or cutoff >= kickoff or snapshot >= kickoff:
            diagnostics.append({"game_id": game.get("game_id"), "reason": "cutoff_not_pregame"})
            continue
        matches = [event for event in response["data"]
                   if event.get("sport_key") == SPORT_KEYS.get(game.get("league"))
                   and _identity_matches(event, game, aliases, time_tolerance_seconds)]
        if len(matches) != 1:
            diagnostics.append({"game_id": game.get("game_id"), "reason": "event_not_uniquely_matched", "matches": len(matches)})
            continue
        event = matches[0]
        for book in event.get("bookmakers", []):
            if not book.get("key"):
                continue
            for market in book.get("markets", []):
                market_key = market.get("key")
                if market_key not in NORMALIZED_MARKETS:
                    continue
                quote_time = parse_time(market.get("last_update") or book.get("last_update"))
                if quote_time is None or quote_time > snapshot:
                    diagnostics.append({"game_id": game["game_id"], "book_key": book["key"],
                                        "reason": "missing_or_future_quote_timestamp"})
                    continue
                for outcome in market.get("outcomes", []):
                    if market_key == "totals":
                        side = str(outcome.get("name", "")).lower()
                        if side not in ("over", "under"):
                            continue
                    else:
                        side = "home" if outcome.get("name") == event.get("home_team") else "away" if outcome.get("name") == event.get("away_team") else None
                        if side is None:
                            continue
                    try:
                        odds = float(outcome["price"])
                        point = None if market_key == "h2h" else float(outcome["point"])
                        if not math.isfinite(odds) or abs(odds) < 100 or (point is not None and not math.isfinite(point)):
                            raise ValueError
                    except (KeyError, ValueError, TypeError):
                        diagnostics.append({"game_id": game["game_id"], "book_key": book["key"], "reason": "invalid_contract_price_or_line"})
                        continue
                    quotes.append({"game_id": game["game_id"], "league": game["league"], "odds_event_id": event["id"],
                                   "market": NORMALIZED_MARKETS[market_key], "side": side, "line": point,
                                   "odds": odds, "book_key": book["key"], "book_title": book.get("title", book["key"]),
                                   "quote_time": _iso(quote_time.isoformat()), "snapshot_time": _iso(snapshot.isoformat()),
                                   "retrieved_at": retrieved_at, "decision_time": _iso(cutoff.isoformat()),
                                   "is_closing": bool(is_closing), "commence_time": _iso(kickoff.isoformat()),
                                   "source": "the_odds_api_historical_snapshot"})
    return quotes


def exact_contract_quote(quotes, *, game_id, market, side, line, book_key,
                         decision_time, max_age_seconds=None, is_closing=False):
    """Select a contract only from the latest eligible snapshot for that book.

    Filtering to a requested line *before* selecting the newest snapshot would
    let an obsolete +3.5 survive after the book moved to +2.5. Do not do that.
    """
    cutoff = parse_time(decision_time)
    if cutoff is None:
        raise ValueError("Invalid decision timestamp")
    candidates = []
    for quote in quotes:
        if (quote.get("game_id"), quote.get("market"), quote.get("book_key"), bool(quote.get("is_closing"))) != (game_id, market, book_key, bool(is_closing)):
            continue
        snapshot, updated = parse_time(quote.get("snapshot_time")), parse_time(quote.get("quote_time"))
        if snapshot is not None and updated is not None and updated <= snapshot <= cutoff:
            candidates.append((snapshot, quote))
    if not candidates:
        return None
    newest = max(timestamp for timestamp, _ in candidates)
    matches = [quote for timestamp, quote in candidates if timestamp == newest
               and quote.get("side") == side and quote.get("line") == line]
    if not matches:
        return None
    if len({(quote.get("odds"), quote.get("quote_time")) for quote in matches}) > 1:
        raise ValueError("Conflicting prices for the same book, contract and snapshot")
    chosen = matches[0]
    if max_age_seconds is not None and (cutoff - parse_time(chosen["quote_time"])).total_seconds() > max_age_seconds:
        return None
    return dict(chosen)


def _fetch_historical_requests_locked(manifest, output_dir, *, max_credits, allow_paid=False,
                                     api_key=None, downloader=None):
    """Explicit paid download with persistent conservative credit reservations.

    There are no automatic retries. Failed requests retain their reserved credit
    cost because a client cannot assume the provider did not charge them.
    Re-running resumes cached successes and skips prior attempts in this manifest.
    """
    if allow_paid is not True:
        raise ValueError("Paid historical fetch requires explicit allow_paid=True and a credit budget")
    if isinstance(max_credits, bool) or not isinstance(max_credits, int) or max_credits <= 0:
        raise ValueError("max_credits must be a positive integer")
    key = api_key or os.getenv("ODDS_API_KEY")
    if not key:
        raise ValueError("ODDS_API_KEY is required for an explicitly approved historical fetch")
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    downloader = downloader or CachedDownloader(root / "odds_cache", retries=0)
    if downloader.retries != 0:
        raise ValueError("Budgeted historical fetch requires a downloader with retries=0")
    manifest_id = _verify_manifest_hash(manifest)
    for request in manifest["requests"]:
        parts = urllib.parse.urlsplit(request["url"])
        parameters = urllib.parse.parse_qs(parts.query)
        expected_path = "/v4/historical/sports/" + SPORT_KEYS.get(request.get("league"), "invalid") + "/odds"
        if (parts.scheme, parts.netloc, parts.path) != ("https", "api.the-odds-api.com", expected_path):
            raise ValueError("Historical requests must target the approved provider endpoint")
        if set(parameters) != {"regions", "markets", "oddsFormat", "date"} or any(len(values) != 1 for values in parameters.values()):
            raise ValueError("Unexpected historical request parameters")
        requested_markets = set(parameters["markets"][0].split(","))
        requested_regions = set(parameters["regions"][0].split(","))
        actual_cost = 10 * len(requested_markets) * len(requested_regions)
        if (not requested_markets.issubset(NORMALIZED_MARKETS) or not requested_regions.issubset({"us", "us2", "uk", "au", "eu", "ca", "us_dfs"})
                or parameters["oddsFormat"] != ["american"] or parse_time(parameters["date"][0]) is None
                or request["estimated_credits"] != actual_cost):
            raise ValueError("Historical request market, region, date or cost is invalid")
    ledger_path = root / ("odds_fetch_ledger_" + manifest_id + ".json")
    ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else {
        "manifest_id": manifest_id, "reserved_credits": 0, "attempts": {}, "results": []}
    if ledger["reserved_credits"] > max_credits:
        raise ValueError("Previously reserved credits exceed the supplied budget")

    def persist():
        temporary = ledger_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(ledger, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(ledger_path)

    for request in manifest["requests"]:
        request_id = request["request_id"]
        if request_id in ledger["attempts"]:
            continue
        authenticated_url = request["url"] + "&" + urllib.parse.urlencode({"apiKey": key})
        cached = downloader.cached(authenticated_url)
        cost = int(request["estimated_credits"])
        if cached is None and ledger["reserved_credits"] + cost > max_credits:
            break
        if cached is None:
            ledger["reserved_credits"] += cost
        ledger["attempts"][request_id] = {"status": "reserved", "reserved_credits": cost if cached is None else 0,
                                           "requested_at": request["requested_at"]}
        persist()
        try:
            resource = cached or downloader.fetch(authenticated_url)
            payload = json.loads(resource.read_bytes())
            if parse_time(payload.get("timestamp")) is None or not isinstance(payload.get("data"), list):
                raise ValueError("Historical response schema is invalid")
            ledger["attempts"][request_id]["status"] = "cached" if cached else "downloaded"
            ledger["results"].append({"request_id": request_id, "path": str(resource.path),
                                      "sha256": resource.sha256, "retrieved_at": resource.retrieved_at,
                                      "timestamp": payload["timestamp"], "targets": request["targets"]})
        except (RuntimeError, ValueError, OSError):
            ledger["attempts"][request_id]["status"] = "failed"
            ledger["attempts"][request_id]["error"] = "Historical request failed; no unverified response used"
        persist()
    ledger["max_credits"] = max_credits
    ledger["remaining_planned_requests"] = len(manifest["requests"]) - len(ledger["attempts"])
    ledger["paid_fetch_performed"] = any(item["reserved_credits"] for item in ledger["attempts"].values())
    persist()
    return ledger


def fetch_historical_requests(manifest, output_dir, *, max_credits, allow_paid=False,
                              api_key=None, downloader=None):
    """One paid fetch process per manifest; reserve credits before every call."""
    if allow_paid is not True:
        raise ValueError("Paid historical fetch requires explicit allow_paid=True and a credit budget")
    manifest_id = _verify_manifest_hash(manifest)
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    lock = root / (".odds_fetch_" + manifest_id + ".lock")
    try:
        lock.mkdir()
    except FileExistsError:
        raise RuntimeError("A historical fetch is already locked for this manifest; check the running process before removing a stale lock") from None
    try:
        return _fetch_historical_requests_locked(manifest, output_dir, max_credits=max_credits,
                                                 allow_paid=allow_paid, api_key=api_key, downloader=downloader)
    finally:
        lock.rmdir()


def normalize_fetched_manifest(manifest, games, output_dir, aliases=None):
    """Turn saved successful responses into entry/closing JSONL for replay.

    Does not access the network or require a key. Snapshot hashes and target
    timestamps are checked again before a cached response becomes a quote.
    """
    root = Path(output_dir)
    manifest_id = _verify_manifest_hash(manifest)
    ledger_path = root / ("odds_fetch_ledger_" + manifest_id + ".json")
    if not ledger_path.exists():
        raise ValueError("No historical fetch ledger exists for this request manifest")
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    if ledger.get("manifest_id") != manifest_id:
        raise ValueError("Historical fetch ledger belongs to a different manifest")
    games_by_id = {game["game_id"]: game for game in games}
    requests_by_id = {request["request_id"]: request for request in manifest["requests"]}
    diagnostics, combined, loaded = [], [], set()
    for result in ledger.get("results", []):
        request_id = result.get("request_id")
        request = requests_by_id.get(request_id)
        status = ledger.get("attempts", {}).get(request_id, {}).get("status")
        if request is None or status not in ("cached", "downloaded"):
            continue
        digest = result.get("sha256", "")
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("Historical response has no valid content hash")
        raw = Path(result["path"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("Historical response content hash mismatch")
        payload = json.loads(raw)
        for target in request.get("targets", []):
            game = games_by_id.get(target["game_id"])
            if game is None:
                diagnostics.append({"game_id": target["game_id"], "request_id": request_id, "reason": "game_missing_from_normalized_dataset"})
                continue
            local_diagnostics = []
            quotes = normalize_historical_quotes(payload, [game], target["decision_time"],
                                                  is_closing=target["is_closing"],
                                                  retrieved_at=result.get("retrieved_at"), aliases=aliases,
                                                  diagnostics=local_diagnostics)
            for quote in quotes:
                quote["request_id"] = request_id
                quote["response_sha256"] = digest
                combined.append(quote)
            diagnostics.extend({**item, "request_id": request_id, "is_closing": target["is_closing"]}
                               for item in local_diagnostics)
        loaded.add(request_id)
    unique = {}
    identity = ("game_id", "market", "side", "line", "book_key", "snapshot_time", "decision_time", "is_closing")
    for quote in combined:
        key = tuple(quote.get(field) for field in identity)
        if key in unique and (unique[key]["odds"], unique[key]["quote_time"]) != (quote["odds"], quote["quote_time"]):
            raise ValueError("Conflicting duplicate historical contracts")
        unique[key] = quote
    all_quotes = sorted(unique.values(), key=lambda quote: (quote["decision_time"], quote["game_id"], quote["book_key"], quote["market"], quote["side"]))
    entries = [quote for quote in all_quotes if not quote["is_closing"]]
    closing = [quote for quote in all_quotes if quote["is_closing"]]
    root.mkdir(parents=True, exist_ok=True)
    for name, rows in (("entry_quotes.jsonl", entries), ("closing_quotes.jsonl", closing)):
        destination = root / name
        temporary = destination.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as output:
            for row in rows:
                output.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
        temporary.replace(destination)
    diagnostics_path = root / "historical_odds_diagnostics.json"
    diagnostics_path.write_text(json.dumps(diagnostics, indent=2), encoding="utf-8")
    requested_games = {target["game_id"] for request in manifest["requests"] for target in request["targets"] if not target["is_closing"]}
    report = {"manifest_id": manifest_id, "generated_at": utc_now(),
              "requests_planned": len(requests_by_id), "responses_available": len(loaded),
              "unavailable_requests": len(requests_by_id) - len(loaded),
              "games_requested": len(requested_games), "games_with_entry_quotes": len({quote["game_id"] for quote in entries}),
              "games_with_closing_quotes": len({quote["game_id"] for quote in closing}),
              "entry_quote_rows": len(entries), "closing_quote_rows": len(closing),
              "entry_path": str(root / "entry_quotes.jsonl"), "closing_path": str(root / "closing_quotes.jsonl"),
              "diagnostics_path": str(diagnostics_path), "diagnostic_counts": dict(Counter(item["reason"] for item in diagnostics)),
              "closing_definition": manifest.get("closing_definition"), "network_requests_made": 0}
    (root / "historical_odds_coverage.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report
