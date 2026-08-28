#!/usr/bin/env python3
"""
chesscom-rating-summary.py
Analyzes Chess.com user archives for rating trajectories, surges, dormancies,
short-ply streaks, and generates a lifetime performance synthesis.
"""

import sys
import json
import urllib.request
import urllib.error
from datetime import datetime, timezone

__version__ = "0.0.13"

SURGE_MIN_GAIN = 150
SURGE_MIN_GAMES = 15
SURGE_MAX_DAYS = 7.0
SURGE_MIN_PACE_DAY = 20.0
SHORT_PLY_THRESHOLD = 13
SHORT_PLY_MIN_STREAK = 3
DORMANCY_STANDARD_DAYS = 30
DORMANCY_EXTENDED_DAYS = 90


def fetch_json(url):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "ChessRatingSummaryScript/1.0 (FairPlayAnalysis)"}
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise e


def parse_pgn_headers(pgn_text):
    headers = {}
    for line in pgn_text.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            content = line[1:-1].strip()
            parts = content.split(" ", 1)
            if len(parts) == 2:
                key = parts[0]
                val = parts[1].strip('\"')
                headers[key] = val
    return headers


def format_utc(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def analyze_account(username):
    user_clean = username.strip().lower()
    archives_data = fetch_json(f"https://api.chess.com/pub/player/{user_clean}/games/archives")
    
    if not archives_data or "archives" not in archives_data:
        print(f"[-] Error: Could not fetch archives for user '{username}'.")
        sys.exit(1)

    archives = archives_data["archives"]
    print(f"[*] Found {len(archives)} monthly archives for '{username}'. Fetching...")

    games_by_pool = {"bullet": [], "blitz": [], "rapid": []}

    for archive_url in archives:
        data = fetch_json(archive_url)
        if not data or "games" not in data:
            continue

        for g in data["games"]:
            if not g.get("rated", False):
                continue

            time_class = g.get("time_class")
            if time_class not in games_by_pool:
                continue

            white = g.get("white", {})
            black = g.get("black", {})
            
            is_white = white.get("username", "").lower() == user_clean
            is_black = black.get("username", "").lower() == user_clean

            if not (is_white or is_black):
                continue

            user_data = white if is_white else black
            opp_data = black if is_white else white

            user_rating = user_data.get("rating")
            opp_rating = opp_data.get("rating")
            user_result = user_data.get("result", "")
            opp_username = opp_data.get("username", "Unknown")
            end_time = g.get("end_time", 0)

            # Extract ply count if PGN exists
            ply_count = 0
            pgn = g.get("pgn", "")
            if pgn:
                headers = parse_pgn_headers(pgn)
                ply_count = int(headers.get("PlyCount", 0))

            # Determine win/loss/draw
            if user_result in ["win"]:
                outcome = "W"
            elif user_result in ["agreed", "repetition", "stalemate", "insufficient", "timevsinsufficient", "50move"]:
                outcome = "D"
            else:
                outcome = "L"

            games_by_pool[time_class].append({
                "end_time": end_time,
                "rating": user_rating,
                "opp_rating": opp_rating,
                "opp_username": opp_username,
                "outcome": outcome,
                "ply_count": ply_count
            })

    print(f"[*] Completed scanning {len(archives)} monthly archives.\n")

    # Sort games chronologically
    for pool in games_by_pool:
        games_by_pool[pool].sort(key=lambda x: x["end_time"])

    # 1. RATED RATING SUMMARY TABLE
    print("=" * 88)
    print(f" RATED RATING SUMMARY: {username}")
    print("=" * 88)
    print(f"{'Category':<11}| {'Games':<9}| {'Rating':<9}| {'Last Played (UTC)'}")
    print("-" * 88)

    active_pools = {}
    for pool, pool_name in [("bullet", "Bullet"), ("blitz", "Blitz"), ("rapid", "Rapid")]:
        g_list = games_by_pool[pool]
        if g_list:
            last_game = g_list[-1]
            last_date = format_utc(last_game["end_time"])
            curr_rating = last_game["rating"]
            active_pools[pool_name] = curr_rating
            print(f"{pool_name:<11}| {len(g_list):<9}| {curr_rating:<9}| {last_date}")
        else:
            print(f"{pool_name:<11}| {'0':<9}| {'N/A':<9}| N/A")

    print("-" * 88)
    if len(active_pools) == 1:
        pool_name, r = next(iter(active_pools.items()))
        print(f"Only one active rated category found: {pool_name} ({r}). Spread not applicable.")
    elif len(active_pools) > 1:
        spread = max(active_pools.values()) - min(active_pools.values())
        print(f"Active rating spread across categories: {spread} pts.")
    else:
        print("No rated games found.")
    print("=" * 88 + "\n")

    # 2. SUSPICIOUS SHORT-PLY STREAKS
    print("=" * 88)
    print(" SUSPICIOUS SHORT-PLY STREAKS (0 < Ply <= 13, >= 3 Consecutive Games)")
    print("=" * 88)
    short_ply_streaks_found = 0

    for pool, pool_name in [("bullet", "Bullet"), ("blitz", "Blitz"), ("rapid", "Rapid")]:
        g_list = games_by_pool[pool]
        streak = []
        for g in g_list:
            if 0 < g["ply_count"] <= SHORT_PLY_THRESHOLD:
                streak.append(g)
            else:
                if len(streak) >= SHORT_PLY_MIN_STREAK:
                    short_ply_streaks_found += 1
                    print(f"[{pool_name}] Streak of {len(streak)} games ending at {format_utc(streak[-1]['end_time'])}")
                streak = []
        if len(streak) >= SHORT_PLY_MIN_STREAK:
            short_ply_streaks_found += 1
            print(f"[{pool_name}] Streak of {len(streak)} games ending at {format_utc(streak[-1]['end_time'])}")

    if short_ply_streaks_found == 0:
        print("No consecutive streaks of >= 3 short-ply games detected.")
    print("=" * 88 + "\n")

    # 3. RATING TRAJECTORY, DORMANCY & SURGES
    print("=" * 88)
    print(" RATING TRAJECTORY, DORMANCY & SURGE ANALYSIS")
    print("=" * 88 + "\n")

    # Onboarding
    print("-- INITIAL ACCOUNT ONBOARDING (First 30 Games per Pool) " + "-" * 32)
    print(f"{'Category':<11}| {'Initial -> End':<21}| {'Delta':<10}| {'Win Rate':<10}| {'Max Streak':<12}| {'Avg Opponent'}")
    print("-" * 88)

    for pool, pool_name in [("bullet", "Bullet"), ("blitz", "Blitz"), ("rapid", "Rapid")]:
        g_list = games_by_pool[pool]
        if not g_list:
            print(f"{pool_name:<11}| {'No games played':<21}| {'N/A':<10}| {'N/A':<10}| {'N/A':<12}| N/A")
            continue

        sample = g_list[:30]
        init_r = sample[0]["rating"]
        end_r = sample[-1]["rating"]
        delta = end_r - init_r
        delta_str = f"+{delta}" if delta >= 0 else str(delta)

        wins = sum(1 for g in sample if g["outcome"] == "W")
        win_rate = (wins / len(sample)) * 100

        max_streak = 0
        curr_streak = 0
        for g in sample:
            if g["outcome"] == "W":
                curr_streak += 1
                max_streak = max(max_streak, curr_streak)
            else:
                curr_streak = 0

        valid_opp_ratings = [g["opp_rating"] for g in sample if g["opp_rating"]]
        avg_opp = round(sum(valid_opp_ratings) / len(valid_opp_ratings)) if valid_opp_ratings else 0

        range_str = f"{init_r} -> {end_r} (#{len(sample)})"
        print(f"{pool_name:<11}| {range_str:<21}| {delta_str:<10}| {win_rate:>5.1f}%    | {max_streak} games     | {avg_opp}")

    print("-" * 88 + "\n")

    # Inactivity / Dormancy
    print("-- INACTIVITY & DORMANCY GAPS (Standard >= 30d, Extended >= 90d) " + "-" * 23)
    print(f"{'Category':<11}| {'Inactive Period (UTC)':<26}| {'Duration':<12}| {'Tier':<12}| {'Return Trajectory (First 5 Games)'}")
    print("-" * 88)

    dormancy_gaps_found = []
    for pool, pool_name in [("bullet", "Bullet"), ("blitz", "Blitz"), ("rapid", "Rapid")]:
        g_list = games_by_pool[pool]
        for i in range(len(g_list) - 1):
            t1 = g_list[i]["end_time"]
            t2 = g_list[i + 1]["end_time"]
            gap_days = (t2 - t1) / 86400.0

            if gap_days >= DORMANCY_STANDARD_DAYS:
                tier = "Extended" if gap_days >= DORMANCY_EXTENDED_DAYS else "Standard"
                period_str = f"{format_utc(t1)[:10]} -> {format_utc(t2)[:10]}"
                
                # First 5 games after return
                return_sample = g_list[i + 1 : i + 6]
                ret_init = return_sample[0]["rating"]
                ret_end = return_sample[-1]["rating"]
                ret_delta = ret_end - ret_init
                ret_delta_str = f"+{ret_delta}" if ret_delta >= 0 else str(ret_delta)
                w = sum(1 for g in return_sample if g["outcome"] == "W")
                l = sum(1 for g in return_sample if g["outcome"] == "L")
                d = sum(1 for g in return_sample if g["outcome"] == "D")
                traj_str = f"{ret_init} -> {ret_end} ({ret_delta_str} pts, {w}-{l}-{d})"

                dormancy_gaps_found.append({
                    "pool": pool_name,
                    "period": period_str,
                    "days": round(gap_days),
                    "tier": tier
                })
                print(f"{pool_name:<11}| {period_str:<26}| {round(gap_days)} days     | {tier:<12}| {traj_str}")

    if not dormancy_gaps_found:
        print("No dormancy gaps >= 30 days detected.")
    print("-" * 88 + "\n")

    # High-Velocity Surges
    print(f"-- HIGH-VELOCITY SURGES (Gain >= +{SURGE_MIN_GAIN} pts, >= {SURGE_MIN_GAMES} games, <= {SURGE_MAX_DAYS}d, >= {SURGE_MIN_PACE_DAY} pts/d) " + "-" * 10 + "\n")

    total_surges = 0
    all_surges_data = []

    for pool, pool_name in [("bullet", "Bullet"), ("blitz", "Blitz"), ("rapid", "Rapid")]:
        g_list = games_by_pool[pool]
        n = len(g_list)
        i = 0

        while i < n:
            found_surge = None
            for j in range(i + SURGE_MIN_GAMES, n):
                t_start = g_list[i]["end_time"]
                t_end = g_list[j]["end_time"]
                duration_days = (t_end - t_start) / 86400.0

                if duration_days > SURGE_MAX_DAYS:
                    break

                r_start = g_list[i]["rating"]
                r_end = g_list[j]["rating"]
                gain = r_end - r_start
                pace_day = gain / duration_days if duration_days > 0 else 0

                if gain >= SURGE_MIN_GAIN and pace_day >= SURGE_MIN_PACE_DAY:
                    found_surge = (i, j, duration_days, gain, pace_day)

            if found_surge:
                s_idx, e_idx, dur, gain, pace_day = found_surge
                window = g_list[s_idx : e_idx + 1]
                total_surges += 1

                wins = sum(1 for g in window if g["outcome"] == "W")
                losses = sum(1 for g in window if g["outcome"] == "L")
                draws = sum(1 for g in window if g["outcome"] == "D")
                win_rate = (wins / len(window)) * 100
                pts_game = gain / len(window)

                opp_ratings = [g["opp_rating"] for g in window if g["opp_rating"]]
                avg_opp = round(sum(opp_ratings) / len(opp_ratings)) if opp_ratings else 0
                min_opp = min(opp_ratings) if opp_ratings else 0
                max_opp = max(opp_ratings) if opp_ratings else 0

                start_g = window[0]
                end_g = window[-1]

                print(f"[Surge #{total_surges}] {pool_name} | +{gain} pts ({start_g['rating']} -> {end_g['rating']}) over {len(window)} games | {dur:.1f} days")
                print(f"Record: {wins} Wins, {losses} Losses, {draws} Draws ({win_rate:.1f}% Win Rate) | Pace: +{pts_game:.1f} pts/game (+{pace_day:.1f} pts/day)")
                print("-" * 88)
                print(f"  Start Match : {start_g['rating']} vs {start_g['opp_username']} ({start_g['opp_rating']}) | {format_utc(start_g['end_time'])} UTC")
                print(f"  Peak Match  : {end_g['rating']} vs {end_g['opp_username']} ({end_g['opp_rating']}) | {format_utc(end_g['end_time'])} UTC")
                print(f"  Opponents   : Avg {avg_opp} rating (Min: {min_opp}, Max: {max_opp})\n")

                all_surges_data.append({
                    "id": total_surges,
                    "pool": pool_name,
                    "gain": gain,
                    "games": len(window),
                    "days": dur
                })

                i = e_idx + 1
            else:
                i += 1

    if total_surges == 0:
        print("No high-velocity surges detected matching the target parameters.\n")

    print("=" * 88)

    # 4. LIFETIME SYNTHESIS SUMMARY BLOCK
    print_lifetime_synthesis(username, archives, games_by_pool, total_surges, dormancy_gaps_found, short_ply_streaks_found)


def print_lifetime_synthesis(username, archives, games_by_pool, total_surges, dormancy_gaps, short_ply_streaks):
    print("=" * 88)
    print(f" LIFETIME ACCOUNT & PERFORMANCE SYNTHESIS: {username}")
    print("=" * 88)

    all_games = []
    for g_list in games_by_pool.values():
        all_games.extend(g_list)

    if not all_games:
        print("No rated games available for synthesis.")
        print("=" * 88)
        return

    all_games.sort(key=lambda x: x["end_time"])

    total_games_count = len(all_games)
    first_ts = all_games[0]["end_time"]
    last_ts = all_games[-1]["end_time"]
    timespan_days = max((last_ts - first_ts) / 86400.0, 1.0)
    months_count = len(archives)
    avg_per_day = total_games_count / timespan_days
    avg_per_month = total_games_count / max(months_count, 1)

    print(f"* Archive Timespan        : {format_utc(first_ts)[:10]} to {format_utc(last_ts)[:10]} ({months_count} monthly archives, {timespan_days:.1f} days)")
    print(f"* Total Rated Games       : {total_games_count} (Avg: {avg_per_day:.1f} games/day, {avg_per_month:.1f} games/month)")

    # Pool-specific metrics
    for pool, pool_name in [("rapid", "Rapid"), ("blitz", "Blitz"), ("bullet", "Bullet")]:
        g_list = games_by_pool[pool]
        if not g_list:
            continue

        ratings = [g["rating"] for g in g_list if g["rating"]]
        init_r = g_list[0]["rating"]
        curr_r = g_list[-1]["rating"]
        net_delta = curr_r - init_r
        net_delta_str = f"+{net_delta}" if net_delta >= 0 else str(net_delta)

        peak_r = max(ratings) if ratings else curr_r
        peak_idx = ratings.index(peak_r) if ratings else 0
        peak_date = format_utc(g_list[peak_idx]["end_time"])

        floor_r = min(ratings) if ratings else curr_r

        wins = sum(1 for g in g_list if g["outcome"] == "W")
        losses = sum(1 for g in g_list if g["outcome"] == "L")
        draws = sum(1 for g in g_list if g["outcome"] == "D")
        lifetime_wr = (wins / len(g_list)) * 100

        print(f"* Pool Focus [{pool_name:<5}]    : {len(g_list)} games | Lifetime Record: {wins}W / {losses}L / {draws}D ({lifetime_wr:.1f}% WR)")
        print(f"  - Initial -> Current    : {init_r} -> {curr_r} ({net_delta_str} net pts)")
        print(f"  - Lifetime Peak / Floor : {peak_r} (on {peak_date}) / {floor_r}")

    print(f"* Surges Detected         : {total_surges} surge window(s)")
    print(f"* Dormancy Gaps (>=30d)   : {len(dormancy_gaps)} gap(s) identified")
    print(f"* Short-Ply Sequences     : {short_ply_streaks} anomalous streak(s)")
    print("=" * 88)


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <chesscom_username>")
        sys.exit(1)

    username = sys.argv[1]
    analyze_account(username)


if __name__ == "__main__":
    main()
