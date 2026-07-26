#!/usr/bin/env python3
"""
chess-perf-eval.py
A terminal-native performance comparator tool that analyzes player games against 
local Stockfish engine baselines using direct opponent harvesting, filtered ACPL stats,
global baseline fallback generation, and empirical Z-score statistical diagnostics.

Usage Options (CLI Parameters):
  -p, --player <playername>                     Target player username on the platform.
  -s, --site <chess.com | lichess.org>          Platform site.
  -t, --time-control <bullet|blitz|rapid|classical>
                                                Time control speed category.
                                                Note: 'classical' is valid ONLY for Lichess.org.
  --standard                                    Standard audit level (Target: 200 non-forced decisions).
  --deep                                        Deep audit level (Target: 400 non-forced decisions).
  -h, --help                                    Show the help screen and exit.

Interactive Prompting:
  Any parameter omitted from the command line will be interactively prompted at runtime.
  Invalid command-line options will display an error and re-prompt for correct input.
"""

import io
import os
import sys
import json
import math
import shutil
import random
import argparse
from collections import Counter
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
import chess
import chess.engine
import chess.pgn

# System Configuration
CONFIG_FILE = os.path.join(os.getcwd(), "engine_config.json")

ANALYSIS_DEPTH = 18           # High precision target depth
ANALYSIS_TIME_LIMIT = 5.0     # Hard time limit per move evaluation (seconds)

# Opening Cutoff
OPENING_BOOK_PLIES = 12       # Skip first 6 full moves (12 half-moves)

# ACPL Filtering Thresholds
EVAL_CAP_CENTIPAWNS = 400     # Ignore positions evaluated beyond +/- 400 CP (4.0 pawns)
MAX_SINGLE_MOVE_LOSS = 200    # Cap single-move loss spikes to prevent 1 blunder from ruining game ACPL

# Baseline Harvesting Limits
RATING_WINDOW = 150           # Initial direct peer search window (+/- 150 ELO)
MIN_PEER_DECISIONS = 200      # Strictly required decision count for valid Welch's Z-test

# Network Configuration
MAX_RETRIES = 5
USER_AGENT = "ChessPerfEval (Contact: local_script_user)"

# -----------------------------------------------------------------------------
# Robust HTTP Session Management
# -----------------------------------------------------------------------------
def get_robust_session():
    """
    Creates a requests.Session with exponential backoff and retry adapters
    to handle transient network drops, 429 rate limits, and standard HTTP server errors.
    """
    session = requests.Session()
    retries = Retry(
        total=MAX_RETRIES,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        raise_on_status=False
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({"User-Agent": USER_AGENT})
    return session

HTTP_SESSION = get_robust_session()

# -----------------------------------------------------------------------------
# Terminal Engine Resolution
# -----------------------------------------------------------------------------
def load_saved_engine_path():
    """Loads saved Stockfish path from engine_config.json in current directory if valid."""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                data = json.load(f)
                saved_path = data.get("stockfish_path")
                if saved_path and os.path.isfile(saved_path):
                    return saved_path
        except Exception:
            pass
    return None

def save_engine_path(path):
    """Saves Stockfish path to engine_config.json in current working directory."""
    try:
        config_data = {"stockfish_path": os.path.abspath(path)}
        with open(CONFIG_FILE, "w") as f:
            json.dump(config_data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
            
        if os.path.exists(CONFIG_FILE):
            print(f"[+] Saved configuration to: {CONFIG_FILE}\n")
        else:
            print(f"[!] Error: Failed to verify file creation at {CONFIG_FILE}\n")
    except Exception as e:
        print(f"[!] Error: Could not write configuration file: {e}\n")

def prompt_terminal_path_input():
    """Fallback: Interactively requests executable path and asks permission to save."""
    print("\n" + "=" * 60)
    print("[!] Stockfish executable was not found automatically.")
    print("=" * 60)
    
    while True:
        user_input = input("Enter full path to Stockfish binary (or 'q' to quit): ").strip()
        user_input = user_input.strip("'\"")
        
        if user_input.lower() == 'q':
            print("Exiting.")
            sys.exit(1)
            
        expanded_path = os.path.expanduser(user_input)
        if os.path.isfile(expanded_path):
            print(f"\nAccepted binary path: '{expanded_path}'")
            while True:
                save_choice = input("Save this path to 'engine_config.json' in current directory? (y/n): ").strip().lower()
                if save_choice in ['y', 'yes']:
                    save_engine_path(expanded_path)
                    break
                elif save_choice in ['n', 'no']:
                    print("[+] Path accepted for this session only.\n")
                    break
                else:
                    print("[X] Invalid entry. Please enter 'y' or 'n'.")
                
            return expanded_path
        else:
            print(f"[X] Invalid file path: '{user_input}'. Please try again.\n")

def find_stockfish():
    """Resolves Stockfish executable via saved config, ENV, system PATH, or prompt."""
    saved = load_saved_engine_path()
    if saved:
        return saved

    env_path = os.environ.get("STOCKFISH_PATH")
    if env_path and os.path.isfile(env_path):
        return env_path

    is_win = sys.platform.startswith("win")
    binary_names = ["stockfish.exe", "stockfish"] if is_win else ["stockfish", "stockfish.exe"]

    search_dirs = [
        os.getcwd(),
        "/usr/bin",
        "/usr/local/bin",
        "/usr/games",
        "/opt/homebrew/bin"
    ]

    for d in search_dirs:
        for name in binary_names:
            candidate = os.path.join(d, name)
            if os.path.isfile(candidate):
                return candidate

    for name in binary_names:
        path_in_env = shutil.which(name)
        if path_in_env:
            return path_in_env

    return prompt_terminal_path_input()

# -----------------------------------------------------------------------------
# API Fetchers & Validation
# -----------------------------------------------------------------------------
def validate_username(platform_key, username):
    """Verifies with platform API that the user exists."""
    if platform_key == "chesscom":
        url = f"https://api.chess.com/pub/player/{username}"
    else:
        url = f"https://lichess.org/api/user/{username}"

    try:
        res = HTTP_SESSION.get(url, timeout=8)
        if res.status_code == 200:
            return True, None
        elif res.status_code == 404:
            return False, f"Player '{username}' not found on {platform_key.capitalize()}."
        else:
            return False, f"API check failed with status code {res.status_code}."
    except requests.RequestException as e:
        return False, f"Network error validating user: {e}"

def is_account_active(platform_key, username):
    """Verifies if an opponent account is active or closed/banned for FPV/TOS."""
    if platform_key == "chesscom":
        url = f"https://api.chess.com/pub/player/{username}"
        try:
            res = HTTP_SESSION.get(url, timeout=8)
            if res.status_code == 200:
                status = res.json().get("status", "").lower()
                if "closed" in status:
                    return False, f"Account {status}"
                return True, "Active"
            return False, f"API HTTP {res.status_code}"
        except requests.RequestException as e:
            return False, f"Network error checking status ({type(e).__name__})"
    else:  # Lichess
        url = f"https://lichess.org/api/user/{username}"
        try:
            res = HTTP_SESSION.get(url, timeout=8)
            if res.status_code == 200:
                data = res.json()
                if data.get("closed"):
                    return False, "Account Closed"
                if data.get("tosViolation"):
                    return False, "Fair Play / TOS Violation"
                return True, "Active"
            return False, f"API HTTP {res.status_code}"
        except requests.RequestException as e:
            return False, f"Network error checking status ({type(e).__name__})"

# -----------------------------------------------------------------------------
# Specific Time Control Auto-Detector
# -----------------------------------------------------------------------------
def find_most_frequent_time_control(platform_key, username, speed_class):
    """
    Parses recent archive games strictly within the selected speed category 
    (e.g., 'rapid', 'blitz') to find the single most frequently played exact time control
    (e.g., '15+10', '600', '180+2').
    """
    print(f"\nScanning recent archives for category '{speed_class.upper()}' to detect primary time control...")
    tc_counter = Counter()

    if platform_key == "chesscom":
        archives_url = f"https://api.chess.com/pub/player/{username}/games/archives"
        try:
            res = HTTP_SESSION.get(archives_url, timeout=10)
            if res.status_code == 200:
                archives = res.json().get("archives", [])
                for archive_url in reversed(archives):
                    game_res = HTTP_SESSION.get(archive_url, timeout=10)
                    if game_res.status_code != 200:
                        continue
                    data = game_res.json().get("games", [])
                    for g in data:
                        if g.get("time_class", "").lower() == speed_class.lower():
                            raw_tc = g.get("time_control", "")
                            if raw_tc:
                                tc_counter[raw_tc] += 1
                    if sum(tc_counter.values()) >= 50:
                        break
        except requests.RequestException as e:
            print(f"Error auto-detecting time control: {e}")

    else:  # Lichess
        url = f"https://lichess.org/api/games/user/{username}?max=50&perfType={speed_class}"
        try:
            res = HTTP_SESSION.get(url, headers={"Accept": "application/x-chess-pgn"}, timeout=10)
            if res.status_code == 200:
                pgn_text = io.StringIO(res.text)
                while True:
                    game = chess.pgn.read_game(pgn_text)
                    if game is None:
                        break
                    tc_header = game.headers.get("TimeControl", "")
                    if tc_header:
                        tc_counter[tc_header] += 1
        except requests.RequestException as e:
            print(f"Error auto-detecting time control: {e}")

    if tc_counter:
        top_tc, count = tc_counter.most_common(1)[0]
        print(f"[+] Primary time control detected in '{speed_class.upper()}': '{top_tc}' ({count} recent games).")
        return top_tc

    print(f"[!] Could not determine specific time control for '{speed_class.upper()}'. Harvesting all games in category.")
    return None

# -----------------------------------------------------------------------------
# Game Harvesting Streamer
# -----------------------------------------------------------------------------
def stream_games(platform_key, username, speed_class, exact_tc=None):
    """
    Generator function that streams matching PGN games sequentially from archives
    to allow bucket-filling by decision count.
    """
    if platform_key == "chesscom":
        archives_url = f"https://api.chess.com/pub/player/{username}/games/archives"
        try:
            res = HTTP_SESSION.get(archives_url, timeout=10)
            if res.status_code != 200:
                return
            archives = res.json().get("archives", [])
            for archive_url in reversed(archives):
                game_res = HTTP_SESSION.get(archive_url, timeout=10)
                if game_res.status_code != 200:
                    continue
                data = game_res.json().get("games", [])
                for g in reversed(data):
                    tc_class = g.get("time_class", "")
                    tc_control = g.get("time_control", "")
                    if tc_class.lower() == speed_class.lower():
                        if exact_tc and tc_control != exact_tc:
                            continue
                        pgn_io = io.StringIO(g.get("pgn", ""))
                        game = chess.pgn.read_game(pgn_io)
                        if game:
                            yield game
        except requests.RequestException as e:
            print(f"Network error streaming games: {e}")
            return
    else:  # Lichess
        url = f"https://lichess.org/api/games/user/{username}?perfType={speed_class}"
        try:
            response = HTTP_SESSION.get(url, headers={"Accept": "application/x-chess-pgn"}, stream=True, timeout=10)
            if response.status_code != 200:
                return
            pgn_text = io.StringIO(response.text)
            while True:
                game = chess.pgn.read_game(pgn_text)
                if game is None:
                    break
                tc_header = game.headers.get("TimeControl", "")
                if exact_tc and tc_header != exact_tc:
                    continue
                yield game
        except requests.RequestException as e:
            print(f"Network error streaming games: {e}")
            return

# -----------------------------------------------------------------------------
# Dual Player Game Analysis Engine
# -----------------------------------------------------------------------------
def analyze_game_and_harvest(game, target_user, engine):
    """
    Analyzes critical non-forced decisions for BOTH target player and opponent.
    Filters out opening book (first 6 full moves/12 plies), forced moves,
    lopsided/decided positions (>400 CP / 4.0 pawns), and caps single-move loss at 200 CP.
    """
    headers = game.headers
    white_name = headers.get("White", "")
    black_name = headers.get("Black", "")
    
    white_lower = white_name.lower()
    black_lower = black_name.lower()
    user_lower = target_user.lower()

    if user_lower in white_lower:
        target_color = chess.WHITE
        opp_color = chess.BLACK
        opp_name = black_name
        try:
            target_rating = int(headers.get("WhiteElo", 1500))
            opp_rating = int(headers.get("BlackElo", 1500))
        except ValueError:
            target_rating, opp_rating = 1500, 1500
    elif user_lower in black_lower:
        target_color = chess.BLACK
        opp_color = chess.WHITE
        opp_name = white_name
        try:
            target_rating = int(headers.get("BlackElo", 1500))
            opp_rating = int(headers.get("WhiteElo", 1500))
        except ValueError:
            target_rating, opp_rating = 1500, 1500
    else:
        target_color = chess.WHITE
        opp_color = chess.BLACK
        opp_name = black_name
        try:
            target_rating = int(headers.get("WhiteElo", 1500))
            opp_rating = int(headers.get("BlackElo", 1500))
        except ValueError:
            target_rating, opp_rating = 1500, 1500

    board = game.board()
    move_count = 0

    target_stats = {"rating": target_rating, "decisions": 0, "matches": 0, "cp_loss": 0, "cp_list": [], "match_list": []}
    opp_stats = {"name": opp_name, "rating": opp_rating, "decisions": 0, "matches": 0, "cp_loss": 0, "cp_list": [], "match_list": []}

    eval_limit = chess.engine.Limit(depth=ANALYSIS_DEPTH, time=ANALYSIS_TIME_LIMIT)

    for node in game.mainline():
        move = node.move
        current_turn = board.turn
        move_count += 1

        # 1. Skip opening book (first 6 full moves / 12 half-moves)
        if move_count <= OPENING_BOOK_PLIES:
            board.push(move)
            continue

        # 2. Skip forced moves
        if board.legal_moves.count() <= 1:
            board.push(move)
            continue

        # Engine assessment prior to move
        analysis = engine.analyse(board, eval_limit)
        pv = analysis.get("pv", [])
        top_move = pv[0] if pv else None
        
        score_before = analysis["score"].pov(current_turn)
        
        # 3. Skip mate positions
        if score_before.is_mate():
            board.push(move)
            continue
            
        cp_before = score_before.score(mate_score=10000)
        
        # 4. Filter out decided/lopsided positions (>400 CP / 4.0 pawns)
        if abs(cp_before) > EVAL_CAP_CENTIPAWNS:
            board.push(move)
            continue

        # Evaluate move
        board_after = board.copy()
        board_after.push(move)
        analysis_after = engine.analyse(board_after, eval_limit)
        score_after = analysis_after["score"].pov(current_turn)

        if score_after.is_mate():
            cp_after = 10000 if score_after.mate() > 0 else -10000
        else:
            cp_after = score_after.score(mate_score=10000)

        raw_cp_loss = max(0, cp_before - cp_after)
        bounded_cp_loss = min(raw_cp_loss, MAX_SINGLE_MOVE_LOSS)

        active_dict = target_stats if current_turn == target_color else opp_stats
        active_dict["decisions"] += 1
        active_dict["cp_loss"] += bounded_cp_loss
        active_dict["cp_list"].append(bounded_cp_loss)
        
        is_match = 1 if move == top_move else 0
        active_dict["match_list"].append(is_match)
        if is_match:
            active_dict["matches"] += 1

        board.push(move)

    res_target = target_stats if target_stats["decisions"] > 0 else None
    res_opp = opp_stats if opp_stats["decisions"] > 0 else None

    return res_target, res_opp

# -----------------------------------------------------------------------------
# Statistical Z-Score Engine
# -----------------------------------------------------------------------------
def calculate_sample_variance(data_list):
    """Calculates sample variance (s^2) for a given list of numbers."""
    n = len(data_list)
    if n < 2:
        return 0.0
    mean = sum(data_list) / n
    return sum((x - mean) ** 2 for x in data_list) / (n - 1)

def z_to_p_value(z_score):
    """Converts a positive Z-score to a one-tailed p-value."""
    if z_score <= 0:
        return 0.5
    return 0.5 * math.erfc(z_score / math.sqrt(2))

def compute_z_statistics(target_cp, peer_cp, target_match, peer_match):
    """Computes two-sample Welch's Z-scores and standard errors."""
    n_t = len(target_cp)
    n_p = len(peer_cp)
    
    if n_t < MIN_PEER_DECISIONS or n_p < MIN_PEER_DECISIONS:
        return None

    # 1. ACPL Precision Z-Score
    mean_acpl_t = sum(target_cp) / n_t
    mean_acpl_p = sum(peer_cp) / n_p
    var_acpl_t = calculate_sample_variance(target_cp)
    var_acpl_p = calculate_sample_variance(peer_cp)
    
    se_acpl = math.sqrt((var_acpl_t / n_t) + (var_acpl_p / n_p))
    z_acpl = (mean_acpl_p - mean_acpl_t) / se_acpl if se_acpl > 0 else 0.0

    # 2. Top-1 Match Rate Z-Score
    p_t = sum(target_match) / n_t
    p_p = sum(peer_match) / n_p
    
    var_match_t = p_t * (1.0 - p_t)
    var_match_p = p_p * (1.0 - p_p)
    se_match = math.sqrt((var_match_t / n_t) + (var_match_p / n_p))
    
    z_match = (p_t - p_p) / se_match if se_match > 0 else 0.0

    # 3. Composite Empirical Z-Score
    z_composite = (z_acpl + z_match) / math.sqrt(2)
    p_value = z_to_p_value(z_composite)

    return {
        "n_target": n_t,
        "n_peer": n_p,
        "se_acpl": se_acpl,
        "se_match": se_match,
        "z_acpl": z_acpl,
        "z_match": z_match,
        "z_composite": z_composite,
        "p_value": p_value
    }

# -----------------------------------------------------------------------------
# Interactive Prompts & Input Validation Helpers
# -----------------------------------------------------------------------------
def parse_and_validate_site(raw_site):
    """Normalizes and validates site input. Returns 'chesscom', 'lichess', or None."""
    if not raw_site:
        return None
    val = raw_site.strip().lower()
    if val in ["1", "chess.com", "chesscom"]:
        return "chesscom"
    elif val in ["2", "lichess.org", "lichess"]:
        return "lichess"
    return None

def validate_tc_for_site(platform_key, tc_str):
    """Validates if time control string is valid for given platform."""
    if not tc_str:
        return None
    val = tc_str.strip().lower()
    valid_common = ["bullet", "blitz", "rapid"]
    
    if val in valid_common:
        return val
    elif val == "classical":
        if platform_key == "lichess":
            return "classical"
        else:
            return False  # Classical is invalid for Chess.com
    return None

def prompt_platform():
    """Prompts for target platform with strict option validation."""
    while True:
        platform = input("Platform (1: Chess.com, 2: Lichess) [Default: 1]: ").strip().lower()
        if platform in ["", "1", "chess.com", "chesscom"]:
            return "chesscom"
        elif platform in ["2", "lichess", "lichess.org"]:
            return "lichess"
        print("[X] Invalid choice. Enter '1' for Chess.com or '2' for Lichess.")

def prompt_username(platform_key):
    """Prompts for username and validates immediately against platform API."""
    while True:
        username = input(f"Username on {platform_key.capitalize()}: ").strip()
        if not username:
            print("[X] Username cannot be blank.")
            continue
        
        print(f"Verifying username '{username}' on {platform_key.capitalize()}...")
        valid, err_msg = validate_username(platform_key, username)
        if valid:
            print(f"[+] Username '{username}' verified successfully.")
            return username
        else:
            print(f"[X] {err_msg} Please try again.")

def prompt_time_control(platform_key):
    """Prompts for time control category FIRST before parsing archives."""
    if platform_key == "chesscom":
        print("\nSelect Time Control Category:")
        print(" 1. Blitz [Default]")
        print(" 2. Rapid")
        print(" 3. Bullet")
        print(" 4. Daily")
        
        tc_map = {"1": "blitz", "2": "rapid", "3": "bullet", "4": "daily"}
    else:  # Lichess
        print("\nSelect Time Control Category:")
        print(" 1. Blitz [Default]")
        print(" 2. Rapid")
        print(" 3. Bullet")
        print(" 4. Classical")
        print(" 5. Correspondence")
        
        tc_map = {"1": "blitz", "2": "rapid", "3": "bullet", "4": "classical", "5": "correspondence"}
    
    while True:
        tc_input = input("Choice [Default: 1]: ").strip()
        if tc_input == "":
            return "blitz"
        if tc_input in tc_map:
            return tc_map[tc_input]
            
        print(f"[X] Invalid choice. Enter a number between 1 and {len(tc_map)}.")

def prompt_audit_depth():
    """Prompts for statistical target decision volume (200 vs 400)."""
    print("\nSelect Evaluation Audit Target:")
    print(" 1. Standard Audit  : Target 200 decisions [Default] (Minimum required for Welch's Z-test)")
    print(" 2. Deep Audit      : Target 400 decisions (High-precision forensic review)")
    
    while True:
        choice = input("Choice [Default: 1]: ").strip()
        if choice in ["", "1"]:
            return 200
        elif choice == "2":
            return 400
        print("[X] Invalid choice. Please enter '1' for Standard Audit or '2' for Deep Audit.")

# -----------------------------------------------------------------------------
# Main Execution & Reporting
# -----------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Chess Performance Evaluator CLI - Evaluates player precision and ACPL against local Stockfish engine baselines.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python chess-perf-eval.py -p Hikaru -s chess.com -t blitz --deep
  python chess-perf-eval.py -p MagnusCarlsen -s lichess.org -t classical --standard
  python chess-perf-eval.py  (runs in interactive mode)
"""
    )
    parser.add_argument("-p", "--player", metavar="<playername>", help="Target player username on the platform", type=str, default=None)
    parser.add_argument("-s", "--site", metavar="<site>", help="Platform site ('chess.com' or 'lichess.org')", type=str, default=None)
    parser.add_argument("-t", "--time-control", metavar="<bullet, blitz, rapid, classical>", help="Time control speed category", type=str, default=None)
    
    depth_group = parser.add_mutually_exclusive_group()
    depth_group.add_argument("--standard", action="store_true", help="Standard audit level (Target: 200 non-forced decisions)")
    depth_group.add_argument("--deep", action="store_true", help="Deep audit level (Target: 400 non-forced decisions)")

    args = parser.parse_args()

    print("==================================================")
    print(" CHESS PERFORMANCE EVALUATOR (chess-perf-eval.py)")
    print("==================================================\n")

    # 1. Resolve Engine & Read Official Name
    stockfish_bin = find_stockfish()
    try:
        engine = chess.engine.SimpleEngine.popen_uci(stockfish_bin)
        engine_name = engine.id.get("name", "Stockfish Engine")
    except Exception as e:
        print(f"\nError initializing Stockfish binary at '{stockfish_bin}': {e}")
        if os.path.exists(CONFIG_FILE):
            print(f"Tip: Delete local '{CONFIG_FILE}' to reset the saved path.")
        return

    # 2. Process Command Line Parameters & Prompt ONLY for Missing / Invalid Parameters
    platform_key = None
    if args.site:
        platform_key = parse_and_validate_site(args.site)
        if not platform_key:
            print(f"[X] Error: Invalid site parameter '{args.site}'.")
            platform_key = prompt_platform()
    else:
        platform_key = prompt_platform()

    username = None
    if args.player:
        raw_user = args.player.strip()
        print(f"Verifying CLI username '{raw_user}' on {platform_key.capitalize()}...")
        valid, err_msg = validate_username(platform_key, raw_user)
        if valid:
            print(f"[+] Username '{raw_user}' verified successfully.")
            username = raw_user
        else:
            print(f"[X] {err_msg}")
            username = prompt_username(platform_key)
    else:
        username = prompt_username(platform_key)

    speed_class = None
    if args.time_control:
        tc_res = validate_tc_for_site(platform_key, args.time_control)
        if tc_res is False:
            print(f"[X] Error: 'classical' speed category is only available on Lichess.org, not {platform_key.capitalize()}.")
            speed_class = prompt_time_control(platform_key)
        elif tc_res is None:
            print(f"[X] Error: Invalid time control category '{args.time_control}'. Expected: bullet, blitz, rapid, or classical.")
            speed_class = prompt_time_control(platform_key)
        else:
            speed_class = tc_res
    else:
        speed_class = prompt_time_control(platform_key)

    target_decisions = None
    if args.standard:
        target_decisions = 200
    elif args.deep:
        target_decisions = 400
    else:
        target_decisions = prompt_audit_depth()

    # 3. Detect Specific Time Control within Selected Category ONLY
    exact_time_control = find_most_frequent_time_control(platform_key, username, speed_class)

    tc_label = f"{speed_class.upper()} ({exact_time_control})" if exact_time_control else speed_class.upper()
    print(f"\nEngine Model   : {engine_name}")
    print(f"Target Volume  : {target_decisions} Non-Forced Decisions ({tc_label})")
    print(f"Filters Active : Opening cut <= 6 moves | Eval cap <= |400| CP | Max loss bound = 200 CP\n")

    t_decisions, t_matches, t_cp_loss = 0, 0, 0
    o_decisions, o_matches, o_cp_loss = 0, 0, 0
    t_ratings, o_ratings = [], []
    games_analyzed = 0
    
    target_cp_all, peer_cp_all = [], []
    target_match_all, peer_match_all = [], []
    
    harvested_opponents = set()
    target_clean = username.strip().lower()
    harvested_opponents.add(target_clean)

    # 4. Stream and Harvest Games Until Decision Bucket Hits Target Volume
    game_stream = stream_games(platform_key, username, speed_class, exact_tc=exact_time_control)
    
    for game in game_stream:
        if t_decisions >= target_decisions:
            break

        target_res, opp_res = analyze_game_and_harvest(game, username, engine)
        if target_res and target_res["decisions"] > 0:
            games_analyzed += 1
            t_ratings.append(target_res["rating"])
            t_decisions += target_res["decisions"]
            t_matches += target_res["matches"]
            t_cp_loss += target_res["cp_loss"]
            target_cp_all.extend(target_res["cp_list"])
            target_match_all.extend(target_res["match_list"])
            
            g_match = (target_res["matches"] / target_res["decisions"]) * 100
            g_acpl = target_res["cp_loss"] / target_res["decisions"]
            
            opp_str = f"vs {opp_res['name']} ({opp_res['rating']})" if opp_res else "vs Opponent"
            print(f"[{games_analyzed:02d}] {opp_str} | +{target_res['decisions']:2d} Dec (Progress: {t_decisions}/{target_decisions}) | Top-1: {g_match:5.1f}% | ACPL: {g_acpl:5.1f}")
            
            if opp_res:
                opp_name = opp_res["name"]
                opp_clean = opp_name.strip().lower()
                opp_rating = opp_res["rating"]
                target_rating = target_res["rating"]

                if opp_clean in harvested_opponents:
                    print(f"     └──> [PEER SKIPPED] Opponent '{opp_name}' is a duplicate.")
                elif abs(opp_rating - target_rating) > RATING_WINDOW:
                    print(f"     └──> [PEER SKIPPED] Opponent '{opp_name}' ({opp_rating}) outside rating window (+/- {RATING_WINDOW}).")
                else:
                    active, status_reason = is_account_active(platform_key, opp_name)
                    if not active:
                        print(f"     └──> [PEER EXCLUDED] Opponent '{opp_name}' - {status_reason}")
                    else:
                        harvested_opponents.add(opp_clean)
                        o_ratings.append(opp_rating)
                        o_decisions += opp_res["decisions"]
                        o_matches += opp_res["matches"]
                        o_cp_loss += opp_res["cp_loss"]
                        peer_cp_all.extend(opp_res["cp_list"])
                        peer_match_all.extend(opp_res["match_list"])
                        print(f"     └──> [PEER HARVESTED] Opponent '{opp_name}' ({opp_rating}) added to baseline!")

    engine.quit()

    if t_decisions == 0:
        print("\n[!] No valid critical decisions gathered from recent archives. Exiting.")
        return

    # Metrics Summary
    avg_target_rating = int(sum(t_ratings) / len(t_ratings))
    actual_match_rate = (t_matches / t_decisions) * 100
    actual_acpl = t_cp_loss / t_decisions

    peer_match_rate = (o_matches / o_decisions) * 100 if o_decisions > 0 else 0.0
    peer_acpl = o_cp_loss / o_decisions if o_decisions > 0 else 0.0
    
    min_peer_r = min(o_ratings) if o_ratings else avg_target_rating - RATING_WINDOW
    max_peer_r = max(o_ratings) if o_ratings else avg_target_rating + RATING_WINDOW
    display_peer_count = len(harvested_opponents - {target_clean})

    print("\n" + "=" * 60)
    print(f" EMPIRICAL EVALUATION REPORT: {username}")
    print("=" * 60)
    print(f" Engine Model   : {engine_name}")
    print(f" Target Volume  : {target_decisions} Non-Forced Decisions ({tc_label})")
    print(f" Filters Active : Opening cut <= 6 moves | Eval cap <= |400| CP | Max loss bound = 200 CP")
    print("-" * 60)
    print(f" Platform / Category    : {platform_key.capitalize()} ({speed_class.upper()})")
    print(f" Specific Time Control  : {exact_time_control if exact_time_control else 'All in Category'}")
    print(f" Target Decisions       : {t_decisions} moves across {games_analyzed} valid games")
    print(f" Peer Baseline Volume   : {o_decisions} moves across {display_peer_count} active opponents")
    print(f" Peer Rating Window     : [{min_peer_r} to {max_peer_r}]")
    print("-" * 60)
    print(" PERFORMANCE METRICS     |  TARGET PLAYER  | REAL PEER BASELINE")
    print("-" * 60)
    print(f" Top-1 Engine Fidelity  |      {actual_match_rate:5.1f}%       |       {peer_match_rate:5.1f}%")
    print(f" Weighted Avg Loss(ACPL)|      {actual_acpl:5.1f}        |       {peer_acpl:5.1f}")
    print("-" * 60)
    
    match_delta = actual_match_rate - peer_match_rate
    acpl_delta = peer_acpl - actual_acpl
    
    print("\nEMPIRICAL DIAGNOSTIC:")
    print(f" • Match Rate Delta : {match_delta:+.1f}% vs direct peer sample")
    print(f" • Precision Delta  : {acpl_delta:+.1f} ACPL vs direct peer sample")

    # Z-Score Computation
    z_stats = compute_z_statistics(target_cp_all, peer_cp_all, target_match_all, peer_match_all)
    
    print("-" * 60)
    print(" STATISTICAL Z-SCORE ANALYSIS (Welch's Empirical Two-Sample Test)")
    print("-" * 60)
    if z_stats:
        z_comp = z_stats["z_composite"]
        p_val = z_stats["p_value"]
        
        print(f" • Top-1 Match Z-Score    : {z_stats['z_match']:+.2f}  (SE: {z_stats['se_match']*100:.2f}%)")
        print(f" • ACPL Precision Z-Score : {z_stats['z_acpl']:+.2f}  (SE: {z_stats['se_acpl']:.2f} CP)")
        print(f" • Combined Z-Score       : {z_comp:+.2f}")
        print(f" • P-Value (One-Tailed)   : {p_val:.4f}")
        
        print("\nSTATISTICAL EVALUATION:")
        if z_comp < 2.0:
            print(" [✓] NORMAL VARIANCE (Z < +2.0): Play style sits completely within expectable human variation.")
        elif 2.0 <= z_comp < 3.0:
            print(" [!] OUTLIER PERFORMANCE (+2.0 <= Z < +3.0): Player significantly outperformed peers (95%+ confidence).")
        elif 3.0 <= z_comp < 5.0:
            print(" [⚠] EXTREME OUTLIER (+3.0 <= Z < +5.0): Performance exceeds 99.7% of expected human peer variance.")
        else:
            print(" [🔥] ANOMALOUS OVERPERFORMANCE (Z >= +5.0): Performance is statistically incompatible with peer baseline.")
    else:
        print(f" [!] INSUFFICIENT DECISION VOLUME FOR Z-TEST")
        print(f"     • Target Player Decisions : {t_decisions} moves (Required: {MIN_PEER_DECISIONS}+)")
        print(f"     • Peer Baseline Decisions : {o_decisions} moves (Required: {MIN_PEER_DECISIONS}+)")
    print("=" * 60)

if __name__ == "__main__":
    main()
