#!/usr/bin/env python3
"""
wdl-history.py
Fetches player archive data directly from the Chess.com Public API and tallies 
Win/Draw/Loss stats and Score (W + 0.5 * D) with Score % across 7d, 30d, 90d, Year to Date, 
and yearly totals, broken down by time control class (Daily, Bullet, Blitz, Rapid).
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
TIME_CLASSES = ["daily", "bullet", "blitz", "rapid"]


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


def classify_game(time_control):
    """Classify game matching the cc_archive_splitter logic."""
    tc = str(time_control).strip() if time_control else ""

    if not tc or tc in ("-", "?"):
        return None

    try:
        if tc.startswith("1/"):
            return "daily"

        if "+" in tc:
            base, inc = map(int, tc.split("+")[:2])
        else:
            base = int(tc)
            inc = 0

        estimated = base + 40 * inc

        if estimated < 180:
            return "bullet"
        elif estimated < 600:
            return "blitz"
        else:
            return "rapid"

    except Exception:
        return None


def calculate_stats(games_tally):
    """Calculates W, D, L, Total, Raw Score (W + 0.5*D), and Score Percentage."""
    wins = games_tally.get('W', 0)
    draws = games_tally.get('D', 0)
    losses = games_tally.get('L', 0)
    total = wins + draws + losses

    if total == 0:
        return 0, 0, 0, 0, 0.0, 0.0

    raw_score = wins + (0.5 * draws)
    score_pct = (raw_score / total) * 100
    return wins, draws, losses, total, raw_score, score_pct


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
            time_control = game.get('time_control')
            kind = classify_game(time_control)

            if not kind or kind not in TIME_CLASSES:
                continue

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

            parsed_games.append({
                'date': game_date, 
                'outcome': outcome, 
                'time_class': kind
            })

    print("\nProcessing complete!\n")
    return parsed_games


def main():
    parser = argparse.ArgumentParser(
        description="Fetch Chess.com archives and tally WDL/Score for a player."
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
    ytd_start = datetime(now.year, 1, 1, tzinfo=timezone.utc)

    # Data structure: timeframe -> time_class -> W/D/L tallies
    timeframes = ["7 Days", "30 Days", "90 Days", "Year to Date"]
    stats = {tf: {tc: defaultdict(int) for tc in TIME_CLASSES} for tf in timeframes}
    stats["Yearly"] = defaultdict(lambda: {tc: defaultdict(int) for tc in TIME_CLASSES})

    # Aggregate outcomes
    for game in games:
        gdate = game['date']
        outcome = game['outcome']
        tc = game['time_class']
        year = gdate.year

        if gdate >= d7:
            stats["7 Days"][tc][outcome] += 1
        if gdate >= d30:
            stats["30 Days"][tc][outcome] += 1
        if gdate >= d90:
            stats["90 Days"][tc][outcome] += 1
        if gdate >= ytd_start:
            stats["Year to Date"][tc][outcome] += 1

        stats["Yearly"][year][tc][outcome] += 1

    # Format layout widths
    W_CLASS = 18
    W_WIN   = 6
    W_DRAW  = 6
    W_LOSS  = 6
    W_TOT   = 8
    W_SCORE = 16
    TOTAL_WIDTH = W_CLASS + W_WIN + W_DRAW + W_LOSS + W_TOT + W_SCORE + 15

    print("\n" + "=" * TOTAL_WIDTH)
    print(f"  WDL PERFORMANCE SUMMARY FOR: {player}")
    print("=" * TOTAL_WIDTH)

    def print_section(section_label, tc_dict):
        # Filter down to classes that have at least 1 game in this timeframe
        active_classes = [tc for tc in TIME_CLASSES if sum(tc_dict[tc].values()) > 0]
        if not active_classes:
            return

        print(f"\n{section_label}")
        print("-" * TOTAL_WIDTH)
        header_str = f"{'':<{W_CLASS}} | {'Win':>{W_WIN}} | {'Draw':>{W_DRAW}} | {'Loss':>{W_LOSS}} | {'Total':>{W_TOT}} | {'Score (%)':>{W_SCORE}}"
        print(header_str)
        print("-" * TOTAL_WIDTH)

        for tc in active_classes:
            w, d, l, total, score, score_pct = calculate_stats(tc_dict[tc])
            score_str = f"{score:.1f} ({score_pct:.1f}%)"
            row_str = f"  {tc.capitalize():<{W_CLASS - 2}} | {w:>{W_WIN}} | {d:>{W_DRAW}} | {l:>{W_LOSS}} | {total:>{W_TOT}} | {score_str:>{W_SCORE}}"
            print(row_str)

    # Print relative periods
    for window in ["7 Days", "30 Days", "90 Days", "Year to Date"]:
        print_section(window, stats[window])

    # Print Yearly Breakdown
    if stats["Yearly"]:
        print("\n\n" + "=" * TOTAL_WIDTH)
        print("  YEARLY BREAKDOWN")
        print("=" * TOTAL_WIDTH)

        for year in sorted(stats["Yearly"].keys(), reverse=True):
            print_section(str(year), stats["Yearly"][year])

    print()

if __name__ == "__main__":
    main()
