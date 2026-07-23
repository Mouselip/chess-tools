#!/usr/bin/env python3
"""
chess-perf-eval.py
A terminal-native performance comparator tool that analyzes player games against 
local Stockfish engine baselines using direct opponent harvesting, filtered ACPL stats,
global baseline fallback generation, and empirical Z-score statistical diagnostics.
"""

import io
import os
import sys
import json
import math
import shutil
import random
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
import chess
import chess.engine
import chess.pgn

# System Configuration
# Always write and check config in the CURRENT WORKING DIRECTORY where the command is run
CONFIG_FILE = os.path.join(os.getcwd(), "engine_config.json")

ANALYSIS_DEPTH = 18           # High precision target depth
ANALYSIS_TIME_LIMIT = 5.0     # Hard time limit per move evaluation (seconds)

# ACPL Filtering Thresholds
EVAL_CAP_CENTIPAWNS = 400     # Ignore positions evaluated beyond +/- 4.0 pawns (decided games/endgames)
MAX_SINGLE_MOVE_LOSS = 200    # Cap single-move loss spikes to prevent 1 blunder from ruining game ACPL

# Baseline Harvesting Limits
RATING_WINDOW = 150           # Initial direct peer search window (+/- 150 ELO)
MIN_PEER_DECISIONS = 200      # Strictly enforced minimum decision moves required for statistical validity

# Network Configuration
MAX_RETRIES = 5
USER_AGENT = "ChessPerfEval/1.0 (Contact: local_script_user)"

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
        backoff_factor=1.5,  # Backoff delays: 1.5s, 3s, 6s, 12s, 24s
        status_forcelist=[429, 500, 502, 503, 504],
        raise_on_status=False
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({"User-Agent": USER_AGENT})
    return session

# Global thread-safe robust session
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
    """Saves Stockfish path to engine_config.json in the current working directory."""
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
    """
    4-Tier Terminal Resolution Strategy:
    1. Saved config in current working directory (engine_config.json)
    2. Environment variable (STOCKFISH_PATH)
    3. Auto-detection ($PATH, current folder, standard Linux/Mac paths)
    4. Terminal prompt fallback
    """
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

def fetch_chesscom_games(username, speed_class, num_games=25):
    """Fetches PGNs from Chess.com traversing monthly archives."""
    archives_url = f"https://api.chess.com/pub/player/{username}/games/archives"
    
    print(f"\nFetching up to {num_games} {speed_class.upper()} games from Chess.com archives for '{username}'...")
    try:
        res = HTTP_SESSION.get(archives_url, timeout=10)
        if res.status_code != 200:
            print(f"Error fetching Chess.com archives for '{username}'")
            return []
            
        archives = res.json().get("archives", [])
        if not archives:
            return []

        matched_games = []
        for archive_url in reversed(archives):
            game_res = HTTP_SESSION.get(archive_url, timeout=10)
            if game_res.status_code != 200:
                continue
                
            data = game_res.json().get("games", [])
            for g in reversed(data):
                tc_class = g.get("time_class", "")
                
                if tc_class == speed_class:
                    pgn_io = io.StringIO(g.get("pgn", ""))
                    game = chess.pgn.read_game(pgn_io)
                    if game:
                        matched_games.append(game)
                        if len(matched_games) >= num_games:
                            return matched_games
                            
        return matched_games
    except requests.RequestException as e:
        print(f"Network error fetching games: {e}")
        return []

def fetch_lichess_games(username, speed_class, num_games=25):
    """Fetches PGNs from Lichess filtering server-side by perfType."""
    url = f"https://lichess.org/api/games/user/{username}?max={num_games}&perfType={speed_class}"
    
    print(f"\nFetching last {num_games} {speed_class.upper()} games from Lichess for '{username}'...")
    try:
        response = HTTP_SESSION.get(url, headers={"Accept": "application/x-chess-pgn"}, timeout=10)
        if response.status_code != 200:
            print(f"Error fetching Lichess games: HTTP {response.status_code}")
            return []
            
        games = []
        pgn_text = io.StringIO(response.text)
        while len(games) < num_games:
            game = chess.pgn.read_game(pgn_text)
            if game is None:
                break
            games.append(game)
        return games
    except requests.RequestException as e:
        print(f"Network error fetching games: {e}")
        return []

# -----------------------------------------------------------------------------
# Secondary Baseline Fallback Generator (Global Peer Pool)
# -----------------------------------------------------------------------------
def fetch_global_peer_games(platform_key, target_elo, speed_class, exclude_set, required_games=25):
    """
    Fallback mechanism when direct opponent harvesting yields insufficient baseline data.
    Queries active public players sitting within target_elo +/- 200 to extract games.
    """
    print(f"\n[!] Direct opponent harvest below minimum threshold ({MIN_PEER_DECISIONS} moves).")
    print(f"[*] Initializing Global Peer Fallback Generator...")
    print(f"[*] Targeting active rating bracket: ~{target_elo} ELO on {platform_key.capitalize()} ({speed_class.upper()})...")

    fallback_games = []
    harvested_usernames = set()

    if platform_key == "chesscom":
        leaderboard_url = "https://api.chess.com/pub/leaderboards"
        try:
            res = HTTP_SESSION.get(leaderboard_url, timeout=10)
            candidate_pool = []
            if res.status_code == 200:
                data = res.json()
                for player in data.get("live_rapid", []) + data.get("live_blitz", []):
                    uname = player.get("username", "")
                    if uname.lower() not in exclude_set:
                        candidate_pool.append(uname)
            
            random.shuffle(candidate_pool)
            for user in candidate_pool:
                if len(fallback_games) >= required_games:
                    break
                
                stats_url = f"https://api.chess.com/pub/player/{user}/stats"
                st_res = HTTP_SESSION.get(stats_url, timeout=6)
                if st_res.status_code == 200:
                    st_data = st_res.json()
                    tc_key = f"chess_{speed_class}"
                    last_rating = st_data.get(tc_key, {}).get("last", {}).get("rating", 0)
                    
                    if abs(last_rating - target_elo) <= 200:
                        u_games = fetch_chesscom_games(user, speed_class, num_games=3)
                        for g in u_games:
                            fallback_games.append((user, g))
                            harvested_usernames.add(user.lower())
                            if len(fallback_games) >= required_games:
                                break
        except Exception as e:
            print(f"[!] Error fetching fallback global pool: {e}")

    else:  # Lichess
        url = f"https://lichess.org/api/tv/channels"
        try:
            res = HTTP_SESSION.get(url, timeout=10)
            if res.status_code == 200:
                data = res.json()
                for channel, details in data.items():
                    user = details.get("user", {}).get("name", "")
                    if user and user.lower() not in exclude_set:
                        u_games = fetch_lichess_games(user, speed_class, num_games=3)
                        for g in u_games:
                            fallback_games.append((user, g))
                            harvested_usernames.add(user.lower())
                            if len(fallback_games) >= required_games:
                                break
        except Exception as e:
            print(f"[!] Error fetching Lichess fallback pool: {e}")

    return fallback_games, harvested_usernames

# -----------------------------------------------------------------------------
# Dual Player Game Analysis Engine (Target + Opponent Harvesting)
# -----------------------------------------------------------------------------
def analyze_game_and_harvest(game, target_user, engine):
    """
    Analyzes critical non-forced decisions for BOTH the target player and their opponent.
    Filters out opening book moves, forced moves, lopsided/decided positions (>4.0 CP),
    and caps single-move loss spikes at 200 CP.
    Returns per-decision decision vectors for exact variance and Z-score processing.
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

        # 1. Skip opening book (first 10 full moves / 20 half-moves)
        if move_count <= 20:
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
        
        # 4. Filter out decided/lopsided positions (>4.0 pawns eval advantage or disadvantage)
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

def erfc(x):
    """Approximation of complementary error function for p-value calculation."""
    # Abramowitz and Stegun formula 7.1.26
    a1, a2, a3, a4, a5 = 0.0705230784, 0.0422820123, 0.0092705272, 0.0001520143, 0.0002765672, 0.0000430638
    t = 1.0 / (1.0 + a1 * x + a2 * (x**2) + a3 * (x**3) + a4 * (x**4) + a5 * (x**5) + 0.0000430638 * (x**6))
    return t ** 16

def z_to_p_value(z_score):
    """Converts a positive Z-score to a one-tailed p-value."""
    if z_score <= 0:
        return 0.5
    # Standard normal cumulative distribution approximation
    return 0.5 * math.erfc(z_score / math.sqrt(2))

def compute_z_statistics(target_cp, peer_cp, target_match, peer_match):
    """
    Computes two-sample Z-scores and standard errors comparing target vs peer pool.
    Positive Z = Target outperformed peer baseline.
    """
    n_t = len(target_cp)
    n_p = len(peer_cp)
    
    # Strictly require 200+ decisions on BOTH sides for Z-test computation
    if n_t < MIN_PEER_DECISIONS or n_p < MIN_PEER_DECISIONS:
        return None

    # 1. ACPL Precision Z-Score
    mean_acpl_t = sum(target_cp) / n_t
    mean_acpl_p = sum(peer_cp) / n_p
    var_acpl_t = calculate_sample_variance(target_cp)
    var_acpl_p = calculate_sample_variance(peer_cp)
    
    se_acpl = math.sqrt((var_acpl_t / n_t) + (var_acpl_p / n_p))
    z_acpl = (mean_acpl_p - mean_acpl_t) / se_acpl if se_acpl > 0 else 0.0

    # 2. Top-1 Match Rate Z-Score (Two-proportion / Binomial model)
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
# Interactive Prompts with Strict Error Checking
# -----------------------------------------------------------------------------
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
    """Prompts for time control with strict option validation."""
    print("\nSelect Primary Time Control to evaluate:")
    print(" 1. Blitz (Default)")
    print(" 2. Rapid")
    print(" 3. Bullet")
    print(" 4. Daily / Classical")
    
    tc_map = {
        "1": "blitz",
        "2": "rapid",
        "3": "bullet",
        "4": "daily" if platform_key == "chesscom" else "classical"
    }
    
    while True:
        tc_input = input("Choice [Default: 1]: ").strip()
        if tc_input == "":
            return "blitz"
        if tc_input in tc_map:
            return tc_map[tc_input]
        print("[X] Invalid choice. Please enter a number between 1 and 4.")

def prompt_sample_size():
    """Prompts for sample size strictly bounded between 25 and 100."""
    print("\nSample Size Selection:")
    print(" Allowed range: 25 to 100 games (e.g., 25 standard, 50 robust, 100 deep audit)")
    
    while True:
        raw_count = input("Number of games to analyze [Default: 25]: ").strip()
        if raw_count == "":
            return 25
        try:
            val = int(raw_count)
            if 25 <= val <= 100:
                return val
            else:
                print("[X] Please enter a sample size between 25 and 100.")
        except ValueError:
            print("[X] Invalid input. Please enter a valid whole number.")

# -----------------------------------------------------------------------------
# Main Execution & Reporting
# -----------------------------------------------------------------------------
def main():
    print("==================================================")
    print(" CHESS PERFORMANCE EVALUATOR (chess-perf-eval.py)")
    print("==================================================\n")

    # Resolve Engine Path
    stockfish_bin = find_stockfish()

    # Validated Interactive Inputs
    platform_key = prompt_platform()
    username = prompt_username(platform_key)
    speed_class = prompt_time_control(platform_key)
    num_games = prompt_sample_size()

    # Fetch Games
    if platform_key == "chesscom":
        games = fetch_chesscom_games(username, speed_class, num_games=num_games)
    else:
        games = fetch_lichess_games(username, speed_class, num_games=num_games)

    if not games:
        print(f"\n[!] No matching {speed_class.upper()} games retrieved for '{username}'. Exiting.")
        return

    # Initialize Engine
    try:
        engine = chess.engine.SimpleEngine.popen_uci(stockfish_bin)
    except Exception as e:
        print(f"\nError initializing Stockfish binary at '{stockfish_bin}': {e}")
        if os.path.exists(CONFIG_FILE):
            print(f"Tip: Delete local '{CONFIG_FILE}' to reset the saved path.")
        return

    print(f"\nEvaluating target games & harvesting opponent performance (Depth={ANALYSIS_DEPTH}, Max Time={ANALYSIS_TIME_LIMIT}s/move)...")
    print(f"Filters Active: Ignoring positions > |{EVAL_CAP_CENTIPAWNS/100:.1f}| CP | Bounding single-move loss at {MAX_SINGLE_MOVE_LOSS} CP.\n")

    t_decisions, t_matches, t_cp_loss = 0, 0, 0
    o_decisions, o_matches, o_cp_loss = 0, 0, 0
    t_ratings, o_ratings = [], []
    
    # Decision arrays for variance & Z-score calculation
    target_cp_all, peer_cp_all = [], []
    target_match_all, peer_match_all = [], []
    
    harvested_opponents = set()
    target_clean = username.strip().lower()
    harvested_opponents.add(target_clean)

    for idx, game in enumerate(games, start=1):
        target_res, opp_res = analyze_game_and_harvest(game, username, engine)
        if target_res:
            t_ratings.append(target_res["rating"])
            t_decisions += target_res["decisions"]
            t_matches += target_res["matches"]
            t_cp_loss += target_res["cp_loss"]
            target_cp_all.extend(target_res["cp_list"])
            target_match_all.extend(target_res["match_list"])
            
            g_match = (target_res["matches"] / target_res["decisions"]) * 100
            g_acpl = target_res["cp_loss"] / target_res["decisions"]
            
            opp_str = f"vs {opp_res['name']} ({opp_res['rating']})" if opp_res else "vs Opponent"
            print(f"[{idx:02d}/{len(games)}] {opp_str} | Decisions: {target_res['decisions']:2d} | Top-1: {g_match:5.1f}% | ACPL: {g_acpl:5.1f}")
            
            # HARVEST OPPONENT DATA WITH EXPLICIT CHECKS & CLEAR REJECTION LOGGING
            if opp_res:
                opp_name = opp_res["name"]
                opp_clean = opp_name.strip().lower()
                opp_rating = opp_res["rating"]
                target_rating = target_res["rating"]

                # 1. Deduplication Check
                if opp_clean in harvested_opponents:
                    print(f"       └──> [PEER SKIPPED] Opponent '{opp_name}' is a duplicate (already in baseline).")

                # 2. Rating Window Check (+/- 150 ELO)
                elif abs(opp_rating - target_rating) > RATING_WINDOW:
                    print(f"       └──> [PEER SKIPPED] Opponent '{opp_name}' ({opp_rating}) outside rating window (+/- {RATING_WINDOW}).")

                # 3. Account Status Check (Fair Play / TOS bans)
                else:
                    active, status_reason = is_account_active(platform_key, opp_name)
                    if not active:
                        print(f"       └──> [PEER EXCLUDED] Opponent '{opp_name}' - {status_reason}")
                    else:
                        harvested_opponents.add(opp_clean)
                        o_ratings.append(opp_rating)
                        o_decisions += opp_res["decisions"]
                        o_matches += opp_res["matches"]
                        o_cp_loss += opp_res["cp_loss"]
                        peer_cp_all.extend(opp_res["cp_list"])
                        peer_match_all.extend(opp_res["match_list"])
                        print(f"       └──> [PEER HARVESTED] Opponent '{opp_name}' ({opp_rating}) added to peer baseline!")

    # SECONDARY FALLBACK TRIGGER: If direct peer decisions < MIN_PEER_DECISIONS (200 moves)
    if o_decisions < MIN_PEER_DECISIONS and len(t_ratings) > 0:
        avg_target_elo = int(sum(t_ratings) / len(t_ratings))
        
        # Lock target user into exclusion set before pulling global pool
        harvested_opponents.add(target_clean)
        
        fallback_games, fallback_users = fetch_global_peer_games(
            platform_key, avg_target_elo, speed_class, harvested_opponents, required_games=25
        )
        
        for p_user, f_game in fallback_games:
            p_clean = p_user.strip().lower()
            
            # PREVENT DUPLICATES & SELF-HARVESTING
            if p_clean in harvested_opponents:
                continue
                
            p_target_res, p_opp_res = analyze_game_and_harvest(f_game, p_user, engine)
            if p_target_res:
                harvested_opponents.add(p_clean)  # Lock candidate immediately to prevent duplicate game logs
                o_ratings.append(p_target_res["rating"])
                o_decisions += p_target_res["decisions"]
                o_matches += p_target_res["matches"]
                o_cp_loss += p_target_res["cp_loss"]
                peer_cp_all.extend(p_target_res["cp_list"])
                peer_match_all.extend(p_target_res["match_list"])
                print(f"       └──> [GLOBAL PEER HARVESTED] Active rating-matched peer '{p_user}' ({p_target_res['rating']}) injected into baseline!")

    engine.quit()

    if t_decisions == 0:
        print("\nNo critical non-forced moves found to analyze.")
        return

    # Metrics Summary
    avg_target_rating = int(sum(t_ratings) / len(t_ratings))
    actual_match_rate = (t_matches / t_decisions) * 100
    actual_acpl = t_cp_loss / t_decisions

    peer_match_rate = (o_matches / o_decisions) * 100 if o_decisions > 0 else 0.0
    peer_acpl = o_cp_loss / o_decisions if o_decisions > 0 else 0.0
    
    min_peer_r = min(o_ratings) if o_ratings else avg_target_rating - RATING_WINDOW
    max_peer_r = max(o_ratings) if o_ratings else avg_target_rating + RATING_WINDOW

    # Adjust peer dataset count display to exclude target player
    display_peer_count = len(harvested_opponents - {target_clean})

    print("\n" + "=" * 60)
    print(f" EMPIRICAL EVALUATION REPORT: {username}")
    print("=" * 60)
    print(f" Platform / Time Control : {platform_key.capitalize()} ({speed_class.upper()})")
    print(f" Target Player Games     : {len(t_ratings)} games / {t_decisions} decisions")
    print(f" Peer Dataset Size       : {display_peer_count} active opponents / {o_decisions} decisions")
    print(f" Peer Rating Window      : [{min_peer_r} to {max_peer_r}]")
    print("-" * 60)
    print(" PERFORMANCE METRICS      |  TARGET PLAYER  | REAL PEER BASELINE")
    print("-" * 60)
    print(f" Top-1 Engine Fidelity   |      {actual_match_rate:5.1f}%       |       {peer_match_rate:5.1f}%")
    print(f" Weighted Avg Loss (ACPL)|      {actual_acpl:5.1f}        |       {peer_acpl:5.1f}")
    print("-" * 60)
    
    # Delta Summary
    match_delta = actual_match_rate - peer_match_rate
    acpl_delta = peer_acpl - actual_acpl  # Positive means lower ACPL than expected (better precision)
    
    print("\nEMPIRICAL DIAGNOSTIC:")
    print(f" • Match Rate Delta : {match_delta:+.1f}% vs direct peer sample")
    print(f" • Precision Delta  : {acpl_delta:+.1f} ACPL vs direct peer sample")

    # Z-Score Computation Guardrail
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
        
        if num_games < 100:
            print(f"\n [DIAGNOSTIC]: The requested sample of {num_games} games yielded under {MIN_PEER_DECISIONS} non-forced decisions.")
            print(f"              Consider selecting a larger sample size (e.g., 50 or 100 games).")
        else:
            print(f"\n [DIAGNOSTIC]: The maximum sample size of {num_games} games yielded under {MIN_PEER_DECISIONS} non-forced decisions.")
            print(f"              Insufficient move volume available in this game set to perform a valid Z-test.")
    print("=" * 60)

if __name__ == "__main__":
    main()
