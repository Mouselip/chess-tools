#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright (c) 2026 Tyrin R. Price
# chesscom-rating-summary.py
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
#   ratings. Evaluates rating trajectories by categorizing dormancy gaps
#   into standard (>= 30 days) and extended (>= 90 days) tiers with return
#   session trajectory tracking (first 5 games back). Flags high-density
#   rapid rating surges bounded strictly by calendar time, minimum game volume,
#   daily velocity, and anomalous win rate floors, filtering out high-volume
#   speed-pool grinding and onboarding noise with normalized sub-day time clamping.
#   Concludes with a graduated synthesized verdict (Human, Smoke, or Fire)
#   evaluating fair-play risk without jumping straight over Smoke on isolated heaters.
#
# Version: v0.0.18

import argparse
import datetime
import json
import re
import sys
import urllib.error
import urllib.request

HEADERS = {
    "User-Agent": "chesscom-rating-summary/0.0.18 (Contact: GitHub/Mouselip)"
}

TARGET_CATEGORIES = ("bullet", "blitz", "rapid")
DEFAULT_MAX_PLY = 13
DEFAULT_MIN_STREAK = 3
DEFAULT_ONBOARDING_GAMES = 50
DEFAULT_RETURN_SAMPLE_GAMES = 5

# Inactivity defaults
DEFAULT_STANDARD_DORMANCY_DAYS = 30
DEFAULT_EXTENDED_DORMANCY_DAYS = 90

# Surge defaults
DEFAULT_SURGE_MIN_PTS = 150
DEFAULT_SURGE_MAX_DAYS = 7.0
DEFAULT_SURGE_MIN_GAMES = 15
DEFAULT_SURGE_MIN_VELOCITY = 20.0
DEFAULT_SURGE_IGNORE_ONBOARDING = 50
DEFAULT_SURGE_MIN_WIN_RATE = 75.0


def fetch_json(url):
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            sys.stderr.write(f"\n[-] Error: Resource not found at {url}\n")
        else:
            sys.stderr.write(f"\n[-] HTTP Error {e.code}: {e.reason}\n")
        sys.stderr.flush()
    except urllib.error.URLError as e:
        sys.stderr.write(f"\n[-] URL Error: {e.reason}\n")
        sys.stderr.flush()
    except Exception as e:
        sys.stderr.write(f"\n[-] Unexpected error: {e}\n")
        sys.stderr.flush()
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
        description="Scan Chess.com archives for rated games, summaries, short-ply streaks, dormancy gaps, and surges."
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
        "--dormancy-days",
        type=int,
        default=DEFAULT_STANDARD_DORMANCY_DAYS,
        help=f"Minimum days of inactivity for standard dormancy (default: {DEFAULT_STANDARD_DORMANCY_DAYS})",
    )
    parser.add_argument(
        "--extended-dormancy-days",
        type=int,
        default=DEFAULT_EXTENDED_DORMANCY_DAYS,
        help=f"Minimum days of inactivity for extended dormancy (default: {DEFAULT_EXTENDED_DORMANCY_DAYS})",
    )
    parser.add_argument(
        "--return-games",
        type=int,
        default=DEFAULT_RETURN_SAMPLE_GAMES,
        help=f"Number of games to sample immediately upon return from dormancy (default: {DEFAULT_RETURN_SAMPLE_GAMES})",
    )
    parser.add_argument(
        "--surge-min-pts",
        type=int,
        default=DEFAULT_SURGE_MIN_PTS,
        help=f"Minimum rating gain for surge detection (default: {DEFAULT_SURGE_MIN_PTS})",
    )
    parser.add_argument(
        "--surge-max-days",
        type=float,
        default=DEFAULT_SURGE_MAX_DAYS,
        help=f"Maximum calendar days span for a surge window (default: {DEFAULT_SURGE_MAX_DAYS})",
    )
    parser.add_argument(
        "--surge-min-games",
        type=int,
        default=DEFAULT_SURGE_MIN_GAMES,
        help=f"Minimum game volume required within surge window (default: {DEFAULT_SURGE_MIN_GAMES})",
    )
    parser.add_argument(
        "--surge-min-velocity",
        type=float,
        default=DEFAULT_SURGE_MIN_VELOCITY,
        help=f"Minimum rating points gained per day (default: {DEFAULT_SURGE_MIN_VELOCITY})",
    )
    parser.add_argument(
        "--surge-ignore-onboarding",
        type=int,
        default=DEFAULT_SURGE_IGNORE_ONBOARDING,
        help=f"Ignore surge windows starting within initial N onboarding games (default: {DEFAULT_SURGE_IGNORE_ONBOARDING})",
    )
    parser.add_argument(
        "--surge-min-winrate",
        type=float,
        default=DEFAULT_SURGE_MIN_WIN_RATE,
        help=f"Minimum win rate percentage required to flag an anomalous surge (default: {DEFAULT_SURGE_MIN_WIN_RATE}%%)",
    )
    args = parser.parse_args()

    username = args.username.strip()
    username_lower = username.lower()
    max_ply = args.max_ply
    min_streak = args.min_streak
    onboarding_limit = args.onboarding_games
    return_sample_games = args.return_games
    dormancy_sec = args.dormancy_days * 86400
    ext_dormancy_sec = args.extended_dormancy_days * 86400

    archives_url = f"https://api.chess.com/pub/player/{username_lower}/games/archives"
    archives_data = fetch_json(archives_url)

    if not archives_data or "archives" not in archives_data:
        sys.stderr.write(f"[-] Failed to retrieve archives for player: {username}\n")
        sys.stderr.flush()
        sys.exit(1)

    archive_urls = archives_data.get("archives", [])
    if not archive_urls:
        print(f"[-] No game archives found for {username}.", flush=True)
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
    sys.stderr.write(f"[*] Found {total_months} monthly archives for '{username}'. Fetching...\n")
    sys.stderr.flush()

    for idx, month_url in enumerate(archive_urls, start=1):
        parts = month_url.strip("/").split("/")
        year_month = f"{parts[-2]}-{parts[-1]}" if len(parts) >= 2 else f"month {idx}"

        sys.stderr.write(f"\r[*] Fetching archive [{idx}/{total_months}]: {year_month}...")
        sys.stderr.flush()

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

            # Collect game metadata for analysis
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

    sys.stderr.write(f"\r[*] Completed scanning {total_months} monthly archives.               \n\n")
    sys.stderr.flush()

    # Sort chronologically by end_time
    all_rated_games.sort(key=lambda g: g["end_time"])
    for cat in TARGET_CATEGORIES:
        category_games[cat].sort(key=lambda g: g["end_time"])

    # Section 1: Rating Summary Output
    print("=" * 88, flush=True)
    print(f" RATED RATING SUMMARY: {username}", flush=True)
    print("=" * 88, flush=True)
    print(f"{'Category':<10} | {'Games':<8} | {'Rating':<8} | {'Last Played (UTC)':<20}", flush=True)
    print("-" * 88, flush=True)

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

        print(f"{cat.capitalize():<10} | {count:<8} | {rating_str:<8} | {dt_str:<20}", flush=True)

    print("-" * 88, flush=True)

    if len(active_categories) >= 2:
        highest_cat = max(active_categories, key=active_categories.get)
        lowest_cat = min(active_categories, key=active_categories.get)
        highest_rating = active_categories[highest_cat]
        lowest_rating = active_categories[lowest_cat]
        spread = highest_rating - lowest_rating

        print(f"Highest Rated Category : {highest_cat.capitalize()} ({highest_rating})", flush=True)
        print(f"Lowest Rated Category  : {lowest_cat.capitalize()} ({lowest_rating})", flush=True)
        print(f"Rating Difference      : {spread} points", flush=True)
    elif len(active_categories) == 1:
        cat, rating = next(iter(active_categories.items()))
        print(f"Only one active rated category found: {cat.capitalize()} ({rating}). Spread not applicable.", flush=True)
    else:
        print("No rated games found in bullet, blitz, or rapid categories.", flush=True)

    print("=" * 88, flush=True)

    # Section 2: Chronological Short-Ply Streak Detection
    print(f"\n" + "=" * 88, flush=True)
    print(f" SUSPICIOUS SHORT-PLY STREAKS (0 < Ply <= {max_ply}, >= {min_streak} Consecutive Games)", flush=True)
    print("=" * 88, flush=True)

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
        print(f"No consecutive streaks of >= {min_streak} short-ply games detected.", flush=True)
    else:
        for idx, streak in enumerate(streaks, start=1):
            wins = sum(1 for g in streak if g["outcome"] == "WIN")
            losses = sum(1 for g in streak if g["outcome"] == "LOSS")
            draws = sum(1 for g in streak if g["outcome"] == "DRAW")
            start_dt = datetime.datetime.fromtimestamp(streak[0]["end_time"], tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            end_dt = datetime.datetime.fromtimestamp(streak[-1]["end_time"], tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

            print(f"\n[Streak #{idx}] Length: {len(streak)} games (+{wins} -{losses} ={draws}) | {start_dt} to {end_dt} UTC", flush=True)
            print("-" * 88, flush=True)
            for g in streak:
                dt_str = datetime.datetime.fromtimestamp(g["end_time"], tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                matchup_info = f"vs {g['opponent']} ({g['player_rating']}/{g['opponent_rating']})"
                print(f"  [{g['outcome']:<4}] {g['time_class'].capitalize():<6} | {g['ply']:>2} ply | {g['event_type']:<7} | {matchup_info:<30} | {dt_str} UTC | {g['url']}", flush=True)

    # Section 3: Rating Trajectory, Dormancy & Surge Analysis
    print(f"\n" + "=" * 88, flush=True)
    print(" RATING TRAJECTORY, DORMANCY & SURGE ANALYSIS", flush=True)
    print("=" * 88, flush=True)

    # 3A: Initial Account Onboarding
    print(f"\n-- INITIAL ACCOUNT ONBOARDING (First {onboarding_limit} Games per Pool) " + "-" * max(0, (88 - len(f"-- INITIAL ACCOUNT ONBOARDING (First {onboarding_limit} Games per Pool) "))), flush=True)
    print(f"{'Category':<10} | {'Initial -> End':<19} | {'Delta':<8} | {'Win Rate':<9} | {'Max Streak':<11} | {'Avg Opponent':<12}", flush=True)
    print("-" * 88, flush=True)

    for cat in TARGET_CATEGORIES:
        games = category_games[cat]
        if not games:
            print(f"{cat.capitalize():<10} | {'No games played':<19} | {'N/A':<8} | {'N/A':<9} | {'N/A':<11} | {'N/A':<12}", flush=True)
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
        print(f"{cat.capitalize():<10} | {rating_range_str:<19} | {delta_str:<8} | {win_rate:>5.1f}%   | {str(max_streak_len) + ' games':<11} | {round(avg_opp):<12}", flush=True)

    print("-" * 88, flush=True)

    # 3B: Inactivity & Dormancy Gaps
    print(f"\n-- INACTIVITY & DORMANCY GAPS (Standard >= {args.dormancy_days}d, Extended >= {args.extended_dormancy_days}d) " + "-" * max(0, (88 - len(f"-- INACTIVITY & DORMANCY GAPS (Standard >= {args.dormancy_days}d, Extended >= {args.extended_dormancy_days}d) "))), flush=True)
    print(f"{'Category':<10} | {'Inactive Period (UTC)':<25} | {'Duration':<11} | {'Tier':<10} | {f'Return Trajectory (First {return_sample_games} Games)':<30}", flush=True)
    print("-" * 88, flush=True)

    dormancy_events = []
    for cat in TARGET_CATEGORIES:
        games = category_games[cat]
        if len(games) < 2:
            continue

        for i in range(len(games) - 1):
            g_prev = games[i]
            g_next = games[i + 1]
            gap_seconds = g_next["end_time"] - g_prev["end_time"]

            if gap_seconds >= dormancy_sec:
                gap_days = gap_seconds // 86400
                tier = "Extended" if gap_seconds >= ext_dormancy_sec else "Standard"
                d_start = datetime.datetime.fromtimestamp(g_prev["end_time"], tz=datetime.timezone.utc).strftime("%Y-%m-%d")
                d_end = datetime.datetime.fromtimestamp(g_next["end_time"], tz=datetime.timezone.utc).strftime("%Y-%m-%d")
                period_str = f"{d_start} -> {d_end}"

                # Sample the return cluster (up to return_sample_games)
                return_sample = games[i + 1: i + 1 + return_sample_games]
                sample_len = len(return_sample)
                pre_rating = g_prev["player_rating"]
                post_sample_rating = return_sample[-1]["player_rating"]
                sample_delta = post_sample_rating - pre_rating

                s_wins = sum(1 for g in return_sample if g["outcome"] == "WIN")
                s_losses = sum(1 for g in return_sample if g["outcome"] == "LOSS")
                s_draws = sum(1 for g in return_sample if g["outcome"] == "DRAW")

                delta_str = f"{sample_delta:+d}"
                record_str = f"{s_wins}-{s_losses}-{s_draws}"
                if sample_len == 1:
                    trajectory_str = f"{pre_rating} -> {post_sample_rating} ({delta_str} pts, 1 gm: {record_str})"
                else:
                    trajectory_str = f"{pre_rating} -> {post_sample_rating} ({delta_str} pts, {record_str})"

                dormancy_events.append({
                    "category": cat,
                    "gap_days": gap_days,
                    "gap_seconds": gap_seconds,
                    "tier": tier,
                    "pre_time": g_prev["end_time"],
                    "post_time": g_next["end_time"],
                    "post_game_index": i + 1,
                    "pre_rating": pre_rating,
                    "post_rating": g_next["player_rating"],
                })
                print(f"{cat.capitalize():<10} | {period_str:<25} | {str(gap_days) + ' days':<11} | {tier:<10} | {trajectory_str:<30}", flush=True)

    if not dormancy_events:
        print(f"No inactivity gaps >= {args.dormancy_days} days detected.", flush=True)
    print("-" * 88, flush=True)

    # 3C: High-Velocity Surge Windows
    surge_criteria_str = f"-- HIGH-VELOCITY SURGES (Gain >= +{args.surge_min_pts} pts, >= {args.surge_min_games} games, <= {args.surge_max_days}d, >= {args.surge_min_velocity} pts/d, >= {args.surge_min_winrate}% WR) "
    print(f"\n{surge_criteria_str}" + "-" * max(0, 88 - len(surge_criteria_str)), flush=True)

    surges = []
    for cat in TARGET_CATEGORIES:
        games = category_games[cat]
        n_games = len(games)
        if n_games < args.surge_min_games:
            continue

        # Ignore candidate surges starting within initial onboarding window
        start_bound = max(0, args.surge_ignore_onboarding)
        for i in range(start_bound, n_games):
            start_g = games[i]
            for j in range(i + args.surge_min_games - 1, n_games):
                end_g = games[j]
                time_diff = end_g["end_time"] - start_g["end_time"]
                actual_days = max(time_diff / 86400.0, 0.001)

                if actual_days > args.surge_max_days:
                    break

                gain = end_g["player_rating"] - start_g["player_rating"]
                if gain >= args.surge_min_pts:
                    # Clamp velocity divisor to at least 1.0 day to avoid micro-session extrapolation spikes
                    effective_days = max(actual_days, 1.0)
                    velocity = gain / effective_days

                    if velocity >= args.surge_min_velocity:
                        window_games = games[i:j + 1]
                        wins = sum(1 for g in window_games if g["outcome"] == "WIN")
                        losses = sum(1 for g in window_games if g["outcome"] == "LOSS")
                        draws = sum(1 for g in window_games if g["outcome"] == "DRAW")
                        win_rate = (wins / len(window_games)) * 100.0

                        # Filter out organic grinding sessions with balanced win rates
                        if win_rate < args.surge_min_winrate:
                            continue

                        pace_game = gain / (len(window_games) - 1)

                        # Check if surge began immediately post-dormancy
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
                            "days": actual_days,
                            "wins": wins,
                            "losses": losses,
                            "draws": draws,
                            "win_rate": win_rate,
                            "pace_game": pace_game,
                            "pace_day": velocity,
                            "start_game": start_g,
                            "end_game": end_g,
                            "window_games": window_games,
                            "reactivation": reactivation_info,
                        })

    # Deduplicate overlapping surge windows by selecting the maximum gain window
    surges.sort(key=lambda s: s["start_time"])
    filtered_surges = []
    for s in surges:
        if not filtered_surges:
            filtered_surges.append(s)
            continue
        prev = filtered_surges[-1]
        if s["category"] == prev["category"] and s["start_time"] <= prev["end_time"]:
            if s["gain"] > prev["gain"]:
                filtered_surges[-1] = s
        else:
            filtered_surges.append(s)

    if not filtered_surges:
        print("No high-velocity surge windows detected.", flush=True)
    else:
        for idx, s in enumerate(filtered_surges, start=1):
            start_dt = datetime.datetime.fromtimestamp(s["start_time"], tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            end_dt = datetime.datetime.fromtimestamp(s["end_time"], tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            days_str = f"{s['days']:.1f} days" if s['days'] >= 1.0 else f"{round(s['days'] * 24, 1)} hours"
            opp_ratings = [g["opponent_rating"] for g in s["window_games"]]
            avg_opp = sum(opp_ratings) / len(opp_ratings) if opp_ratings else 0
            min_opp = min(opp_ratings) if opp_ratings else 0
            max_opp = max(opp_ratings) if opp_ratings else 0

            print(f"\n[Surge #{idx}] {s['category'].capitalize()} | +{s['gain']} pts ({s['start_rating']} -> {s['end_rating']}) over {s['game_count']} games | {days_str}", flush=True)
            print(f"Record: {s['wins']} Wins, {s['losses']} Losses, {s['draws']} Draws ({s['win_rate']:.1f}% Win Rate) | Pace: +{s['pace_game']:.1f} pts/game (+{s['pace_day']:.1f} pts/day)", flush=True)

            if s["reactivation"]:
                gap_d = s["reactivation"]["gap_days"]
                tier_str = s["reactivation"]["tier"]
                print(f"Alert : REACTIVATION SURGE -> Surge began immediately after {gap_d} days of {tier_str.lower()} dormancy!", flush=True)

            print("-" * 88, flush=True)
            print(f"  Start Match : {s['start_rating']} vs {s['start_game']['opponent']} ({s['start_game']['opponent_rating']}) | {start_dt} UTC", flush=True)
            print(f"  Peak Match  : {s['end_rating']} vs {s['end_game']['opponent']} ({s['end_game']['opponent_rating']}) | {end_dt} UTC", flush=True)
            print(f"  Opponents   : Avg {round(avg_opp)} rating (Min: {min_opp}, Max: {max_opp})", flush=True)

    # Section 4: Forensic Synthesis & Verdict Determination
    reasons = []
    signals_smoke = []
    signals_fire = []

    # Check for Fire-level conditions (sustained engine-level thresholds)
    for s in filtered_surges:
        if s["reactivation"] and s["pace_day"] >= 25.0 and s["win_rate"] >= 80.0:
            signals_fire.append(f"Reactivation surge (+{s['gain']} pts, {s['win_rate']:.1f}% win rate, +{s['pace_day']:.1f} pts/day post-dormancy)")
        elif s["gain"] >= 200 and s["days"] <= 7.0 and s["pace_day"] >= 35.0 and s["win_rate"] >= 85.0:
            signals_fire.append(f"High-velocity surge (+{s['gain']} pts in {s['days']:.1f}d at +{s['pace_day']:.1f} pts/day, {s['win_rate']:.1f}% win rate)")

    total_short_games = sum(len(st) for st in streaks)
    max_streak_len = max([len(st) for st in streaks]) if streaks else 0
    if max_streak_len >= 5 or total_short_games >= 12:
        signals_fire.append(f"Severe short-ply streaks (Max: {max_streak_len} consecutive games, Total: {total_short_games})")

    # Check for Smoke-level conditions (isolated heaters or borderline patterns)
    for s in filtered_surges:
        if s not in signals_fire:
            if 75.0 <= s["win_rate"] < 85.0 or s["pace_day"] >= 20.0:
                signals_smoke.append(f"Elevated surge session (+{s['gain']} pts over {s['game_count']} games, {s['win_rate']:.1f}% win rate, +{s['pace_day']:.1f} pts/day)")

    if len(streaks) >= 2 and not signals_fire:
        signals_smoke.append(f"Multiple short-ply streaks detected ({len(streaks)} streaks)")

    if signals_fire:
        verdict = "Fire"
        reasons = signals_fire
    elif signals_smoke:
        verdict = "Smoke"
        reasons = signals_smoke
    else:
        verdict = "Human"
        reasons = ["Rating progression and volume profiles align with standard organic play."]

    print("\n" + "=" * 88, flush=True)
    print(" FINAL FORENSIC SYNTHESIS", flush=True)
    print("=" * 88, flush=True)
    print(f" Verdict         : {verdict}", flush=True)
    print(f" Primary Signals :", flush=True)
    streak_summary = f"{len(streaks)} streaks found (Max: {max_streak_len} games, Total: {total_short_games} games)" if streaks else "None detected"
    print(f"   - [Short-Ply Streaks]   : {streak_summary}", flush=True)

    ext_gaps = [d for d in dormancy_events if d["tier"] == "Extended"]
    max_gap_days = max([d["gap_days"] for d in dormancy_events]) if dormancy_events else 0
    gap_summary = f"{len(dormancy_events)} gaps found ({len(ext_gaps)} extended, Max: {max_gap_days} days)" if dormancy_events else "None detected"
    print(f"   - [Dormancy Gaps]       : {gap_summary}", flush=True)

    print(f"   - [Surge Profile]       : {len(filtered_surges)} high-velocity surges detected", flush=True)
    print(f" Evaluation Details:", flush=True)
    for r in reasons:
        print(f"   * {r}", flush=True)
    print("=" * 88 + "\n", flush=True)


if __name__ == "__main__":
    main()
