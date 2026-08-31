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
#   for rated games across bullet, blitz, rapid, and daily categories.
#   Retrieves player profile metadata to report account status, title,
#   and script version at the top of the report.
#   Reports game counts, W-D-L records, overall score percentage, average
#   accuracy with analysis coverage counts, latest rating, last played date
#   per category, abandoned games with material deficit telemetry, and rating
#   spread. Detects suspicious streaks of consecutive short-ply games
#   (0 < ply <= 13, live categories only) and sustained high-accuracy winning
#   streaks (accuracy >= 96.0%, ply >= 45, 100% wins, strictly consecutive and
#   time-bounded within <= 48h, live categories only). Evaluates rating trajectories,
#   dormancy gaps, rating landslides / sandbagging / tilt spirals, and high-velocity
#   surges with density-gated accuracy corroboration and cross-pool rating ceiling
#   congruence suppression. Applies a 2-year recency lookback window for active
#   verdicts (Smoke/Fire) while segregating older triggers into historical context.
#   Synthesizes graduated verdicts (Human, Smoke, or Fire) with explicit category
#   names, dates in evaluation details, clear trigger attribution, and pool breakdowns.
#   Includes robust HTTP 429/transient retry backoff and an optional User-Agent CLI flag.
#
# Version: v2.0.0

import argparse
import datetime
import json
import re
import sys
import time
import urllib.error
import urllib.request

VERSION = "v2.0.0"
DEFAULT_REPO_URL = "https://github.com/Mouselip/chess-tools"

# Pool and category filters
TARGET_CATEGORIES = ("bullet", "blitz", "rapid", "daily")
LIVE_CATEGORIES = ("bullet", "blitz", "rapid")

# Time decay lookback window for active verdicts (2 years)
RECENCY_LOOKBACK_SECONDS = 2 * 365 * 86400

# Short-ply streak detection constants (Live pools only)
MAX_PLY = 13
MIN_SHORT_PLY_STREAK = 3

# Account profile baseline constants
ONBOARDING_GAMES = 50
RETURN_SAMPLE_GAMES = 5

# Inactivity and dormancy constants
STANDARD_DORMANCY_DAYS = 30
EXTENDED_DORMANCY_DAYS = 90

# Rating surge detection constants
SURGE_MIN_PTS = 150
SURGE_MAX_DAYS = 7.0
SURGE_MIN_GAMES = 15
SURGE_MIN_VELOCITY = 20.0
SURGE_IGNORE_ONBOARDING = 50
SURGE_MIN_WIN_RATE = 75.0
CONGRUENCE_TOLERANCE_PTS = 50

# Rating landslide / sandbagging detection thresholds per pool
LANDSLIDE_THRESHOLDS = {
    "bullet": {"min_pts": 200, "min_games": 25, "max_days": 7.0, "min_loss_rate": 75.0},
    "blitz":  {"min_pts": 150, "min_games": 15, "max_days": 7.0, "min_loss_rate": 75.0},
    "rapid":  {"min_pts": 120, "min_games": 10, "max_days": 7.0, "min_loss_rate": 75.0},
    "daily":  {"min_pts": 120, "min_games": 10, "max_days": 14.0, "min_loss_rate": 75.0},
}
FAST_DUMP_MIN_PTS = 100
FAST_DUMP_MIN_GAMES = 8
FAST_DUMP_SHORT_PLY_RATIO = 0.50
FAST_DUMP_MIN_LOSS_RATE = 85.0

# High-accuracy winning streak constants
MIN_ACC_STREAK = 4
MIN_ACC_PLY = 45
ACC_STREAK_THRESHOLD = 96.0
ACC_STREAK_MAX_HOURS = 48.0
MIN_ANALYZED_THRESHOLD = 10

# Material piece values for FEN parsing
PIECE_VALUES = {
    'p': 1, 'n': 3, 'b': 3, 'r': 5, 'q': 9,
    'P': 1, 'N': 3, 'B': 3, 'R': 5, 'Q': 9
}


def fetch_json(url, user_agent, max_retries=3):
    headers = {"User-Agent": user_agent}
    req = urllib.request.Request(url, headers=headers)
    for attempt in range(1, max_retries + 1):
        try:
            with urllib.request.urlopen(req) as resp:
                if resp.status == 200:
                    return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                sys.stderr.write(f"\n[-] Error: Resource not found at {url}\n")
                sys.stderr.flush()
                return None
            elif e.code in (429, 500, 502, 503, 504):
                sleep_seconds = attempt * 2
                sys.stderr.write(f"\n[!] HTTP {e.code} ({e.reason}) at {url}. Backing off {sleep_seconds}s (retry {attempt}/{max_retries})...\n")
                sys.stderr.flush()
                time.sleep(sleep_seconds)
            else:
                sys.stderr.write(f"\n[-] HTTP Error {e.code}: {e.reason}\n")
                sys.stderr.flush()
                return None
        except urllib.error.URLError as e:
            sleep_seconds = attempt * 2
            sys.stderr.write(f"\n[!] Network error: {e.reason}. Retrying in {sleep_seconds}s ({attempt}/{max_retries})...\n")
            sys.stderr.flush()
            time.sleep(sleep_seconds)
        except Exception as e:
            sys.stderr.write(f"\n[-] Unexpected error: {e}\n")
            sys.stderr.flush()
            return None
    sys.stderr.write(f"[-] Max retries exceeded for {url}\n")
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


def count_material_deficit(fen, is_white):
    """Returns True if player is down 3 or more points of material on the board."""
    if not fen:
        return False
    board_part = fen.split()[0]
    white_score = sum(PIECE_VALUES[c] for c in board_part if c.isupper() and c in PIECE_VALUES)
    black_score = sum(PIECE_VALUES[c] for c in board_part if c.islower() and c in PIECE_VALUES)
    delta = (white_score - black_score) if is_white else (black_score - white_score)
    return delta <= -3


def main():
    parser = argparse.ArgumentParser(
        description="Scan Chess.com archives for rated games, summaries, short-ply streaks, accuracy winning streaks, dormancy gaps, landslides/sandbagging, and surges."
    )
    parser.add_argument("username", help="Chess.com target username")
    parser.add_argument(
        "--user-agent",
        type=str,
        default="",
        help="Custom User-Agent header string (default: chesscom-rating-summary/<version> (<repo_url>))",
    )
    args = parser.parse_args()

    username = args.username.strip()
    username_lower = username.lower()

    if args.user_agent.strip():
        user_agent = args.user_agent.strip()
    else:
        user_agent = f"chesscom-rating-summary/{VERSION.lstrip('v')} ({DEFAULT_REPO_URL})"

    dormancy_sec = STANDARD_DORMANCY_DAYS * 86400
    ext_dormancy_sec = EXTENDED_DORMANCY_DAYS * 86400

    profile_url = f"https://api.chess.com/pub/player/{username_lower}"
    profile_data = fetch_json(profile_url, user_agent=user_agent)
    if profile_data:
        account_status = profile_data.get("status", "unknown")
        player_title = profile_data.get("title", "")
    else:
        account_status = "unknown"
        player_title = ""

    archives_url = f"https://api.chess.com/pub/player/{username_lower}/games/archives"
    archives_data = fetch_json(archives_url, user_agent=user_agent)

    if not archives_data or "archives" not in archives_data:
        sys.stderr.write(f"\n[-] Failed to retrieve archives for player: {username}\n")
        sys.stderr.flush()
        sys.exit(1)

    archive_urls = archives_data.get("archives", [])
    if not archive_urls:
        print(f"[-] No game archives found for {username}.", flush=True)
        sys.exit(0)

    stats = {
        cat: {
            "count": 0,
            "wins": 0,
            "draws": 0,
            "losses": 0,
            "accuracy_sum": 0.0,
            "accuracy_count": 0,
            "latest_rating": None,
            "last_played_ts": 0,
            "abandoned_total": 0,
            "abandoned_deficit": 0,
        }
        for cat in TARGET_CATEGORIES
    }

    all_rated_games = []
    live_rated_games = []
    category_games = {cat: [] for cat in TARGET_CATEGORIES}

    total_months = len(archive_urls)
    sys.stderr.write(f"[*] Found {total_months} monthly archives for '{username}'. Fetching...\n")
    sys.stderr.flush()

    for idx, month_url in enumerate(archive_urls, start=1):
        parts = month_url.strip("/").split("/")
        year_month = f"{parts[-2]}-{parts[-1]}" if len(parts) >= 2 else f"month {idx}"

        sys.stderr.write(f"\r[*] Fetching archive [{idx}/{total_months}]: {year_month}...")
        sys.stderr.flush()

        month_data = fetch_json(month_url, user_agent=user_agent)
        if not month_data:
            continue

        for game in month_data.get("games", []):
            rules = game.get("rules", "")
            if rules != "chess":
                continue

            if not game.get("rated", False):
                continue

            time_class = game.get("time_class", "").lower()
            white = game.get("white", {})
            black = game.get("black", {})

            white_user = white.get("username", "")
            black_user = black.get("username", "")

            white_lower = white_user.lower()
            black_lower = black_user.lower()

            accuracies = game.get("accuracies")
            player_acc = None

            if white_lower == username_lower:
                player_color = "white"
                player_rating = white.get("rating")
                player_result = white.get("result", "")
                opponent_name = black_user
                opponent_rating = black.get("rating")
                if isinstance(accuracies, dict):
                    player_acc = accuracies.get("white")
            elif black_lower == username_lower:
                player_color = "black"
                player_rating = black.get("rating")
                player_result = black.get("result", "")
                opponent_name = white_user
                opponent_rating = white.get("rating")
                if isinstance(accuracies, dict):
                    player_acc = accuracies.get("black")
            else:
                continue

            end_time = game.get("end_time", 0)

            if player_result == "win":
                outcome = "WIN"
            elif player_result in ("agreed", "repetition", "stalemate", "timevsinsufficient", "insufficient"):
                outcome = "DRAW"
            else:
                outcome = "LOSS"

            is_w = (player_color == "white")
            is_abandoned_loss = (outcome == "LOSS" and player_result == "abandoned")
            is_deficit_abandon = is_abandoned_loss and count_material_deficit(game.get("fen", ""), is_w)

            if time_class in stats:
                stats[time_class]["count"] += 1
                if outcome == "WIN":
                    stats[time_class]["wins"] += 1
                elif outcome == "DRAW":
                    stats[time_class]["draws"] += 1
                else:
                    stats[time_class]["losses"] += 1
                    if is_abandoned_loss:
                        stats[time_class]["abandoned_total"] += 1
                        if is_deficit_abandon:
                            stats[time_class]["abandoned_deficit"] += 1

                if player_acc is not None:
                    stats[time_class]["accuracy_sum"] += float(player_acc)
                    stats[time_class]["accuracy_count"] += 1

                if end_time >= stats[time_class]["last_played_ts"]:
                    stats[time_class]["last_played_ts"] = end_time
                    stats[time_class]["latest_rating"] = player_rating

            pgn = game.get("pgn", "")
            ply_count = count_ply_from_pgn(pgn)
            event_type = extract_event_type(pgn)
            game_url = game.get("url", "")

            game_obj = {
                "end_time": end_time,
                "time_class": time_class,
                "ply": ply_count,
                "outcome": outcome,
                "color": player_color,
                "accuracy": float(player_acc) if player_acc is not None else None,
                "event_type": event_type,
                "opponent": opponent_name,
                "opponent_rating": opponent_rating if opponent_rating is not None else 0,
                "player_rating": player_rating if player_rating is not None else 0,
                "url": game_url,
                "is_abandoned": is_abandoned_loss,
                "is_deficit_abandon": is_deficit_abandon,
            }

            all_rated_games.append(game_obj)
            if time_class in LIVE_CATEGORIES:
                live_rated_games.append(game_obj)
            if time_class in category_games:
                category_games[time_class].append(game_obj)

    sys.stderr.write(f"\r[*] Completed scanning {total_months} monthly archives.               \n\n")
    sys.stderr.flush()

    all_rated_games.sort(key=lambda g: g["end_time"])
    live_rated_games.sort(key=lambda g: g["end_time"])
    for cat in TARGET_CATEGORIES:
        category_games[cat].sort(key=lambda g: g["end_time"])

    # Section 1: Rating Summary Output
    print("=" * 102, flush=True)
    title_str = f" [{player_title}]" if player_title else ""
    header_left = f" RATED RATING SUMMARY: {username}{title_str} | Status: {account_status}"
    header_right = f"{VERSION} "
    header_spaces = max(1, 102 - len(header_left) - len(header_right))
    print(f"{header_left}{' ' * header_spaces}{header_right}", flush=True)
    print("=" * 102, flush=True)
    print(f"{'Category':<10} | {'Games':<8} | {'W-D-L':<15} | {'Score %':<9} | {'Avg Acc (Analyzed)':<22} | {'Rating':<8} | {'Last Played (UTC)':<20}", flush=True)
    print("-" * 102, flush=True)

    active_categories = {}
    total_acc_count = 0
    total_games_all = len(all_rated_games)

    for cat in TARGET_CATEGORIES:
        data = stats[cat]
        count = data["count"]
        if count == 0:
            continue

        w = data["wins"]
        d = data["draws"]
        l = data["losses"]
        rating = data["latest_rating"]
        ts = data["last_played_ts"]
        acc_cnt = data["accuracy_count"]
        acc_sum = data["accuracy_sum"]
        total_acc_count += acc_cnt

        dt_str = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S"
        ) if ts else "N/A"
        rating_str = str(rating) if rating is not None else "N/A"
        wdl_str = f"{w}-{d}-{l}"
        score_pct = ((w + 0.5 * d) / count) * 100.0
        score_str = f"{score_pct:>5.1f}%"
        if rating is not None:
            active_categories[cat] = rating

        if acc_cnt >= MIN_ANALYZED_THRESHOLD:
            avg_acc = acc_sum / acc_cnt
            acc_display = f"{avg_acc:>5.1f}% ({acc_cnt:>4}/{count:<5})"
        elif acc_cnt > 0:
            avg_acc = acc_sum / acc_cnt
            acc_display = f"{avg_acc:>5.1f}%*({acc_cnt:>2}/{count:<5})"
        else:
            acc_display = f"  N/A  (   0/{count:<5})"

        print(f"{cat.capitalize():<10} | {count:<8} | {wdl_str:<15} | {score_str:<9} | {acc_display:<22} | {rating_str:<8} | {dt_str:<20}", flush=True)

    print("-" * 102, flush=True)

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
        print("No rated games found in bullet, blitz, rapid, or daily categories.", flush=True)

    # Abandoned Telemetry (Sparse: only categories with abandoned games)
    has_abandoned = any(stats[cat]["abandoned_total"] > 0 for cat in TARGET_CATEGORIES)
    if has_abandoned:
        print("\n-- ABANDONED GAMES TELEMETRY " + "-" * (102 - len("-- ABANDONED GAMES TELEMETRY ")), flush=True)
        for cat in TARGET_CATEGORIES:
            ab_total = stats[cat]["abandoned_total"]
            ab_def = stats[cat]["abandoned_deficit"]
            if ab_total > 0:
                print(f"{cat.capitalize():<6} Abandoned Losses : {ab_total} total | {ab_def} with Material Deficit (<= -3 pts)", flush=True)

    print("=" * 102, flush=True)

    # Section 2: Chronological Short-Ply Streak Detection (Live Categories Only)
    print(f"\n" + "=" * 102, flush=True)
    print(f" SUSPICIOUS SHORT-PLY STREAKS (0 < Ply <= {MAX_PLY}, >= {MIN_SHORT_PLY_STREAK} Consecutive Live Games)", flush=True)
    print("=" * 102, flush=True)

    streaks = []
    current_streak = []

    for game in live_rated_games:
        if 0 < game["ply"] <= MAX_PLY:
            current_streak.append(game)
        else:
            if len(current_streak) >= MIN_SHORT_PLY_STREAK:
                streaks.append(list(current_streak))
            current_streak = []

    if len(current_streak) >= MIN_SHORT_PLY_STREAK:
        streaks.append(list(current_streak))

    if not streaks:
        print(f"No consecutive streaks of >= {MIN_SHORT_PLY_STREAK} short-ply live games detected.", flush=True)
    else:
        for idx, streak in enumerate(streaks, start=1):
            wins = sum(1 for g in streak if g["outcome"] == "WIN")
            losses = sum(1 for g in streak if g["outcome"] == "LOSS")
            draws = sum(1 for g in streak if g["outcome"] == "DRAW")
            start_dt = datetime.datetime.fromtimestamp(streak[0]["end_time"], tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            end_dt = datetime.datetime.fromtimestamp(streak[-1]["end_time"], tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

            print(f"\n[Streak #{idx}] Length: {len(streak)} games (+{wins} -{losses} ={draws}) | {start_dt} to {end_dt} UTC", flush=True)
            print("-" * 102, flush=True)
            for g in streak:
                dt_str = datetime.datetime.fromtimestamp(g["end_time"], tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                matchup_info = f"vs {g['opponent']} ({g['player_rating']}/{g['opponent_rating']})"
                print(f"  [{g['outcome']:<4}] {g['time_class'].capitalize():<6} | {g['ply']:>2} ply | {g['event_type']:<7} | {matchup_info:<30} | {dt_str} UTC | {g['url']}", flush=True)

    # Section 3: High-Accuracy Winning Streak Detection (Live Pools Only, 100% Wins, Time-Bounded)
    print(f"\n" + "=" * 102, flush=True)
    print(f" HIGH-ACCURACY WINNING STREAKS (>= {ACC_STREAK_THRESHOLD:.1f}% Acc, >= {MIN_ACC_PLY} Ply, 100% Wins, <= {ACC_STREAK_MAX_HOURS:.0f}h, >= {MIN_ACC_STREAK} Games)", flush=True)
    print("=" * 102, flush=True)

    max_gap_seconds = ACC_STREAK_MAX_HOURS * 3600.0
    acc_streaks = []

    for cat in LIVE_CATEGORIES:
        games = category_games[cat]
        current_acc_streak = []
        for g in games:
            is_candidate = (
                g["outcome"] == "WIN"
                and g["accuracy"] is not None
                and g["accuracy"] >= ACC_STREAK_THRESHOLD
                and g["ply"] >= MIN_ACC_PLY
            )

            if is_candidate:
                if current_acc_streak:
                    elapsed = g["end_time"] - current_acc_streak[0]["end_time"]
                    if elapsed <= max_gap_seconds:
                        current_acc_streak.append(g)
                    else:
                        if len(current_acc_streak) >= MIN_ACC_STREAK:
                            acc_streaks.append(list(current_acc_streak))
                        current_acc_streak = [g]
                else:
                    current_acc_streak.append(g)
            else:
                if len(current_acc_streak) >= MIN_ACC_STREAK:
                    acc_streaks.append(list(current_acc_streak))
                current_acc_streak = []

        if len(current_acc_streak) >= MIN_ACC_STREAK:
            acc_streaks.append(list(current_acc_streak))

    if not acc_streaks:
        print(f"No consecutive winning streaks of >= {MIN_ACC_STREAK} high-accuracy live games detected.", flush=True)
    else:
        for idx, streak in enumerate(acc_streaks, start=1):
            avg_acc = sum(g["accuracy"] for g in streak) / len(streak)
            peak_acc = max(g["accuracy"] for g in streak)
            cat_name = streak[0]["time_class"].capitalize()
            start_dt = datetime.datetime.fromtimestamp(streak[0]["end_time"], tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            end_dt = datetime.datetime.fromtimestamp(streak[-1]["end_time"], tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            duration_hrs = (streak[-1]["end_time"] - streak[0]["end_time"]) / 3600.0

            print(f"\n[Acc Streak #{idx}] {cat_name} | {len(streak)} Wins (Avg Acc: {avg_acc:.1f}%, Peak: {peak_acc:.1f}%) | {duration_hrs:.1f} hours | {start_dt} to {end_dt} UTC", flush=True)
            print("-" * 102, flush=True)
            for g in streak:
                dt_str = datetime.datetime.fromtimestamp(g["end_time"], tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                matchup_info = f"vs {g['opponent']} ({g['player_rating']}/{g['opponent_rating']})"
                print(f"  [{g['outcome']:<4}] {g['time_class'].capitalize():<6} | {g['ply']:>2} ply | {g['accuracy']:>5.1f}% acc | {matchup_info:<30} | {dt_str} UTC | {g['url']}", flush=True)

    # Section 4: Rating Trajectory, Dormancy & Surge Analysis
    print(f"\n" + "=" * 102, flush=True)
    print(" RATING TRAJECTORY, DORMANCY & SURGE ANALYSIS", flush=True)
    print("=" * 102, flush=True)

    # 4A: Initial Account Onboarding (Sparse: only categories with games)
    print(f"\n-- INITIAL ACCOUNT ONBOARDING (First {ONBOARDING_GAMES} Games per Pool) " + "-" * max(0, (102 - len(f"-- INITIAL ACCOUNT ONBOARDING (First {ONBOARDING_GAMES} Games per Pool) "))), flush=True)
    print(f"{'Category':<10} | {'Initial -> End':<19} | {'Delta':<8} | {'Win Rate':<9} | {'Max Win Streak':<15} | {'Avg Opponent':<12}", flush=True)
    print("-" * 102, flush=True)

    for cat in TARGET_CATEGORIES:
        games = category_games[cat]
        if not games:
            continue

        sample = games[:ONBOARDING_GAMES]
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
        print(f"{cat.capitalize():<10} | {rating_range_str:<19} | {delta_str:<8} | {win_rate:>5.1f}%   | {str(max_streak_len) + ' games':<15} | {round(avg_opp):<12}", flush=True)

    print("-" * 102, flush=True)

    # 4B: Inactivity & Dormancy Gaps
    print(f"\n-- INACTIVITY & DORMANCY GAPS (Standard >= {STANDARD_DORMANCY_DAYS}d, Extended >= {EXTENDED_DORMANCY_DAYS}d) " + "-" * max(0, (102 - len(f"-- INACTIVITY & DORMANCY GAPS (Standard >= {STANDARD_DORMANCY_DAYS}d, Extended >= {EXTENDED_DORMANCY_DAYS}d) "))), flush=True)
    print(f"{'Category':<10} | {'Inactive Period (UTC)':<25} | {'Duration':<11} | {'Tier':<10} | {f'Return Trajectory (First {RETURN_SAMPLE_GAMES} Games)':<30}", flush=True)
    print("-" * 102, flush=True)

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

                return_sample = games[i + 1: i + 1 + RETURN_SAMPLE_GAMES]
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
        print(f"No inactivity gaps >= {STANDARD_DORMANCY_DAYS} days detected.", flush=True)
    print("-" * 102, flush=True)

    # 4C: High-Velocity Surge Windows
    surge_criteria_str = f"-- HIGH-VELOCITY SURGES (Gain >= +{SURGE_MIN_PTS} pts, >= {SURGE_MIN_GAMES} games, <= {SURGE_MAX_DAYS}d, >= {SURGE_MIN_VELOCITY} pts/d, >= {SURGE_MIN_WIN_RATE}% WR) "
    print(f"\n{surge_criteria_str}" + "-" * max(0, 102 - len(surge_criteria_str)), flush=True)

    surges = []
    max_surge_seconds = SURGE_MAX_DAYS * 86400.0

    for cat in TARGET_CATEGORIES:
        games = category_games[cat]
        n_games = len(games)
        if n_games < SURGE_MIN_GAMES:
            continue

        start_bound = max(0, SURGE_IGNORE_ONBOARDING)
        cat_dormancy = [d for d in dormancy_events if d["category"] == cat]

        for i in range(start_bound, n_games - SURGE_MIN_GAMES + 1):
            start_g = games[i]
            start_ts = start_g["end_time"]

            wins = 0
            losses = 0
            draws = 0
            analyzed_in_window = []

            # Pre-accumulate up to SURGE_MIN_GAMES - 1
            for k in range(i, i + SURGE_MIN_GAMES - 1):
                g_k = games[k]
                out = g_k["outcome"]
                if out == "WIN":
                    wins += 1
                elif out == "LOSS":
                    losses += 1
                elif out == "DRAW":
                    draws += 1
                if g_k["accuracy"] is not None:
                    analyzed_in_window.append(g_k["accuracy"])

            for j in range(i + SURGE_MIN_GAMES - 1, n_games):
                end_g = games[j]
                time_diff = end_g["end_time"] - start_ts
                if time_diff > max_surge_seconds:
                    break

                out = end_g["outcome"]
                if out == "WIN":
                    wins += 1
                elif out == "LOSS":
                    losses += 1
                elif out == "DRAW":
                    draws += 1
                if end_g["accuracy"] is not None:
                    analyzed_in_window.append(end_g["accuracy"])

                gain = end_g["player_rating"] - start_g["player_rating"]
                if gain < SURGE_MIN_PTS:
                    continue

                actual_days = max(time_diff / 86400.0, 0.001)
                effective_days = max(actual_days, 1.0)
                velocity = gain / effective_days
                if velocity < SURGE_MIN_VELOCITY:
                    continue

                w_count = j - i + 1
                win_rate = (wins / w_count) * 100.0
                if win_rate < SURGE_MIN_WIN_RATE:
                    continue

                window_games = games[i:j + 1]
                pace_game = gain / (w_count - 1)
                acc_window_count = len(analyzed_in_window)
                acc_coverage_pct = (acc_window_count / w_count) * 100.0
                avg_window_acc = sum(analyzed_in_window) / acc_window_count if acc_window_count > 0 else None

                reactivation_info = None
                for d in cat_dormancy:
                    if 0 <= (i - d["post_game_index"]) <= 2:
                        reactivation_info = d
                        break

                surges.append({
                    "category": cat,
                    "gain": gain,
                    "start_rating": start_g["player_rating"],
                    "end_rating": end_g["player_rating"],
                    "game_count": w_count,
                    "start_time": start_ts,
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
                    "acc_count": acc_window_count,
                    "acc_coverage": acc_coverage_pct,
                    "avg_acc": avg_window_acc,
                })

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
            s["surge_index"] = idx
            start_dt = datetime.datetime.fromtimestamp(s["start_time"], tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            end_dt = datetime.datetime.fromtimestamp(s["end_time"], tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            days_str = f"{s['days']:.1f} days" if s['days'] >= 1.0 else f"{round(s['days'] * 24, 1)} hours"
            opp_ratings = [g["opponent_rating"] for g in s["window_games"]]
            avg_opp = sum(opp_ratings) / len(opp_ratings) if opp_ratings else 0
            min_opp = min(opp_ratings) if opp_ratings else 0
            max_opp = max(opp_ratings) if opp_ratings else 0

            acc_info_str = f"Avg Acc: {s['avg_acc']:.1f}% ({s['acc_count']}/{s['game_count']} analyzed, {s['acc_coverage']:.0f}% coverage)" if s["avg_acc"] is not None else "Avg Acc: N/A (0 analyzed)"

            print(f"\n[Surge #{idx}] {s['category'].capitalize()} | +{s['gain']} pts ({s['start_rating']} -> {s['end_rating']}) over {s['game_count']} games | {days_str}", flush=True)
            print(f"Record: {s['wins']} Wins, {s['losses']} Losses, {s['draws']} Draws ({s['win_rate']:.1f}% Win Rate) | Pace: +{s['pace_game']:.1f} pts/game (+{s['pace_day']:.1f} pts/day)", flush=True)
            print(f"Review: {acc_info_str}", flush=True)

            if s["reactivation"]:
                gap_d = s["reactivation"]["gap_days"]
                tier_str = s["reactivation"]["tier"]
                print(f"Alert : REACTIVATION SURGE -> Surge began immediately after {gap_d} days of {tier_str.lower()} dormancy!", flush=True)

            print("-" * 102, flush=True)
            print(f"  Start Match : {s['start_rating']} vs {s['start_game']['opponent']} ({s['start_game']['opponent_rating']}) | {start_dt} UTC", flush=True)
            print(f"  Peak Match  : {s['end_rating']} vs {s['end_game']['opponent']} ({s['end_game']['opponent_rating']}) | {end_dt} UTC", flush=True)
            print(f"  Opponents   : Avg {round(avg_opp)} rating (Min: {min_opp}, Max: {max_opp})", flush=True)

    # Section 4D: Rating Landslides & Sandbagging Analysis
    landslides_header_str = "-- RATING LANDSLIDES & SANDBAGGING (Pool-Gated / Fast-Dump Thresholds) "
    print(f"\n{landslides_header_str}" + "-" * max(0, 102 - len(landslides_header_str)), flush=True)

    landslides = []
    for cat in TARGET_CATEGORIES:
        games = category_games[cat]
        n_games = len(games)
        cfg = LANDSLIDE_THRESHOLDS.get(cat, LANDSLIDE_THRESHOLDS["blitz"])
        min_p = cfg["min_pts"]
        min_g = cfg["min_games"]
        max_d = cfg["max_days"]
        min_lr = cfg["min_loss_rate"]
        max_landslide_seconds = max_d * 86400.0

        start_bound = max(0, SURGE_IGNORE_ONBOARDING)
        min_window_span = min(FAST_DUMP_MIN_GAMES, min_g)

        for i in range(start_bound, n_games - min_window_span + 1):
            start_g = games[i]
            start_ts = start_g["end_time"]

            wins = 0
            losses = 0
            draws = 0
            short_ply_count = 0
            sum_ply = 0
            abandoned_deficit_count = 0

            # Pre-accumulate up to min_window_span - 1
            for k in range(i, i + min_window_span - 1):
                g_k = games[k]
                out = g_k["outcome"]
                if out == "WIN":
                    wins += 1
                elif out == "LOSS":
                    losses += 1
                elif out == "DRAW":
                    draws += 1
                p_k = g_k["ply"]
                if p_k <= MAX_PLY:
                    short_ply_count += 1
                sum_ply += p_k
                if g_k.get("is_deficit_abandon", False):
                    abandoned_deficit_count += 1

            for j in range(i + min_window_span - 1, n_games):
                end_g = games[j]
                time_diff = end_g["end_time"] - start_ts
                if time_diff > max_landslide_seconds:
                    break

                out = end_g["outcome"]
                if out == "WIN":
                    wins += 1
                elif out == "LOSS":
                    losses += 1
                elif out == "DRAW":
                    draws += 1
                p_j = end_g["ply"]
                if p_j <= MAX_PLY:
                    short_ply_count += 1
                sum_ply += p_j
                if end_g.get("is_deficit_abandon", False):
                    abandoned_deficit_count += 1

                drop = start_g["player_rating"] - end_g["player_rating"]
                if drop <= 0:
                    continue

                w_count = j - i + 1
                loss_rate = (losses / w_count) * 100.0
                short_ply_ratio = short_ply_count / w_count

                is_macro = (drop >= min_p and w_count >= min_g and loss_rate >= min_lr)
                is_fast_dump = (drop >= FAST_DUMP_MIN_PTS and w_count >= FAST_DUMP_MIN_GAMES and short_ply_ratio >= FAST_DUMP_SHORT_PLY_RATIO and loss_rate >= FAST_DUMP_MIN_LOSS_RATE)

                if not (is_macro or is_fast_dump):
                    continue

                actual_days = max(time_diff / 86400.0, 0.001)
                effective_days = max(actual_days, 1.0)
                velocity = drop / effective_days
                pace_game = drop / (w_count - 1)
                avg_ply = sum_ply / w_count

                precursor_surge = None
                for s in filtered_surges:
                    if s["category"] == cat and 0 <= (s["start_time"] - end_g["end_time"]) <= (14 * 86400):
                        precursor_surge = s
                        break

                if is_fast_dump or short_ply_ratio >= 0.40 or avg_ply < 15.0:
                    classifier = "EXPLICIT DUMP"
                    classifier_desc = "EXPLICIT RATING DUMP -> High concentration of rapid short-ply resignations/aborts."
                elif precursor_surge is not None:
                    classifier = "REBOUND DUMP / CYCLE"
                    classifier_desc = f"MANIPULATED SANDBAG/SURGE CYCLE -> Landslide served as deflated floor directly preceding Surge #{precursor_surge.get('surge_index', '?')}."
                else:
                    classifier = "TILT SPIRAL"
                    classifier_desc = "ORGANIC TILT / SLUMP -> Full-length games with normal move counts."

                window_games = games[i:j + 1]

                landslides.append({
                    "category": cat,
                    "drop": drop,
                    "start_rating": start_g["player_rating"],
                    "end_rating": end_g["player_rating"],
                    "game_count": w_count,
                    "start_time": start_ts,
                    "end_time": end_g["end_time"],
                    "days": actual_days,
                    "wins": wins,
                    "losses": losses,
                    "draws": draws,
                    "loss_rate": loss_rate,
                    "pace_game": pace_game,
                    "pace_day": velocity,
                    "start_game": start_g,
                    "end_game": end_g,
                    "window_games": window_games,
                    "short_ply_count": short_ply_count,
                    "short_ply_ratio": short_ply_ratio,
                    "avg_ply": avg_ply,
                    "abandoned_deficit": abandoned_deficit_count,
                    "precursor_surge": precursor_surge,
                    "classifier": classifier,
                    "classifier_desc": classifier_desc,
                })

    landslides.sort(key=lambda l: l["start_time"])
    filtered_landslides = []
    for l in landslides:
        if not filtered_landslides:
            filtered_landslides.append(l)
            continue
        prev = filtered_landslides[-1]
        if l["category"] == prev["category"] and l["start_time"] <= prev["end_time"]:
            if l["drop"] > prev["drop"]:
                filtered_landslides[-1] = l
        else:
            filtered_landslides.append(l)

    if not filtered_landslides:
        print("No high-velocity rating landslides detected.", flush=True)
    else:
        for idx, l in enumerate(filtered_landslides, start=1):
            l["landslide_index"] = idx
            start_dt = datetime.datetime.fromtimestamp(l["start_time"], tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            end_dt = datetime.datetime.fromtimestamp(l["end_time"], tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            days_str = f"{l['days']:.1f} days" if l['days'] >= 1.0 else f"{round(l['days'] * 24, 1)} hours"

            print(f"\n[Landslide #{idx}] {l['category'].capitalize()} | -{l['drop']} pts ({l['start_rating']} -> {l['end_rating']}) over {l['game_count']} games | {days_str}", flush=True)
            print(f"Record: {l['wins']} Wins, {l['losses']} Losses, {l['draws']} Draws ({l['loss_rate']:.1f}% Loss Rate) | Pace: -{l['pace_game']:.1f} pts/game (-{l['pace_day']:.1f} pts/day)", flush=True)
            print(f"Dump Telemetry: {l['short_ply_count']}/{l['game_count']} Short-Ply games (Avg: {l['avg_ply']:.1f} ply) | Abandoned/Deficit: {l['abandoned_deficit']} games", flush=True)
            print(f"Alert : [{l['classifier']}] -> {l['classifier_desc']}", flush=True)
            print("-" * 102, flush=True)
            print(f"  Start Match : {l['start_rating']} vs {l['start_game']['opponent']} ({l['start_game']['opponent_rating']}) | {start_dt} UTC", flush=True)
            print(f"  Trough Match: {l['end_rating']} vs {l['end_game']['opponent']} ({l['end_game']['opponent_rating']}) | {end_dt} UTC", flush=True)
            if l["precursor_surge"]:
                gap_days = (l["precursor_surge"]["start_time"] - l["end_time"]) / 86400.0
                print(f"  Precursor   : Occurred {gap_days:.1f} days prior to Surge #{l['precursor_surge'].get('surge_index', '?')} (+{l['precursor_surge']['gain']} pts)", flush=True)

    # Section 5: Forensic Synthesis & Verdict Determination (With Recency Decay & Pool Congruence)
    latest_game_ts = max((g["end_time"] for g in all_rated_games), default=time.time())
    recency_cutoff_ts = latest_game_ts - RECENCY_LOOKBACK_SECONDS

    live_ratings = {cat: stats[cat]["latest_rating"] for cat in LIVE_CATEGORIES if stats[cat]["latest_rating"] is not None}

    signals_smoke = []
    signals_fire = []
    historical_anomalies = []
    primary_triggers = []

    # Check Surges
    for s in filtered_surges:
        s_cat = s["category"].capitalize()
        s_idx = s.get("surge_index", "?")
        s_start_d = datetime.datetime.fromtimestamp(s["start_time"], tz=datetime.timezone.utc).strftime("%Y-%m-%d")
        s_end_d = datetime.datetime.fromtimestamp(s["end_time"], tz=datetime.timezone.utc).strftime("%Y-%m-%d")
        date_span_str = f"{s_start_d}" if s_start_d == s_end_d else f"{s_start_d} -> {s_end_d}"
        is_recent = (s["end_time"] >= recency_cutoff_ts)

        other_live_max = max([r for c, r in live_ratings.items() if c != s["category"]], default=0)
        is_congruent_with_pool = (s["end_rating"] <= (other_live_max + CONGRUENCE_TOLERANCE_PTS)) if other_live_max > 0 else False

        if s["avg_acc"] is not None and s["acc_count"] >= 5 and s["acc_coverage"] >= 30.0 and s["avg_acc"] >= 93.0:
            sig_text = f"[Surge #{s_idx}] {s_cat} | High-velocity surge corroborated by extreme accuracy (+{s['gain']} pts, {s['win_rate']:.1f}% WR, {s['avg_acc']:.1f}% avg acc across {s['acc_count']} games) | {date_span_str}"
            if is_recent:
                signals_fire.append(sig_text)
                if "Engine-Corroborated Rating Surge" not in primary_triggers:
                    primary_triggers.append("Engine-Corroborated Rating Surge")
            else:
                historical_anomalies.append(f"[Historical Surge #{s_idx}] {sig_text}")
        elif s["reactivation"] and s["pace_day"] >= 25.0 and s["win_rate"] >= 80.0:
            sig_text = f"[Surge #{s_idx}] {s_cat} | Reactivation surge (+{s['gain']} pts, {s['win_rate']:.1f}% win rate, +{s['pace_day']:.1f} pts/day post-dormancy) | {date_span_str}"
            if is_recent:
                signals_fire.append(sig_text)
                if "Post-Dormancy Reactivation Surge" not in primary_triggers:
                    primary_triggers.append("Post-Dormancy Reactivation Surge")
            else:
                historical_anomalies.append(f"[Historical Surge #{s_idx}] {sig_text}")
        elif s["gain"] >= 200 and s["days"] <= 7.0 and s["pace_day"] >= 35.0 and s["win_rate"] >= 85.0:
            sig_text = f"[Surge #{s_idx}] {s_cat} | High-velocity macro surge (+{s['gain']} pts in {s['days']:.1f}d at +{s['pace_day']:.1f} pts/day, {s['win_rate']:.1f}% win rate) | {date_span_str}"
            if is_recent:
                signals_fire.append(sig_text)
                if "High-Velocity Macro Surge" not in primary_triggers:
                    primary_triggers.append("High-Velocity Macro Surge")
            else:
                historical_anomalies.append(f"[Historical Surge #{s_idx}] {sig_text}")
        elif 75.0 <= s["win_rate"] < 85.0 or s["pace_day"] >= 20.0:
            sig_text = f"[Surge #{s_idx}] {s_cat} | Elevated surge session (+{s['gain']} pts over {s['game_count']} games, {s['win_rate']:.1f}% win rate, +{s['pace_day']:.1f} pts/day) | {date_span_str}"
            if is_recent:
                if is_congruent_with_pool:
                    historical_anomalies.append(f"[Suppressed Surge #{s_idx}] {s_cat} | Surge peak ({s['end_rating']}) congruent with established rating baseline ({other_live_max}) | {date_span_str}")
                else:
                    signals_smoke.append(sig_text)
                    if len([x for x in filtered_surges if x["end_time"] >= recency_cutoff_ts]) >= 2:
                        if "Rating Volatility / Multi-Surge Recovery" not in primary_triggers:
                            primary_triggers.append("Rating Volatility / Multi-Surge Recovery")
                    else:
                        if "Isolated High-Velocity Surge" not in primary_triggers:
                            primary_triggers.append("Isolated High-Velocity Surge")
            else:
                historical_anomalies.append(f"[Historical Surge #{s_idx}] {sig_text}")

    # Check Landslides
    for l in filtered_landslides:
        l_cat = l["category"].capitalize()
        l_idx = l.get("landslide_index", "?")
        l_start_d = datetime.datetime.fromtimestamp(l["start_time"], tz=datetime.timezone.utc).strftime("%Y-%m-%d")
        l_end_d = datetime.datetime.fromtimestamp(l["end_time"], tz=datetime.timezone.utc).strftime("%Y-%m-%d")
        date_span_str = f"{l_start_d}" if l_start_d == l_end_d else f"{l_start_d} -> {l_end_d}"
        is_recent = (l["end_time"] >= recency_cutoff_ts)

        if l["classifier"] == "EXPLICIT DUMP":
            sig_text = f"[Landslide #{l_idx}] {l_cat} | Explicit short-ply rating dump (-{l['drop']} pts in {l['days']:.1f}d, {l['short_ply_count']} short-ply games) | {date_span_str}"
            if is_recent:
                signals_fire.append(sig_text)
                if "Explicit Short-Ply Rating Dumping" not in primary_triggers:
                    primary_triggers.append("Explicit Short-Ply Rating Dumping")
            else:
                historical_anomalies.append(f"[Historical Landslide #{l_idx}] {sig_text}")
        elif l["classifier"] == "REBOUND DUMP / CYCLE":
            s_idx = l["precursor_surge"].get("surge_index", "?")
            sig_text = f"[Landslide #{l_idx}] {l_cat} | Sandbagging-surge cycle (-{l['drop']} pts dump serving as baseline for Surge #{s_idx}) | {date_span_str}"
            if is_recent:
                signals_fire.append(sig_text)
                if "Sandbagging-Surge Cycle" not in primary_triggers:
                    primary_triggers.append("Sandbagging-Surge Cycle")
            else:
                historical_anomalies.append(f"[Historical Landslide #{l_idx}] {sig_text}")
        else:
            sig_text = f"[Landslide #{l_idx}] {l_cat} | High-velocity tilt spiral / slump (-{l['drop']} pts over {l['game_count']} games, {l['loss_rate']:.1f}% loss rate) | {date_span_str}"
            if is_recent:
                signals_smoke.append(sig_text)
                if "High-Velocity Tilt Spiral / Slump" not in primary_triggers:
                    primary_triggers.append("High-Velocity Tilt Spiral / Slump")
            else:
                historical_anomalies.append(f"[Historical Landslide #{l_idx}] {sig_text}")

    # Check High-Accuracy Winning Streaks
    for idx_a, a_streak in enumerate(acc_streaks, start=1):
        avg_a = sum(g["accuracy"] for g in a_streak) / len(a_streak)
        a_cat = a_streak[0]["time_class"].capitalize()
        a_start_d = datetime.datetime.fromtimestamp(a_streak[0]["end_time"], tz=datetime.timezone.utc).strftime("%Y-%m-%d")
        a_end_d = datetime.datetime.fromtimestamp(a_streak[-1]["end_time"], tz=datetime.timezone.utc).strftime("%Y-%m-%d")
        a_date_span = f"{a_start_d}" if a_start_d == a_end_d else f"{a_start_d} -> {a_end_d}"
        is_recent = (a_streak[-1]["end_time"] >= recency_cutoff_ts)

        if len(a_streak) >= 5:
            sig_text = f"[Acc Streak #{idx_a}] {a_cat} | Severe high-accuracy winning streak ({len(a_streak)} consecutive wins >= {MIN_ACC_PLY} ply, avg {avg_a:.1f}% acc) | {a_date_span}"
            if is_recent:
                signals_fire.append(sig_text)
                if "Severe High-Accuracy Streak" not in primary_triggers:
                    primary_triggers.append("Severe High-Accuracy Streak")
            else:
                historical_anomalies.append(f"[Historical Acc Streak #{idx_a}] {sig_text}")
        elif len(a_streak) >= 4 and filtered_surges:
            sig_text = f"[Acc Streak #{idx_a}] {a_cat} | High-accuracy winning streak corroborated by surge ({len(a_streak)} consecutive wins, avg {avg_a:.1f}% acc) | {a_date_span}"
            if is_recent:
                signals_fire.append(sig_text)
                if "Surge-Corroborated Accuracy Streak" not in primary_triggers:
                    primary_triggers.append("Surge-Corroborated Accuracy Streak")
            else:
                historical_anomalies.append(f"[Historical Acc Streak #{idx_a}] {sig_text}")
        elif len(a_streak) >= 4:
            sig_text = f"[Acc Streak #{idx_a}] {a_cat} | Sustained high-accuracy winning streak ({len(a_streak)} consecutive wins >= {MIN_ACC_PLY} ply, avg {avg_a:.1f}%) | {a_date_span}"
            if is_recent:
                signals_smoke.append(sig_text)
                if "Sustained High-Accuracy Session" not in primary_triggers:
                    primary_triggers.append("Sustained High-Accuracy Session")
            else:
                historical_anomalies.append(f"[Historical Acc Streak #{idx_a}] {sig_text}")

    # Check Live Short-Ply Streaks (Filtered by Recency)
    recent_streaks = [st for st in streaks if st[-1]["end_time"] >= recency_cutoff_ts]
    recent_short_games = sum(len(st) for st in recent_streaks)
    recent_max_streak_len = max([len(st) for st in recent_streaks]) if recent_streaks else 0

    total_short_games = sum(len(st) for st in streaks)
    max_streak_len = max([len(st) for st in streaks]) if streaks else 0

    if recent_max_streak_len >= 5 or recent_short_games >= 12:
        signals_fire.append(f"Severe live short-ply streaks (Max: {recent_max_streak_len} consecutive games, Total: {recent_short_games} in past 2 years)")
        if "Severe Short-Ply Rating Dumping/Farming" not in primary_triggers:
            primary_triggers.append("Severe Short-Ply Rating Dumping/Farming")
    elif len(recent_streaks) >= 2:
        signals_smoke.append(f"Multiple live short-ply streaks detected ({len(recent_streaks)} streaks in past 2 years)")
        if "Moderate Short-Ply Activity" not in primary_triggers:
            primary_triggers.append("Moderate Short-Ply Activity")

    for st in streaks:
        if st[-1]["end_time"] < recency_cutoff_ts:
            st_start_d = datetime.datetime.fromtimestamp(st[0]["end_time"], tz=datetime.timezone.utc).strftime("%Y-%m-%d")
            historical_anomalies.append(f"[Historical Short-Ply Streak] {len(st)} games ({st[0]['time_class'].capitalize()}) | {st_start_d}")

    # Final graduated verdict determination
    if signals_fire:
        verdict = "Fire"
        reasons = signals_fire
    elif signals_smoke:
        verdict = "Smoke"
        reasons = signals_smoke
    else:
        verdict = "Human"
        primary_triggers = ["None (Clean Organic Baseline)"]
        reasons = ["Rating progression, accuracy distribution, and volume profiles align with standard organic play."]

    print("\n" + "=" * 102, flush=True)
    print(" FINAL FORENSIC SYNTHESIS", flush=True)
    print("=" * 102, flush=True)
    print(f" Verdict         : {verdict}", flush=True)
    print(f" Trigger Category: {', '.join(primary_triggers)}", flush=True)
    print(f" Primary Signals :", flush=True)
    streak_summary = f"{len(streaks)} streaks found (Max: {max_streak_len} games, Total: {total_short_games} games, Live pools only)" if streaks else "None detected"
    print(f"   - [Short-Ply Streaks]   : {streak_summary}", flush=True)

    if acc_streaks:
        max_acc_len = max(len(st) for st in acc_streaks)
        peak_acc_all = max(max(g["accuracy"] for g in st) for st in acc_streaks)
        acc_signal_str = f"{len(acc_streaks)} winning streaks found (Max: {max_acc_len} games, Peak Acc: {peak_acc_all:.1f}%)"
    else:
        overall_acc_coverage = (total_acc_count / total_games_all) * 100.0 if total_games_all > 0 else 0.0
        if total_acc_count < MIN_ANALYZED_THRESHOLD:
            acc_signal_str = f"None detected (Sparse data: {total_acc_count}/{total_games_all} analyzed - {overall_acc_coverage:.1f}% coverage)"
        else:
            acc_signal_str = f"None detected ({total_acc_count}/{total_games_all} analyzed - {overall_acc_coverage:.1f}% coverage)"
    print(f"   - [Accuracy Profile]    : {acc_signal_str}", flush=True)

    ext_gaps = [d for d in dormancy_events if d["tier"] == "Extended"]
    max_gap_days = max([d["gap_days"] for d in dormancy_events]) if dormancy_events else 0
    gap_summary = f"{len(dormancy_events)} gaps found ({len(ext_gaps)} extended, Max: {max_gap_days} days)" if dormancy_events else "None detected"
    print(f"   - [Dormancy Gaps]       : {gap_summary}", flush=True)

    if filtered_landslides:
        dumps = sum(1 for l in filtered_landslides if l["classifier"] == "EXPLICIT DUMP")
        cycles = sum(1 for l in filtered_landslides if l["classifier"] == "REBOUND DUMP / CYCLE")
        tilts = sum(1 for l in filtered_landslides if l["classifier"] == "TILT SPIRAL")
        landslide_signal_str = f"{len(filtered_landslides)} landslides detected ({dumps} dumps, {cycles} cycles, {tilts} tilt slides)"
    else:
        landslide_signal_str = "None detected"
    print(f"   - [Landslide Profile]   : {landslide_signal_str}", flush=True)

    if filtered_surges:
        surge_counts = {}
        for s in filtered_surges:
            c = s["category"].capitalize()
            surge_counts[c] = surge_counts.get(c, 0) + 1
        surge_breakdown_str = ", ".join([f"{k}: {v}" for k, v in surge_counts.items()])
        surge_signal_str = f"{len(filtered_surges)} high-velocity surges detected ({surge_breakdown_str})"
    else:
        surge_signal_str = "0 high-velocity surges detected"
    print(f"   - [Surge Profile]       : {surge_signal_str}", flush=True)

    print(f" Evaluation Details (Active Lookback: Past 2 Years):", flush=True)
    for r in reasons:
        print(f"   * {r}", flush=True)

    if historical_anomalies:
        print(f" Historical Anomalies (> 2 Years Old):", flush=True)
        for h in historical_anomalies:
            print(f"   * {h}", flush=True)

    print("=" * 102 + "\n", flush=True)


if __name__ == "__main__":
    main()
