#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright (c) 2026 Tyrin R. Price
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
#
# Description:
#   Scans the Chess.com public archives for a target player, filtering
#   for rated games in the bullet, blitz, and rapid categories. Reports
#   game counts, latest rating, and last played date per category, and
#   calculates the rating spread between the highest and lowest categories.
#   Additionally inspects the archive for suspicious short-ply games
#   (<= 4 ply / 2 full moves) to detect potential rating farming or dumping
#   involving repeated patterns (>= 3 short games) against the same opponent.
#
# Version: v0.0.3

import argparse
import collections
import datetime
import json
import re
import sys
import urllib.error
import urllib.request

HEADERS = {
    "User-Agent": "chesscom-rating-summary/0.0.3 (Contact: GitHub/Mouselip)"
}

TARGET_CATEGORIES = ("bullet", "blitz", "rapid")
MAX_PLY_THRESHOLD = 4
MIN_REPEATS_THRESHOLD = 3


def fetch_json(url):
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(f"[-] Error: Resource not found at {url}", file=sys.stderr)
        else:
            print(f"[-] HTTP Error {e.code}: {e.reason}", file=sys.stderr)
    except urllib.error.URLError as e:
        print(f"[-] URL Error: {e.reason}", file=sys.stderr)
    except Exception as e:
        print(f"[-] Unexpected error: {e}", file=sys.stderr)
    return None


def count_ply_from_pgn(pgn_str):
    if not pgn_str:
        return 0
    clean_pgn = re.sub(r"\[.*?\]", "", pgn_str)
    clean_pgn = re.sub(r"\{.*?\}", "", clean_pgn)
    clean_pgn = re.sub(r"\d+\.\.\.", "", clean_pgn)
    tokens = clean_pgn.strip().split()
    moves = []
    for token in tokens:
        if re.match(r"^\d+\.$", token):
            continue
        token = re.sub(r"^\d+\.", "", token)
        if token in ("1-0", "0-1", "1/2-1/2", "*"):
            continue
        if token:
            moves.append(token)
    return len(moves)


def main():
    parser = argparse.ArgumentParser(
        description="Scan Chess.com archives for rated games, rating summaries, and suspicious short-ply patterns."
    )
    parser.add_argument("username", help="Chess.com target username")
    parser.add_argument(
        "--max-ply",
        type=int,
        default=MAX_PLY_THRESHOLD,
        help=f"Maximum ply threshold for short-game detection (default: {MAX_PLY_THRESHOLD})",
    )
    parser.add_argument(
        "--min-repeats",
        type=int,
        default=MIN_REPEATS_THRESHOLD,
        help=f"Minimum short-game encounters against an opponent to report (default: {MIN_REPEATS_THRESHOLD})",
    )
    args = parser.parse_args()

    username = args.username.strip()
    username_lower = username.lower()
    max_ply = args.max_ply
    min_repeats = args.min_repeats

    archives_url = f"https://api.chess.com/pub/player/{username_lower}/games/archives"
    archives_data = fetch_json(archives_url)

    if not archives_data or "archives" not in archives_data:
        print(f"[-] Failed to retrieve archives for player: {username}", file=sys.stderr)
        sys.exit(1)

    archive_urls = archives_data.get("archives", [])
    if not archive_urls:
        print(f"[-] No game archives found for {username}.")
        sys.exit(0)

    stats = {
        cat: {
            "count": 0,
            "latest_rating": None,
            "last_played_ts": 0,
        }
        for cat in TARGET_CATEGORIES
    }

    # Tracking short games: { opponent_username: { "wins": [details], "losses": [details], "draws": [details] } }
    short_game_tracker = collections.defaultdict(lambda: {"wins": [], "losses": [], "draws": []})

    total_months = len(archive_urls)
    print(f"[*] Scanning {total_months} monthly archives for '{username}'...")

    for month_url in archive_urls:
        month_data = fetch_json(month_url)
        if not month_data:
            continue

        for game in month_data.get("games", []):
            if not game.get("rated", False):
                continue

            time_class = game.get("time_class", "").lower()
            white = game.get("white", {})
            black = game.get("black", {})

            white_user = white.get("username", "")
            black_user = black.get("username", "")

            white_lower = white_user.lower()
            black_lower = black_user.lower()

            if white_lower == username_lower:
                player_color = "white"
                player_rating = white.get("rating")
                player_result = white.get("result", "")
                opponent_name = black_user
            elif black_lower == username_lower:
                player_color = "black"
                player_rating = black.get("rating")
                player_result = black.get("result", "")
                opponent_name = white_user
            else:
                continue

            end_time = game.get("end_time", 0)

            # Rating category tracking
            if time_class in stats:
                stats[time_class]["count"] += 1
                if end_time >= stats[time_class]["last_played_ts"]:
                    stats[time_class]["last_played_ts"] = end_time
                    stats[time_class]["latest_rating"] = player_rating

            # Short ply analysis
            pgn = game.get("pgn", "")
            ply_count = count_ply_from_pgn(pgn)

            if ply_count <= max_ply:
                game_url = game.get("url", "")
                entry = {
                    "time_class": time_class,
                    "ply": ply_count,
                    "end_time": end_time,
                    "url": game_url,
                    "color": player_color,
                }
                if player_result == "win":
                    short_game_tracker[opponent_name]["wins"].append(entry)
                elif player_result in ("agreed", "repetition", "stalemate", "timevsinsufficient", "insufficient"):
                    short_game_tracker[opponent_name]["draws"].append(entry)
                else:
                    short_game_tracker[opponent_name]["losses"].append(entry)

    # Section 1: Rating Summary
    print("\n" + "=" * 62)
    print(f" RATED RATING SUMMARY: {username}")
    print("=" * 62)
    print(f"{'Category':<10} | {'Games':<8} | {'Rating':<8} | {'Last Played (UTC)':<20}")
    print("-" * 62)

    active_categories = {}
    for cat in TARGET_CATEGORIES:
        data = stats[cat]
        count = data["count"]
        rating = data["latest_rating"]
        ts = data["last_played_ts"]

        if count > 0 and rating is not None:
            dt_str = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            rating_str = str(rating)
            active_categories[cat] = rating
        else:
            dt_str = "N/A"
            rating_str = "N/A"

        print(f"{cat.capitalize():<10} | {count:<8} | {rating_str:<8} | {dt_str:<20}")

    print("-" * 62)

    if len(active_categories) >= 2:
        highest_cat = max(active_categories, key=active_categories.get)
        lowest_cat = min(active_categories, key=active_categories.get)
        highest_rating = active_categories[highest_cat]
        lowest_rating = active_categories[lowest_cat]
        spread = highest_rating - lowest_rating

        print(f"Highest Rated Category : {highest_cat.capitalize()} ({highest_rating})")
        print(f"Lowest Rated Category  : {lowest_cat.capitalize()} ({lowest_rating})")
        print(f"Rating Difference      : {spread} points")
    elif len(active_categories) == 1:
        cat, rating = next(iter(active_categories.items()))
        print(f"Only one active rated category found: {cat.capitalize()} ({rating}). Spread not applicable.")
    else:
        print("No rated games found in bullet, blitz, or rapid categories.")

    print("=" * 62)

    # Section 2: Short-Ply Pairing Analysis
    print(f"\n" + "=" * 62)
    print(f" SUSPICIOUS SHORT-PLY REPORT (<= {max_ply} Ply, >= {min_repeats} Games)")
    print("=" * 62)

    flagged_opponents = 0

    for opponent, records in short_game_tracker.items():
        total_short = len(records["wins"]) + len(records["losses"]) + len(records["draws"])
        if total_short >= min_repeats:
            flagged_opponents += 1
            print(f"\nOpponent: {opponent}")
            print(f"Total Short Games (<= {max_ply} ply): {total_short} (Wins: {len(records['wins'])}, Losses: {len(records['losses'])}, Draws: {len(records['draws'])})")
            print("-" * 62)

            for outcome, label in (("wins", "WIN"), ("losses", "LOSS"), ("draws", "DRAW")):
                for g in records[outcome]:
                    dt_str = datetime.datetime.fromtimestamp(g["end_time"], tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                    print(f"  [{label:<4}] {g['time_class'].capitalize():<6} | {g['ply']} ply | Played {g['color'].capitalize():<5} | {dt_str} UTC | {g['url']}")

    if flagged_opponents == 0:
        print(f"No repeated short-ply patterns detected against the same opponent (>= {min_repeats} games).")

    print("\n" + "=" * 62 + "\n")


if __name__ == "__main__":
    main()
