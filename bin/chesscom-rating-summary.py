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
#   rating spread between highest and lowest categories. Detects
#   suspicious streaks of consecutive short-ply games (0 < ply <= 13)
#   regardless of opponent to identify rating farming, sandbagging, or
#   rapid rating dumping with event categorization and dual player/opponent
#   ratings. Additionally provides comprehensive rating trajectory analysis
#   including initial account onboarding profiles, long periods of dormancy/
#   inactivity, high-velocity rating surges, and post-dormancy reactivation
#   surge alerts.
#
# Version: v0.0.9

import argparse
import datetime
import json
import re
import sys
import urllib.error
import urllib.request

HEADERS = {
    "User-Agent": "chesscom-rating-summary/0.0.9 (Contact: GitHub/Mouselip)"
}

TARGET_CATEGORIES = ("bullet", "blitz", "rapid")
DEFAULT_MAX_PLY = 13
DEFAULT_MIN_STREAK = 3
DEFAULT_ONBOARDING_GAMES = 30
DEFAULT_MIN_GAP_DAYS = 30
DEFAULT_SURGE_PTS = 200
DEFAULT_SURGE_GAMES = 20


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


def extract_event_type(pgn_str):
    if not pgn_str:
        return "Pool"
    match = re.search(r'\[Event\s+"([^"]+)"\]', pgn_str)
    if not match:
        return "Pool"
    event_str = match.group(1).lower()
    if "arena" in event_str:
        return "Arena"
    elif "tournament" in event_str or "tourney" in event_str or "swiss" in event_str:
        return "Tourney"
    return "Pool"


def main():
    parser = argparse.ArgumentParser(
        description="Scan Chess.com archives for rated games, summaries, short-ply streaks, dormancy, and surges."
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
    parser.add_argument(
        "--onboarding-games",
        type=int,
        default=DEFAULT_ONBOARDING_GAMES,
        help=f"Number of initial games per category for onboarding profile (default: {DEFAULT_ONBOARDING_GAMES})",
    )
    parser.add_argument(
        "--min-gap-days",
        type=int,
        default=DEFAULT_MIN_GAP_DAYS,
        help=f"Minimum days of inactivity between games to flag dormancy (default: {DEFAULT_MIN_GAP_DAYS})",
    )
    parser.add_argument(
        "--surge-pts",
        type=int,
        default=DEFAULT_SURGE_PTS,
        help=f"Minimum rating gain to flag as a surge (default: {DEFAULT_SURGE_PTS})",
    )
    parser.add_argument(
        "--surge-games",
        type=int,
        default=DEFAULT_SURGE_GAMES,
        help=f"Maximum game window for a surge to occur (default: {DEFAULT_SURGE_GAMES})",
    )
    args = parser.parse_args()

    username = args.username.strip()
    username_lower = username.lower()
    max_ply = args.max_ply
    min_streak = args.min_streak
    onboarding_limit = args.onboarding_games
    min_gap_seconds = args.min_gap_days * 86400
    surge_pts = args.surge_pts
    surge_games = args.surge_games

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
    category_games = {cat: [] for cat in TARGET_CATEGORIES}

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

            # Collect game metadata for sequential streak and trajectory analysis
            pgn = game.get("pgn", "")
            ply_count = count_ply_from_pgn(pgn)
            event_type = extract_event_type(pgn)
            game_url = game.get("url", "")

            if player_result == "win":
                outcome = "WIN"
            elif player_result in ("agreed", "repetition", "stalemate", "timevsinsufficient", "insufficient"):
                outcome = "DRAW"
            else:
                outcome = "LOSS"

            game_obj = {
                "end_time": end_time,
                "time_class": time_class,
                "ply": ply_count,
                "outcome": outcome,
                "color": player_color,
                "event_type": event_type,
                "opponent": opponent_name,
                "opponent_rating": opponent_rating if opponent_rating is not None else 0,
                "player_rating": player_rating if player_rating is not None else 0,
                "url": game_url,
            }

            all_rated_games.append(game_obj)
            if time_class in category_games:
                category_games[time_class].append(game_obj)

    # Sort chronologically by end_time
    all_rated_games.sort(key=lambda g: g["end_time"])
    for cat in TARGET_CATEGORIES:
        category_games[cat].sort(key=lambda g: g["end_time"])

    # Section 1: Rating Summary Output
    print("\n" + "=" * 78)
    print(f" RATED RATING SUMMARY: {username}")
    print("=" * 78)
    print(f"{'Category':<10} | {'Games':<8} | {'Rating':<8} | {'Last Played (UTC)':<20}")
    print("-" * 78)

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

    print("-" * 78)

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

    print("=" * 78)

    # Section 2: Chronological Short-Ply Streak Detection
    print(f"\n" + "=" * 78)
    print(f" SUSPICIOUS SHORT-PLY STREAKS (0 < Ply <= {max_ply}, >= {min_streak} Consecutive Games)")
    print("=" * 78)

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
            print("-" * 78)
            for g in streak:
                dt_str = datetime.datetime.fromtimestamp(g["end_time"], tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                matchup_info = f"vs {g['opponent']} ({g['player_rating']}/{g['opponent_rating']})"
                print(f"  [{g['outcome']:<4}] {g['time_class'].capitalize():<6} | {g['ply']:>2} ply | {g['event_type']:<7} | {matchup_info:<30} | {dt_str} UTC | {g['url']}")

    # Section 3: Rating Trajectory, Dormancy & Surge Analysis
    print(f"\n" + "=" * 78)
    print(" RATING TRAJECTORY, DORMANCY & SURGE ANALYSIS")
    print("=" * 78)

    # 3A: Initial Account Onboarding
    print(f"\n-- INITIAL ACCOUNT ONBOARDING (First {onboarding_limit} Games per Pool) " + "-" * (78 - len(f"-- INITIAL ACCOUNT ONBOARDING (First {onboarding_limit} Games per Pool) ")))
    print(f"{'Category':<10} | {'Initial -> End':<19} | {'Delta':<8} | {'Win Rate':<9} | {'Max Streak':<11} | {'Avg Opponent':<12}")
    print("-" * 78)

    for cat in TARGET_CATEGORIES:
        games = category_games[cat]
        if not games:
            print(f"{cat.capitalize():<10} | {'No games played':<19} | {'N/A':<8} | {'N/A':<9} | {'N/A':<11} | {'N/A':<12}")
            continue

        sample = games[:onboarding_limit]
        sample_count = len(sample)
        init_rating = sample[0]["player_rating"]
        final_rating = sample[-1]["player_rating"]
        delta = final_rating - init_rating
        delta_str = f"{delta:+d}"

        wins = sum(1 for g in sample if g["outcome"] == "WIN")
        win_rate = (wins / sample_count) * 100.0 if sample_count > 0 else 0.0

        max_streak_len = 0
        curr_streak_len = 0
        for g in sample:
            if g["outcome"] == "WIN":
                curr_streak_len += 1
                if curr_streak_len > max_streak_len:
                    max_streak_len = curr_streak_len
            else:
                curr_streak_len = 0

        avg_opp = sum(g["opponent_rating"] for g in sample) / sample_count if sample_count > 0 else 0

        rating_range_str = f"{init_rating} -> {final_rating} (#{sample_count})"
        print(f"{cat.capitalize():<10} | {rating_range_str:<19} | {delta_str:<8} | {win_rate:>5.1f}%   | {str(max_streak_len) + ' games':<11} | {round(avg_opp):<12}")

    print("-" * 78)

    # 3B: Periods of Inactivity / Dormancy
    print(f"\n-- MAJOR DORMANCY PERIODS (Gaps >= {args.min_gap_days} Days) " + "-" * (78 - len(f"-- MAJOR DORMANCY PERIODS (Gaps >= {args.min_gap_days} Days) ")))
    print(f"{'Category':<10} | {'Inactive Period (UTC)':<31} | {'Gap Duration':<13} | {'Pre -> Post Rating':<18}")
    print("-" * 78)

    dormancy_events = []
    for cat in TARGET_CATEGORIES:
        games = category_games[cat]
        if len(games) < 2:
            continue

        for i in range(len(games) - 1):
            g_prev = games[i]
            g_next = games[i + 1]
            gap_seconds = g_next["end_time"] - g_prev["end_time"]

            if gap_seconds >= min_gap_seconds:
                gap_days = gap_seconds // 86400
                d_start = datetime.datetime.fromtimestamp(g_prev["end_time"], tz=datetime.timezone.utc).strftime("%Y-%m-%d")
                d_end = datetime.datetime.fromtimestamp(g_next["end_time"], tz=datetime.timezone.utc).strftime("%Y-%m-%d")
                period_str = f"{d_start} -> {d_end}"
                rating_str = f"{g_prev['player_rating']} -> {g_next['player_rating']}"
                
                dormancy_events.append({
                    "category": cat,
                    "gap_days": gap_days,
                    "gap_seconds": gap_seconds,
                    "pre_time": g_prev["end_time"],
                    "post_time": g_next["end_time"],
                    "post_game_index": i + 1,
                    "pre_rating": g_prev["player_rating"],
                    "post_rating": g_next["player_rating"],
                })
                print(f"{cat.capitalize():<10} | {period_str:<31} | {str(gap_days) + ' days':<13} | {rating_str:<18}")

    if not dormancy_events:
        print(f"No dormancy gaps >= {args.min_gap_days} days detected.")
    print("-" * 78)

    # 3C: High-Velocity Surge Windows
    print(f"\n-- HIGH-VELOCITY SURGE WINDOWS (Gain >= +{surge_pts} pts in <= {surge_games} games) " + "-" * (78 - len(f"-- HIGH-VELOCITY SURGE WINDOWS (Gain >= +{surge_pts} pts in <= {surge_games} games) ")))

    surges = []
    for cat in TARGET_CATEGORIES:
        games = category_games[cat]
        n_games = len(games)
        if n_games < 2:
            continue

        for i in range(n_games):
            start_g = games[i]
            max_j = min(i + surge_games, n_games)
            for j in range(i + 1, max_j):
                end_g = games[j]
                gain = end_g["player_rating"] - start_g["player_rating"]
                if gain >= surge_pts:
                    window_games = games[i:j + 1]
                    wins = sum(1 for g in window_games if g["outcome"] == "WIN")
                    losses = sum(1 for g in window_games if g["outcome"] == "LOSS")
                    draws = sum(1 for g in window_games if g["outcome"] == "DRAW")
                    time_diff = end_g["end_time"] - start_g["end_time"]
                    days_diff = max(time_diff / 86400.0, 0.01)
                    pace_game = gain / (len(window_games) - 1)
                    pace_day = gain / days_diff

                    # Check for reactivation alert (surge starts within 3 games after a dormancy gap)
                    reactivation_info = None
                    for d in dormancy_events:
                        if d["category"] == cat:
                            if 0 <= (i - d["post_game_index"]) <= 2:
                                reactivation_info = d
                                break

                    surges.append({
                        "category": cat,
                        "gain": gain,
                        "start_rating": start_g["player_rating"],
                        "end_rating": end_g["player_rating"],
                        "game_count": len(window_games),
                        "start_time": start_g["end_time"],
                        "end_time": end_g["end_time"],
                        "days": days_diff,
                        "wins": wins,
                        "losses": losses,
                        "draws": draws,
                        "pace_game": pace_game,
                        "pace_day": pace_day,
                        "start_game": start_g,
                        "end_game": end_g,
                        "window_games": window_games,
                        "reactivation": reactivation_info,
                    })

    # Deduplicate overlapping surge windows to keep the most significant
    surges.sort(key=lambda s: s["start_time"])
    filtered_surges = []
    for s in surges:
        if not filtered_surges:
            filtered_surges.append(s)
            continue
        prev = filtered_surges[-1]
        # Overlapping surge in same category
        if s["category"] == prev["category"] and s["start_time"] <= prev["end_time"]:
            if s["gain"] > prev["gain"]:
                filtered_surges[-1] = s
        else:
            filtered_surges.append(s)

    if not filtered_surges:
        print(f"No surge windows detected matching criteria (+{surge_pts} pts in <= {surge_games} games).")
    else:
        for idx, s in enumerate(filtered_surges, start=1):
            start_dt = datetime.datetime.fromtimestamp(s["start_time"], tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            end_dt = datetime.datetime.fromtimestamp(s["end_time"], tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            days_str = f"{s['days']:.1f} days" if s['days'] >= 1.0 else f"{round(s['days'] * 24, 1)} hours"
            win_rate = (s["wins"] / s["game_count"]) * 100.0
            opp_ratings = [g["opponent_rating"] for g in s["window_games"]]
            avg_opp = sum(opp_ratings) / len(opp_ratings) if opp_ratings else 0
            min_opp = min(opp_ratings) if opp_ratings else 0
            max_opp = max(opp_ratings) if opp_ratings else 0

            print(f"\n[Surge #{idx}] {s['category'].capitalize()} | +{s['gain']} pts ({s['start_rating']} -> {s['end_rating']}) over {s['game_count']} games | {days_str}")
            print(f"Record: {s['wins']} Wins, {s['losses']} Losses, {s['draws']} Draws ({win_rate:.1f}% Win Rate) | Pace: +{s['pace_game']:.1f} pts/game (+{s['pace_day']:.1f} pts/day)")

            if s["reactivation"]:
                gap_d = s["reactivation"]["gap_days"]
                print(f"Alert : REACTIVATION SURGE -> Immediate surge began right after {gap_d} days of dormancy!")

            print("-" * 78)
            print(f"  Start Match : {s['start_rating']} vs {s['start_game']['opponent']} ({s['start_game']['opponent_rating']}) | {start_dt} UTC")
            print(f"  Peak Match  : {s['end_rating']} vs {s['end_game']['opponent']} ({s['end_game']['opponent_rating']}) | {end_dt} UTC")
            print(f"  Opponents   : Avg {round(avg_opp)} rating (Min: {min_opp}, Max: {max_opp})")

    print("\n" + "=" * 78 + "\n")


if __name__ == "__main__":
    main()
