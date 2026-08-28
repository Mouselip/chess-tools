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
#   for rated games in bullet, blitz, and rapid categories. Reports
#   game counts, latest rating, last played date per category, and the
#   rating spread between highest and lowest categories. Additionally
#   detects suspicious streaks of consecutive short-ply games (0 < ply <= 13)
#   regardless of opponent to identify rating farming, sandbagging, or
#   rapid rating dumping, including win/loss breakdown on the streak header.
#
# Version: v0.0.7

import argparse
import datetime
import json
import re
import sys
import urllib.error
import urllib.request

HEADERS = {
    "User-Agent": "chesscom-rating-summary/0.0.7 (Contact: GitHub/Mouselip)"
}

TARGET_CATEGORIES = ("bullet", "blitz", "rapid")
DEFAULT_MAX_PLY = 13
DEFAULT_MIN_STREAK = 3


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
        description="Scan Chess.com archives for rated games, rating summaries, and consecutive short-ply streaks."
    )
    parser.add_argument("username", help="Chess.com target username")
    parser.add_argument(
        "--max-ply",
        type=int,
        default=DEFAULT_MAX_PLY,
        help=f"Maximum ply threshold for short-game detection (default: {DEFAULT_MAX_PLY})",
    )
    parser.add_argument(
        "--min-streak",
        type=int,
        default=DEFAULT_MIN_STREAK,
        help=f"Minimum consecutive short games to flag as a streak (default: {DEFAULT_MIN_STREAK})",
    )
    args = parser.parse_args()

    username = args.username.strip()
    username_lower = username.lower()
    max_ply = args.max_ply
    min_streak = args.min_streak

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

    all_rated_games = []

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
                opponent_rating = black.get("rating")
            elif black_lower == username_lower:
                player_color = "black"
                player_rating = black.get("rating")
                player_result = black.get("result", "")
                opponent_name = white_user
                opponent_rating = white.get("rating")
            else:
                continue

            end_time = game.get("end_time", 0)

            # Category stats tracking
            if time_class in stats:
                stats[time_class]["count"] += 1
                if end_time >= stats[time_class]["last_played_ts"]:
                    stats[time_class]["last_played_ts"] = end_time
                    stats[time_class]["latest_rating"] = player_rating

            # Collect game metadata for sequential streak analysis
            pgn = game.get("pgn", "")
            ply_count = count_ply_from_pgn(pgn)
            game_url = game.get("url", "")

            if player_result == "win":
                outcome = "WIN"
            elif player_result in ("agreed", "repetition", "stalemate", "timevsinsufficient", "insufficient"):
                outcome = "DRAW"
            else:
                outcome = "LOSS"

            all_rated_games.append({
                "end_time": end_time,
                "time_class": time_class,
                "ply": ply_count,
                "outcome": outcome,
                "color": player_color,
                "opponent": opponent_name,
                "opponent_rating": opponent_rating,
                "rating": player_rating,
                "url": game_url,
            })

    # Sort chronologically by end_time
    all_rated_games.sort(key=lambda g: g["end_time"])

    # Section 1: Rating Summary Output
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

    # Section 2: Chronological Short-Ply Streak Detection
    print(f"\n" + "=" * 62)
    print(f" SUSPICIOUS SHORT-PLY STREAKS (0 < Ply <= {max_ply}, >= {min_streak} Consecutive Games)")
    print("=" * 62)

    streaks = []
    current_streak = []

    for game in all_rated_games:
        if 0 < game["ply"] <= max_ply:
            current_streak.append(game)
        else:
            if len(current_streak) >= min_streak:
                streaks.append(list(current_streak))
            current_streak = []

    if len(current_streak) >= min_streak:
        streaks.append(list(current_streak))

    if not streaks:
        print(f"No consecutive streaks of >= {min_streak} short-ply games detected.")
    else:
        for idx, streak in enumerate(streaks, start=1):
            wins = sum(1 for g in streak if g["outcome"] == "WIN")
            losses = sum(1 for g in streak if g["outcome"] == "LOSS")
            draws = sum(1 for g in streak if g["outcome"] == "DRAW")
            start_dt = datetime.datetime.fromtimestamp(streak[0]["end_time"], tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            end_dt = datetime.datetime.fromtimestamp(streak[-1]["end_time"], tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

            print(f"\n[Streak #{idx}] Length: {len(streak)} games (+{wins} -{losses} ={draws}) | {start_dt} to {end_dt} UTC")
            print("-" * 62)
            for g in streak:
                dt_str = datetime.datetime.fromtimestamp(g["end_time"], tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                opp_info = f"{g['opponent']} ({g['opponent_rating']})"
                print(f"  [{g['outcome']:<4}] {g['time_class'].capitalize():<6} | {g['ply']:>2} ply | vs {opp_info:<22} | {dt_str} UTC | {g['url']}")

    print("\n" + "=" * 62 + "\n")


if __name__ == "__main__":
    main()
