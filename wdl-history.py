#!/usr/bin/env python3
"""
wdl-history.py
Fetches player archive data directly from the Chess.com Public API and tallies 
Win/Draw/Loss stats and Score % (W + 0.5 * D) across 7d, 30d, 90d, and yearly totals.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone, timedelta

# Chess.com API requires a custom User-Agent header
HEADERS = {
    'User-Agent': 'ChessWDLStatsScript/1.0 (Python urllib script)'
}

# Chess.com result codes mapped to outcome types
WIN_RESULTS = {'win'}
DRAW_RESULTS = {'stalemate', 'agreed', 'repetition', 'insufficient', '50move', 'timevsinsufficient'}


def fetch_json(url):
    """Utility to fetch JSON from Chess.com API with retry/rate-limit support."""
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        elif e.code == 429:
            time.sleep(2)
            return fetch_json(url)
        else:
            print(f"HTTP Error {e.code} while fetching {url}", file=sys.stderr)
            return None
    except Exception as e:
        print(f"Error fetching {url}: {e}", file=sys.stderr)
        return None


def calculate_stats(games_tally):
    """Calculates W, D, L, Total, and Score Percentage."""
    wins = games_tally.get('W', 0)
    draws = games_tally.get('D', 0)
    losses = games_tally.get('L', 0)
    total = wins + draws + losses

    if total == 0:
        return 0, 0, 0, 0, 0.0

    score_pct = ((wins + (0.5 * draws)) / total) * 100
    return wins, draws, losses, total, score_pct


def process_player_archives(player_name):
    """Retrieves all monthly archives for a player and processes their game records."""
    player_lower = player_name.lower()
    
    archives_url = f"https://api.chess.com/pub/player/{player_lower}/games/archives"
    print(f"Fetching archive index for '{player_name}'...")
    archives_data = fetch_json(archives_url)

    if not archives_data or 'archives' not in archives_data:
        print(f"Error: Player '{player_name}' not found or has no game archives.")
        sys.exit(1)

    archive_urls = archives_data['archives']
    total_months = len(archive_urls)
    print(f"Found {total_months} monthly archive(s). Processing games...\n")

    parsed_games = []

    for idx, url in enumerate(archive_urls, 1):
        print(f"\rFetching month {idx}/{total_months}...", end="", flush=True)
        month_data = fetch_json(url)
        
        if not month_data or 'games' not in month_data:
            continue

        for game in month_data['games']:
            end_timestamp = game.get('end_time')
            if not end_timestamp:
                continue

            game_date = datetime.fromtimestamp(end_timestamp, tz=timezone.utc)

            white = game.get('white', {})
            black = game.get('black', {})

            if white.get('username', '').lower() == player_lower:
                res = white.get('result', '')
            elif black.get('username', '').lower() == player_lower:
                res = black.get('result', '')
            else:
                continue

            if res in WIN_RESULTS:
                outcome = 'W'
            elif res in DRAW_RESULTS:
                outcome = 'D'
            else:
                outcome = 'L'

            parsed_games.append({'date': game_date, 'outcome': outcome})

    print("\nProcessing complete!\n")
    return parsed_games


def main():
    parser = argparse.ArgumentParser(
        description="Fetch Chess.com archives and tally WDL/Score % for a player."
    )
    parser.add_argument("player", nargs="?", help="Chess.com username")
    args = parser.parse_args()

    # Prompt interactively if player username is omitted
    player = args.player
    while not player:
        try:
            player = input("Enter Chess.com username: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nAborted.")
            sys.exit(0)

    games = process_player_archives(player)

    if not games:
        print(f"No games found for player '{player}'.")
        sys.exit(0)

    # Reference dates
    now = datetime.now(timezone.utc)
    d7 = now - timedelta(days=7)
    d30 = now - timedelta(days=30)
    d90 = now - timedelta(days=90)

    # Buckets
    recent_buckets = {
        "Last 7 Days": defaultdict(int),
        "Last 30 Days": defaultdict(int),
        "Last 90 Days": defaultdict(int),
    }
    
    yearly_buckets = defaultdict(lambda: defaultdict(int))

    # Aggregate outcomes
    for game in games:
        gdate = game['date']
        outcome = game['outcome']
        year = gdate.year

        if gdate >= d7:
            recent_buckets["Last 7 Days"][outcome] += 1
        if gdate >= d30:
            recent_buckets["Last 30 Days"][outcome] += 1
        if gdate >= d90:
            recent_buckets["Last 90 Days"][outcome] += 1

        yearly_buckets[year][outcome] += 1

    # --- Output Results ---
    print(f"=======================================================================")
    print(f" Chess.com WDL Performance Summary: {player}")
    print(f"=======================================================================")
    header_fmt = "{:<20} {:>6} {:>6} {:>6} {:>8} {:>10}"
    row_fmt    = "{:<20} {:>6} {:>6} {:>6} {:>8} {:>9.1f}%"

    print(header_fmt.format("Timeframe", "Win", "Draw", "Loss", "Total", "Score %"))
    print("-" * 65)

    # Print Recent Windows
    for label, tally in recent_buckets.items():
        w, d, l, total, score = calculate_stats(tally)
        print(row_fmt.format(label, w, d, l, total, score))

    print("-" * 65)
    print(" Yearly History:")
    print("-" * 65)

    # Print Yearly Breakdown (Sorted descending by year)
    for yr in sorted(yearly_buckets.keys(), reverse=True):
        w, d, l, total, score = calculate_stats(yearly_buckets[yr])
        print(row_fmt.format(str(yr), w, d, l, total, score))

    print("=======================================================================\n")

if __name__ == "__main__":
    main()
