#!/usr/bin/env python3
"""
clock-check.py
A terminal-native chess clock anomaly analyzer that evaluates move-time pacing,
low-variance streaks, instant response ratios, and time distribution anomalies
for a target player using public API game PGNs.
"""

import json
import re
import statistics
import sys
import urllib.request


def prompt_platform():
    """Prompts for target platform with strict option validation."""
    while True:
        platform = input("Platform (1: Chess.com, 2: Lichess) [Default: 1]: ").strip().lower()
        if platform in ["", "1", "chess.com", "chesscom"]:
            return "chesscom"
        elif platform in ["2", "lichess"]:
            return "lichess"
        print("[X] Invalid platform choice. Please enter '1' for Chess.com or '2' for Lichess.")


def prompt_username(platform_key):
    """Prompts for target username."""
    while True:
        username = input(f"Username on {platform_key.capitalize()}: ").strip()
        if username:
            return username
        print("[X] Username cannot be blank.")


def prompt_time_control(platform_key):
    """Prompts for time control with clean, platform-native menus."""
    if platform_key == "chesscom":
        print("\nSelect Chess.com Time Control:")
        print(" 1. Blitz [Default]")
        print(" 2. Rapid")
        print(" 3. Bullet")
        print(" 4. Daily")
        
        tc_map = {
            "1": "blitz",
            "2": "rapid",
            "3": "bullet",
            "4": "daily"
        }
    else:  # Lichess
        print("\nSelect Lichess Time Control:")
        print(" 1. Blitz [Default]")
        print(" 2. Rapid")
        print(" 3. Bullet")
        print(" 4. Classical")
        print(" 5. Correspondence")
        
        tc_map = {
            "1": "blitz",
            "2": "rapid",
            "3": "bullet",
            "4": "classical",
            "5": "correspondence"
        }
    
    while True:
        tc_input = input("Choice [Default: 1]: ").strip()
        if tc_input == "":
            return "blitz"
        if tc_input in tc_map:
            return tc_map[tc_input]
            
        print(f"[X] Invalid choice. Please enter a number between 1 and {len(tc_map)}.")


def prompt_sample_size():
    """Prompts for sample size strictly bounded between 25 and 100."""
    print("\nSample Size Selection:")
    print(" Allowed choices: 25, 50, or 100 games")
    
    while True:
        raw_count = input("Number of games to analyze [Default: 25]: ").strip()
        if raw_count == "":
            return 25
        if raw_count in ["25", "50", "100"]:
            return int(raw_count)
        print("[X] Please select 25, 50, or 100.")


def parse_clk_seconds(clk_str):
    """Converts a %clk HH:MM:SS or MM:SS string into floating seconds."""
    parts = clk_str.split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        elif len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return float(parts[0])
    except ValueError:
        return None


def fetch_chesscom_pgns(username, speed_class, num_games=25):
    """Fetches PGNs from Chess.com archives API matching speed_class."""
    archives_url = f"https://api.chess.com/pub/player/{username}/games/archives"
    
    print(f"\nFetching up to {num_games} {speed_class.upper()} games from Chess.com archives for '{username}'...")
    try:
        req = urllib.request.Request(archives_url, headers={"User-Agent": "ChessClockAnalyzer/1.0"})
        with urllib.request.urlopen(req) as resp:
            archives = json.loads(resp.read().decode()).get("archives", [])
            
        if not archives:
            return []

        matched_pgns = []
        for archive_url in reversed(archives):
            req_arch = urllib.request.Request(archive_url, headers={"User-Agent": "ChessClockAnalyzer/1.0"})
            with urllib.request.urlopen(req_arch) as resp:
                data = json.loads(resp.read().decode()).get("games", [])
                for g in reversed(data):
                    tc_class = g.get("time_class", "").lower()
                    if tc_class == speed_class.lower():
                        pgn_text = g.get("pgn", "")
                        if pgn_text:
                            matched_pgns.append(pgn_text)
                            print(f"\r  [+] Downloaded game {len(matched_pgns)}/{num_games}", end="", flush=True)
                            if len(matched_pgns) >= num_games:
                                print()
                                return matched_pgns
        print()
        return matched_pgns
    except Exception as e:
        print(f"\nNetwork/API error fetching games: {e}")
        return []


def fetch_lichess_pgns(username, speed_class, num_games=25):
    """Fetches PGNs from Lichess API filtering server-side by perfType."""
    url = f"https://lichess.org/api/games/user/{username}?max={num_games}&perfType={speed_class}&clocks=true"
    
    print(f"\nFetching last {num_games} {speed_class.upper()} games from Lichess for '{username}'...")
    try:
        req = urllib.request.Request(
            url, 
            headers={"User-Agent": "ChessClockAnalyzer/1.0", "Accept": "application/x-chess-pgn"}
        )
        with urllib.request.urlopen(req) as resp:
            raw_pgn = resp.read().decode("utf-8", errors="ignore")
            games = [g.strip() for g in raw_pgn.split("\n\n\n") if g.strip()]
            print(f"  [+] Downloaded {len(games)} games from Lichess API.")
            return games[:num_games]
    except Exception as e:
        print(f"Network/API error fetching games: {e}")
        return []


def parse_target_clock_times(pgn_text, target_username):
    """Parses move times (seconds per move) solely for the target player."""
    white_match = re.search(r'\[White\s+"([^"]+)"\]', pgn_text, re.IGNORECASE)
    black_match = re.search(r'\[Black\s+"([^"]+)"\]', pgn_text, re.IGNORECASE)

    if not white_match or not black_match:
        return None

    white_user = white_match.group(1)
    black_user = black_match.group(1)

    is_target_white = (white_user.lower() == target_username.lower())
    is_target_black = (black_user.lower() == target_username.lower())

    if not (is_target_white or is_target_black):
        return None

    target_color = "white" if is_target_white else "black"
    opp_name = black_user if is_target_white else white_user

    # Tokenize moves with %clk comments
    tokens = re.findall(r'(\d+\.+)?\s*([a-zA-B0-9+#=xO-]+)\s*\{\s*\[%clk\s+([0-9:]+)\]\s*\}', pgn_text)
    if not tokens:
        return None

    target_move_times = []
    last_clock = {"white": None, "black": None}
    curr_color = "white"

    for token in tokens:
        _, _, clk_str = token
        clk_sec = parse_clk_seconds(clk_str)

        if clk_sec is not None:
            if last_clock[curr_color] is not None:
                diff = last_clock[curr_color] - clk_sec
                move_time = max(0.05, diff)

                if curr_color == target_color:
                    target_move_times.append(move_time)

            last_clock[curr_color] = clk_sec

        curr_color = "black" if curr_color == "white" else "white"

    return {
        "opp_name": opp_name,
        "target_times": target_move_times
    }


def compute_metrics(move_times):
    """Calculates pacing and anomaly statistics on move duration array."""
    if not move_times:
        return None

    total_moves = len(move_times)
    avg_time = statistics.mean(move_times)
    stdev_time = statistics.stdev(move_times) if total_moves > 1 else 0.0
    median_time = statistics.median(move_times)

    under_05s = sum(1 for t in move_times if t <= 0.5)
    under_10s = sum(1 for t in move_times if t <= 1.0)
    
    # Low-variance runs (5-move windows with std dev < 0.35s)
    consistent_streaks = 0
    if total_moves >= 5:
        for i in range(len(move_times) - 4):
            window = move_times[i:i+5]
            if statistics.stdev(window) < 0.35:
                consistent_streaks += 1

    return {
        "moves": total_moves,
        "avg": avg_time,
        "median": median_time,
        "stdev": stdev_time,
        "sub_05s_pct": (under_05s / total_moves) * 100,
        "sub_10s_pct": (under_10s / total_moves) * 100,
        "hyper_consistent_windows": consistent_streaks,
    }


def main():
    print("==================================================")
    print("      CHESS CLOCK ANALYZER (clock-check.py)       ")
    print("==================================================\n")

    platform_key = prompt_platform()
    username = prompt_username(platform_key)
    speed_class = prompt_time_control(platform_key)
    num_games = prompt_sample_size()

    if platform_key == "chesscom":
        pgns = fetch_chesscom_pgns(username, speed_class, num_games=num_games)
    else:
        pgns = fetch_lichess_pgns(username, speed_class, num_games=num_games)

    if not pgns:
        print(f"\n[!] No matching {speed_class.upper()} games retrieved for '{username}'. Exiting.")
        return

    print(f"\nEvaluating target clock data across {len(pgns)} games...")

    all_target_times = []
    parsed_games = 0

    for idx, pgn in enumerate(pgns, start=1):
        res = parse_target_clock_times(pgn, username)
        if res and res["target_times"]:
            t_times = res["target_times"]
            all_target_times.extend(t_times)
            parsed_games += 1
            
            t_avg = statistics.mean(t_times) if t_times else 0.0
            t_std = statistics.stdev(t_times) if len(t_times) > 1 else 0.0
            
            print(f"[{idx:02d}/{len(pgns)}] vs {res['opp_name']:<18} | Moves: {len(t_times):2d} | Avg: {t_avg:4.1f}s | StDev: {t_std:4.1f}s")

    if not all_target_times:
        print("\n[!] No valid clock tags ([%clk ...]) found in the retrieved PGNs.")
        return

    target_stats = compute_metrics(all_target_times)

    print("\n" + "=" * 55)
    print(f" CLOCK PACING REPORT: {username}")
    print("=" * 55)
    print(f" Platform / Time Control : {platform_key.capitalize()} ({speed_class.upper()})")
    print(f" Evaluated Games         : {parsed_games} games")
    print("-" * 55)
    print(f"{'METRIC':<35} | {'TARGET VALUE':<15}")
    print("-" * 55)
    print(f"{'Total Decisions Analyzed':<35} | {target_stats['moves']:<15}")
    print(f"{'Mean Time Per Move (s)':<35} | {target_stats['avg']:<15.2f}")
    print(f"{'Median Time Per Move (s)':<35} | {target_stats['median']:<15.2f}")
    print(f"{'Standard Deviation (s)':<35} | {target_stats['stdev']:<15.2f}")
    print(f"{'Instant Moves (<=0.5s)':<35} | {target_stats['sub_05s_pct']:<14.1f}%")
    print(f"{'Fast Moves (<=1.0s)':<35} | {target_stats['sub_10s_pct']:<14.1f}%")
    print(f"{'Flat Pacing Windows (5-move runs)':<35} | {target_stats['hyper_consistent_windows']:<15}")
    print("=" * 55)

    print("\nEMPIRICAL DIAGNOSTIC CHECK:")
    flags = 0

    if target_stats["stdev"] < 1.2 and target_stats["avg"] > 3.0:
        print(" [!] LOW VARIANCE: Abnormally flat time-distribution curve (robotic move cadence).")
        flags += 1

    if speed_class in ["rapid", "blitz"] and target_stats["sub_05s_pct"] > 35.0:
        print(f" [!] HIGH INSTANT-MOVE RATIO: {target_stats['sub_05s_pct']:.1f}% of moves executed in <=0.5s.")
        flags += 1

    if target_stats["hyper_consistent_windows"] > (parsed_games * 3):
        print(f" [!] HIGH PACING STREAKS: {target_stats['hyper_consistent_windows']} flat-cadence move sequences detected.")
        flags += 1

    if flags == 0:
        print(" [✓] NORMAL VARIANCE: Move pacing sits fully within expected human time usage.")
    print("=" * 55)


if __name__ == "__main__":
    main()
