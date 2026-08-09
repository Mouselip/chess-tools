#!/usr/bin/env python3
"""
chesscom-perf-eval.py
A terminal-native performance comparator tool that analyzes Chess.com player games 
against local Stockfish engine baselines using direct opponent harvesting, filtered ACPL stats,
non-parametric Mann-Whitney U diagnostics, and two-sample proportion tests.

Usage Options:
  chesscom-perf-eval.py <playername> <bullet|blitz|rapid|daily> [options]

Positional Parameters:
  playername                                    Target player username on Chess.com.
  {bullet,blitz,rapid,daily}                    Speed category.

Options:
  -o, --override-window                         Expand peer rating window from +/-150 to +/-250 Elo.
  -960, --chess960                              Enable Chess960 (Fischer Random) mode.
  --tournaments [N]                             Audit up to N recent tournaments in category (Default: 5).
  --log                                         Enable logging to a timestamped file in cwd.
  -v, --version                                 Show program's version number and exit.
  -h, --help                                    Show the help screen and exit.
"""

import io
import os
import sys
import json
import math
import shutil
import argparse
from datetime import datetime
from collections import Counter
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
import scipy.stats as stats
import chess
import chess.engine
import chess.pgn

# Version Metadata
__version__ = "0.0.3"

# System Configuration
CONFIG_FILE = os.path.join(os.getcwd(), "engine_config.json")

ANALYSIS_DEPTH = 18           # High precision target depth
ANALYSIS_TIME_LIMIT = 5.0     # Hard time limit per move evaluation (seconds)

# Opening Cutoffs
STANDARD_OPENING_BOOK_PLIES = 20  # Skip first 10 full moves for standard chess
CHESS960_OPENING_BOOK_PLIES = 0   # No opening book skip for Chess960

# ACPL Filtering Thresholds
EVAL_CAP_CENTIPAWNS = 400     # Ignore positions evaluated beyond +/- 400 CP (4.0 pawns)
MAX_SINGLE_MOVE_LOSS = 200    # Cap single-move loss spikes to prevent 1 blunder from ruining game ACPL

# Decision Volume Bounds
TARGET_MAX_DECISIONS = 400    # Aim for up to 400 critical middlegame decisions
MIN_PEER_DECISIONS = 200      # Minimum floor required for statistical validity

# Baseline Harvesting Limits
DEFAULT_RATING_WINDOW = 150   # Default direct peer search window (+/- 150 Elo)
OVERRIDE_RATING_WINDOW = 250  # Expanded search window (+/- 250 Elo)

# Network Configuration
MAX_RETRIES = 5
USER_AGENT = f"ChessPerfEval/{__version__} (Contact: local_script_user)"


def stream_write(text, log_file=None, end="\n"):
    """Outputs text simultaneously to stdout and to an open log file if present."""
    print(text, end=end)
    if log_file:
        log_file.write(text + end)
        log_file.flush()


# -----------------------------------------------------------------------------
# Robust HTTP Session Management
# -----------------------------------------------------------------------------
def get_robust_session():
    session = requests.Session()
    retries = Retry(
        total=MAX_RETRIES,
        backoff_factor=1.5,
        status_forcelist=[429, 500, 502, 503, 504],
        raise_on_status=False,
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
    print("\n" + "=" * 60)
    print("[!] Stockfish executable was not found automatically.")
    print("=" * 60)

    while True:
        user_input = input("Enter full path to Stockfish binary (or 'q' to quit): ").strip()
        user_input = user_input.strip("'\"")

        if user_input.lower() == "q":
            print("Exiting.")
            sys.exit(1)

        expanded_path = os.path.expanduser(user_input)
        if os.path.isfile(expanded_path):
            print(f"\nAccepted binary path: '{expanded_path}'")
            while True:
                save_choice = (
                    input("Save this path to 'engine_config.json' in current directory? (y/n): ")
                    .strip()
                    .lower()
                )
                if save_choice in ["y", "yes"]:
                    save_engine_path(expanded_path)
                    break
                elif save_choice in ["n", "no"]:
                    print("[+] Path accepted for this session only.\n")
                    break
                else:
                    print("[X] Invalid entry. Please enter 'y' or 'n'.")

            return expanded_path
        else:
            print(f"[X] Invalid file path: '{user_input}'. Please try again.\n")


def find_stockfish():
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
        "/opt/homebrew/bin",
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
# API Fetchers & Validation (Chess.com Only)
# -----------------------------------------------------------------------------
def validate_username(username):
    url = f"https://api.chess.com/pub/player/{username}"
    try:
        res = HTTP_SESSION.get(url, timeout=8)
        if res.status_code == 200:
            return True, None
        elif res.status_code == 404:
            return False, f"Player '{username}' not found on Chess.com."
        else:
            return False, f"API check failed with status code {res.status_code}."
    except requests.RequestException as e:
        return False, f"Network error validating user: {e}"


def is_account_active(username):
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


# -----------------------------------------------------------------------------
# Tournament & Time Control Fetchers
# -----------------------------------------------------------------------------
def fetch_recent_tournaments(username, max_count=5):
    print(f"\nFetching tournament history for user '{username}'...")
    url = f"https://api.chess.com/pub/player/{username}/tournaments"
    matching_tournaments = []

    try:
        res = HTTP_SESSION.get(url, timeout=10)
        if res.status_code == 200:
            data = res.json()
            finished = data.get("finished", [])
            for t in reversed(finished):
                t_url = t.get("url", "")
                t_id = t_url.split("/")[-1] if t_url else ""
                if t_id:
                    matching_tournaments.append(
                        {"id": t_id, "name": t.get("name", t_id), "url": t_url}
                    )
                    if len(matching_tournaments) >= max_count:
                        break
    except requests.RequestException as e:
        print(f"Error fetching tournament history: {e}")

    return matching_tournaments


def find_most_frequent_time_control(username, speed_class, is_c960=False):
    v_label = "CHESS960" if is_c960 else "STANDARD"
    print(
        f"\nScanning recent archives for {v_label} category '{speed_class.upper()}' to detect primary time control..."
    )
    tc_counter = Counter()

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
                    if not g.get("rated", False):
                        continue

                    if g.get("tournament") or "tournament" in g.get("url", "").lower():
                        continue

                    rules = g.get("rules", "").lower()
                    if is_c960 and rules != "chess960":
                        continue
                    elif not is_c960 and rules == "chess960":
                        continue

                    if g.get("time_class", "").lower() == speed_class.lower():
                        raw_tc = g.get("time_control", "")
                        if raw_tc:
                            tc_counter[raw_tc] += 1
                if sum(tc_counter.values()) >= 50:
                    break
    except requests.RequestException as e:
        print(f"Error auto-detecting time control: {e}")

    if tc_counter:
        top_tc, count = tc_counter.most_common(1)[0]
        print(
            f"[+] Primary rated time control detected in '{speed_class.upper()}': '{top_tc}' ({count} recent games)."
        )
        return top_tc

    return None


def is_tournament_game(game_dict, pgn_game):
    if game_dict.get("tournament") or "tournament" in game_dict.get("url", "").lower():
        return True

    if pgn_game and pgn_game.headers:
        event = pgn_game.headers.get("Event", "").lower()
        if "tournament" in event or "arena" in event or "swiss" in event:
            return True

    return False


# -----------------------------------------------------------------------------
# Game Harvesting Streamer
# -----------------------------------------------------------------------------
def stream_games(
    username,
    speed_class,
    exact_tc=None,
    is_c960=False,
    tournaments=None,
    current_decisions_callback=None,
):
    if tournaments:
        print(
            f"\n[+] Tournament Mode Active: Streaming games across up to {len(tournaments)} matched events..."
        )
        for tour in tournaments:
            if (
                current_decisions_callback
                and current_decisions_callback() >= TARGET_MAX_DECISIONS
            ):
                print(
                    f"[+] Decision target reached ({current_decisions_callback()}/{TARGET_MAX_DECISIONS}). Halting further tournament fetches."
                )
                break

            tour_id = tour["id"]
            tour_name = tour["name"]
            print(f"\n -> Harvesting Tournament: {tour_name} ({tour_id})")

            tour_url = f"https://api.chess.com/pub/tournament/{tour_id}"
            try:
                res = HTTP_SESSION.get(tour_url, timeout=10)
                if res.status_code != 200:
                    continue
                tour_data = res.json()
                rounds = tour_data.get("rounds", [])

                for r_url in rounds:
                    r_res = HTTP_SESSION.get(r_url, timeout=10)
                    if r_res.status_code != 200:
                        continue

                    groups = r_res.json().get("groups", [])
                    for g_url in groups:
                        g_res = HTTP_SESSION.get(g_url, timeout=10)
                        if g_res.status_code != 200:
                            continue

                        games_list = g_res.json().get("games", [])
                        for g in reversed(games_list):
                            w_player = g.get("white", {}).get("username", "").lower()
                            b_player = g.get("black", {}).get("username", "").lower()
                            user_clean = username.lower()

                            if user_clean in (w_player, b_player):
                                pgn_text = g.get("pgn", "")
                                if pgn_text:
                                    pgn_io = io.StringIO(pgn_text)
                                    game = chess.pgn.read_game(pgn_io)
                                    if game:
                                        game.headers["Tournament_Name"] = tour_name
                                        yield game
            except requests.RequestException as e:
                print(f"   [!] Error streaming tournament {tour_id}: {e}")
                continue
    else:
        archives_url = f"https://api.chess.com/pub/player/{username}/games/archives"
        try:
            res = HTTP_SESSION.get(archives_url, timeout=10)
            if res.status_code != 200:
                return
            archives = res.json().get("archives", [])
            for archive_url in reversed(archives):
                if (
                    current_decisions_callback
                    and current_decisions_callback() >= TARGET_MAX_DECISIONS
                ):
                    break

                game_res = HTTP_SESSION.get(archive_url, timeout=10)
                if game_res.status_code != 200:
                    continue
                data = game_res.json().get("games", [])
                for g in reversed(data):
                    if (
                        current_decisions_callback
                        and current_decisions_callback() >= TARGET_MAX_DECISIONS
                    ):
                        break

                    if not g.get("rated", False):
                        continue

                    rules = g.get("rules", "").lower()
                    if is_c960 and rules != "chess960":
                        continue
                    elif not is_c960 and rules == "chess960":
                        continue

                    tc_class = g.get("time_class", "")
                    tc_control = g.get("time_control", "")
                    if tc_class.lower() == speed_class.lower():
                        if exact_tc and tc_control != exact_tc:
                            continue

                        pgn_io = io.StringIO(g.get("pgn", ""))
                        game = chess.pgn.read_game(pgn_io)

                        if is_tournament_game(g, game):
                            continue

                        if game:
                            yield game
        except requests.RequestException as e:
            print(f"Network error streaming games: {e}")
            return


# -----------------------------------------------------------------------------
# Game Analysis Engine
# -----------------------------------------------------------------------------
def is_endgame_phase(board):
    has_white_queen = bool(board.pieces(chess.QUEEN, chess.WHITE))
    has_black_queen = bool(board.pieces(chess.QUEEN, chess.BLACK))

    non_pawn_pieces = (
        len(board.pieces(chess.KNIGHT, chess.WHITE))
        + len(board.pieces(chess.KNIGHT, chess.BLACK))
        + len(board.pieces(chess.BISHOP, chess.WHITE))
        + len(board.pieces(chess.BISHOP, chess.BLACK))
        + len(board.pieces(chess.ROOK, chess.WHITE))
        + len(board.pieces(chess.ROOK, chess.BLACK))
        + len(board.pieces(chess.QUEEN, chess.WHITE))
        + len(board.pieces(chess.QUEEN, chess.BLACK))
    )

    if not has_white_queen and not has_black_queen and non_pawn_pieces <= 8:
        return True

    if non_pawn_pieces <= 6:
        return True

    return False


def analyze_game_and_harvest(game, target_user, engine, opening_plies):
    headers = game.headers
    white_name = headers.get("White", "")
    black_name = headers.get("Black", "")

    white_lower = white_name.lower()
    user_lower = target_user.lower()

    if user_lower in white_lower:
        target_color = chess.WHITE
        opp_name = black_name
        try:
            target_rating = int(headers.get("WhiteElo", 1500))
            opp_rating = int(headers.get("BlackElo", 1500))
        except ValueError:
            target_rating, opp_rating = 1500, 1500
    else:
        target_color = chess.BLACK
        opp_name = white_name
        try:
            target_rating = int(headers.get("BlackElo", 1500))
            opp_rating = int(headers.get("WhiteElo", 1500))
        except ValueError:
            target_rating, opp_rating = 1500, 1500

    board = game.board()
    move_count = 0

    target_stats = {
        "rating": target_rating,
        "decisions": 0,
        "cp_loss": 0,
        "cp_list": [],
        "t1_matches": 0,
        "t2_only_matches": 0,
        "t3_only_matches": 0,
        "match1_list": [],
        "match2_only_list": [],
        "match3_only_list": [],
    }
    opp_stats = {
        "name": opp_name,
        "rating": opp_rating,
        "decisions": 0,
        "cp_loss": 0,
        "cp_list": [],
        "t1_matches": 0,
        "t2_only_matches": 0,
        "t3_only_matches": 0,
        "match1_list": [],
        "match2_only_list": [],
        "match3_only_list": [],
    }

    eval_limit = chess.engine.Limit(depth=ANALYSIS_DEPTH, time=ANALYSIS_TIME_LIMIT)

    for node in game.mainline():
        move = node.move
        current_turn = board.turn
        move_count += 1

        if move_count <= opening_plies:
            board.push(move)
            continue

        if is_endgame_phase(board):
            break

        if board.legal_moves.count() <= 1:
            board.push(move)
            continue

        analysis_pv_list = engine.analyse(board, eval_limit, multipv=3)
        if not analysis_pv_list:
            board.push(move)
            continue

        top_moves = []
        for pv_entry in analysis_pv_list:
            pv_line = pv_entry.get("pv", [])
            if pv_line:
                top_moves.append(pv_line[0])

        score_before = analysis_pv_list[0]["score"].pov(current_turn)

        if score_before.is_mate():
            board.push(move)
            continue

        cp_before = score_before.score(mate_score=10000)

        if abs(cp_before) > EVAL_CAP_CENTIPAWNS:
            board.push(move)
            continue

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

        is_t1 = 1 if (len(top_moves) >= 1 and move == top_moves[0]) else 0
        is_t2_only = 1 if (len(top_moves) >= 2 and move == top_moves[1]) else 0
        is_t3_only = 1 if (len(top_moves) >= 3 and move == top_moves[2]) else 0

        if is_t1:
            active_dict["t1_matches"] += 1
        elif is_t2_only:
            active_dict["t2_only_matches"] += 1
        elif is_t3_only:
            active_dict["t3_only_matches"] += 1

        active_dict["match1_list"].append(is_t1)
        active_dict["match2_only_list"].append(is_t2_only)
        active_dict["match3_only_list"].append(is_t3_only)

        board.push(move)

    res_target = target_stats if target_stats["decisions"] > 0 else None
    res_opp = opp_stats if opp_stats["decisions"] > 0 else None

    return res_target, res_opp


# -----------------------------------------------------------------------------
# Statistical Engine
# -----------------------------------------------------------------------------
def compute_statistical_diagnostics(
    target_cp,
    peer_cp,
    target_m1,
    peer_m1,
    target_m2_only,
    peer_m2_only,
    target_m3_only,
    peer_m3_only,
):
    n_t = len(target_cp)
    n_p = len(peer_cp)

    if n_t < MIN_PEER_DECISIONS or n_p < MIN_PEER_DECISIONS:
        return None

    mean_acpl_t = sum(target_cp) / n_t
    mean_acpl_p = sum(peer_cp) / n_p

    sorted_t = sorted(target_cp)
    sorted_p = sorted(peer_cp)
    median_cpl_t = (
        sorted_t[n_t // 2]
        if n_t % 2 != 0
        else (sorted_t[n_t // 2 - 1] + sorted_t[n_t // 2]) / 2.0
    )
    median_cpl_p = (
        sorted_p[n_p // 2]
        if n_p % 2 != 0
        else (sorted_p[n_p // 2 - 1] + sorted_p[n_p // 2]) / 2.0
    )

    u_stat_cpl, p_val_cpl = stats.mannwhitneyu(target_cp, peer_cp, alternative="two-sided")

    def two_prop_z_test(k1_list, n1, k2_list, n2):
        x1, x2 = sum(k1_list), sum(k2_list)
        p1, p2 = x1 / n1, x2 / n2
        p_pool = (x1 + x2) / (n1 + n2)
        se = math.sqrt(p_pool * (1.0 - p_pool) * (1.0 / n1 + 1.0 / n2))
        if se == 0:
            return 0.0, 1.0, p1, p2, 0.0
        z = (p1 - p2) / se
        p_val = 2.0 * (1.0 - 0.5 * (1.0 + math.erf(abs(z) / math.sqrt(2.0))))
        return z, p_val, p1, p2, se

    z_m1, p_m1, p_t1, p_p1, se_m1 = two_prop_z_test(target_m1, n_t, peer_m1, n_p)
    z_m2, p_m2, p_t2, p_p2, se_m2 = two_prop_z_test(target_m2_only, n_t, peer_m2_only, n_p)
    z_m3, p_m3, p_t3, p_p3, se_m3 = two_prop_z_test(target_m3_only, n_t, peer_m3_only, n_p)

    return {
        "n_target": n_t,
        "n_peer": n_p,
        "mean_acpl_t": mean_acpl_t,
        "mean_acpl_p": mean_acpl_p,
        "median_cpl_t": median_cpl_t,
        "median_cpl_p": median_cpl_p,
        "u_stat_cpl": u_stat_cpl,
        "p_val_cpl": p_val_cpl,
        "z_m1": z_m1,
        "p_m1": p_m1,
        "se_m1": se_m1,
        "z_m2": z_m2,
        "p_m2": p_m2,
        "se_m2": se_m2,
        "z_m3": z_m3,
        "p_m3": p_m3,
        "se_m3": se_m3,
    }


# -----------------------------------------------------------------------------
# Main Execution & Reporting
# -----------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Chess.com Performance Evaluator - Evaluates middlegame precision, ACPL, and candidate move correlation against local Stockfish engine baselines.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python chesscom-perf-eval.py Hikaru blitz
  python chesscom-perf-eval.py GothamChess blitz -960 --tournaments 5
  python chesscom-perf-eval.py MagnusCarlsen rapid -o --log
""",
    )

    parser.add_argument(
        "playername",
        type=str,
        help="Target player username on Chess.com",
    )
    parser.add_argument(
        "time_control",
        type=str,
        choices=["bullet", "blitz", "rapid", "daily"],
        help="Speed category: bullet | blitz | rapid | daily",
    )

    parser.add_argument(
        "-o",
        "--override-window",
        action="store_true",
        help="Expand peer rating window from +/-150 to +/-250 Elo",
    )
    parser.add_argument(
        "-960",
        "--chess960",
        action="store_true",
        help="Enable Chess960 (Fischer Random) mode",
    )
    parser.add_argument(
        "--tournaments",
        nargs="?",
        const=5,
        type=int,
        default=None,
        help="Audit up to N recent tournaments matching category (Default: 5)",
    )
    parser.add_argument(
        "--log",
        action="store_true",
        help="Enable logging to a timestamped file in the current working directory",
    )
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    # Automatically print full help text if run with no command-line arguments
    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(1)

    args = parser.parse_args()

    # 1. Validate Username
    raw_user = args.playername.strip()
    print(f"Verifying username '{raw_user}' on Chess.com...")
    valid, err_msg = validate_username(raw_user)
    if not valid:
        print(f"[X] Error: {err_msg}", file=sys.stderr)
        sys.exit(1)
    username = raw_user
    print(f"[+] Username '{username}' verified successfully.")

    speed_class = args.time_control.strip().lower()
    rating_window_val = (
        OVERRIDE_RATING_WINDOW if args.override_window else DEFAULT_RATING_WINDOW
    )
    is_c960 = args.chess960
    opening_plies = (
        CHESS960_OPENING_BOOK_PLIES if is_c960 else STANDARD_OPENING_BOOK_PLIES
    )
    max_tournaments = args.tournaments
    is_tour = max_tournaments is not None

    # 2. Resolve Engine
    stockfish_bin = find_stockfish()
    try:
        engine = chess.engine.SimpleEngine.popen_uci(stockfish_bin)
        engine_name = engine.id.get("name", "Stockfish Engine")

        if is_c960:
            try:
                engine.configure({"UCI_Chess960": True})
                print("[+] Configured Stockfish with 'UCI_Chess960 = true'")
            except Exception:
                print(
                    "[+] Stockfish manages Chess960 automatically (UCI_Chess960 set automatically by engine)."
                )
    except Exception as e:
        print(f"\nError initializing Stockfish binary at '{stockfish_bin}': {e}", file=sys.stderr)
        if os.path.exists(CONFIG_FILE):
            print(f"Tip: Delete local '{CONFIG_FILE}' to reset the saved path.")
        sys.exit(1)

    # Tournament List Resolution
    tournaments_list = None
    if is_tour:
        tournaments_list = fetch_recent_tournaments(username, max_count=max_tournaments)
        if not tournaments_list:
            print(f"[!] No completed tournaments found for '{username}' via API.", file=sys.stderr)
            sys.exit(1)

    # 3. Detect Time Control (Skip if in Tournament Mode)
    exact_time_control = (
        None if is_tour else find_most_frequent_time_control(username, speed_class, is_c960=is_c960)
    )

    variant_str = "CHESS960 " if is_c960 else ""
    mode_str = "TOURNAMENTS " if is_tour else ""
    tc_label = f"{variant_str}{mode_str}{speed_class.upper()}"

    # Prepare Stream File Target
    log_file = None
    log_file_path = None
    clean_user = username.strip().lower()
    if args.log:
        timestamp_prefix = datetime.now().strftime("%y%m%d%H%M%S")
        log_file_path = os.path.join(os.getcwd(), f"{clean_user}-{timestamp_prefix}.log")
        try:
            log_file = open(log_file_path, "w", encoding="utf-8")
        except OSError as e:
            print(f"[!] Error opening log file: {e}", file=sys.stderr)

    # Write RUN HEADER at the top of log file and console
    stream_write("=" * 78, log_file)
    stream_write(f" CHESS.COM PERFORMANCE EVALUATION [v{__version__}]", log_file)
    stream_write("=" * 78, log_file)
    stream_write(f" Timestamp      : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", log_file)
    stream_write(f" Target Player  : {username}", log_file)
    stream_write(f" Engine Model   : {engine_name} (MultiPV 3)", log_file)
    stream_write(f" Target Volume  : Shoot for {TARGET_MAX_DECISIONS} decisions (Floor: {MIN_PEER_DECISIONS}) [{tc_label}]", log_file)
    stream_write(" Active Filters : Rated Games Only | Strict Paired Sampling", log_file)
    if is_tour:
        stream_write("                  Mode: Tournament Field (No Elo Cap)", log_file)
    else:
        stream_write(f"                  Mode: Standard Pool (+/-{rating_window_val} Elo)", log_file)
    stream_write(f"                  Opening Cut <= {opening_plies//2} moves | Eval Cap <= |400| CP", log_file)
    stream_write("                  Max Loss Bound = 200 CP", log_file)
    if args.log and log_file_path:
        stream_write(f" Logging Status : Enabled -> {os.path.basename(log_file_path)}", log_file)
    else:
        stream_write(" Logging Status : Disabled (pass --log to save log)", log_file)
    stream_write("-" * 78, log_file)
    stream_write("", log_file)

    t_decisions, t_cp_loss = 0, 0
    t_t1_matches, t_t2_only_matches, t_t3_only_matches = 0, 0, 0

    o_decisions, o_cp_loss = 0, 0
    o_t1_matches, o_t2_only_matches, o_t3_only_matches = 0, 0, 0

    t_ratings, o_ratings = [], []
    games_analyzed = 0

    target_cp_all, peer_cp_all = [], []
    target_m1_all, peer_m1_all = [], []
    target_m2_only_all, peer_m2_only_all = [], []
    target_m3_only_all, peer_m3_only_all = [], []

    harvested_opponents = set()
    harvested_opponents.add(clean_user)

    def get_current_decisions():
        return t_decisions

    # Print Progress Table Header
    header_title = (
        f"{'#':<4} {'Moves':>8} {'T1%':>5} {'T2%':>5} {'T3%':>5} {'T123%':>6} {'ACPL':>6}  Opponent"
    )
    header_line = "-" * 71
    stream_write(header_title, log_file)
    stream_write(header_line, log_file)

    # 4. Stream and Harvest Games
    game_stream = stream_games(
        username,
        speed_class,
        exact_tc=exact_time_control,
        is_c960=is_c960,
        tournaments=tournaments_list,
        current_decisions_callback=get_current_decisions,
    )

    for game in game_stream:
        target_res, opp_res = analyze_game_and_harvest(game, username, engine, opening_plies)

        if (
            target_res
            and target_res["decisions"] > 0
            and opp_res
            and opp_res["decisions"] > 0
        ):
            opp_name = opp_res["name"]
            opp_clean = opp_name.strip().lower()
            opp_rating = opp_res["rating"]
            target_rating = target_res["rating"]

            if opp_clean in harvested_opponents:
                stream_write(f"[SKIPPED] Opponent '{opp_name}' is a duplicate.", log_file)
                continue

            # Standard Pool Mode: Apply strict Elo window filtering
            if not is_tour and abs(opp_rating - target_rating) > rating_window_val:
                stream_write(
                    f"[SKIPPED] Opponent '{opp_name}' ({opp_rating}) outside rating window (+/- {rating_window_val}).",
                    log_file,
                )
                continue

            active, status_reason = is_account_active(opp_name)
            if not active:
                stream_write(f"[SKIPPED] Opponent '{opp_name}' - {status_reason}", log_file)
                continue

            # Harvest Data
            harvested_opponents.add(opp_clean)
            games_analyzed += 1

            # Target Player
            t_ratings.append(target_rating)
            t_decisions += target_res["decisions"]
            t_cp_loss += target_res["cp_loss"]
            target_cp_all.extend(target_res["cp_list"])

            t_t1_matches += target_res["t1_matches"]
            t_t2_only_matches += target_res["t2_only_matches"]
            t_t3_only_matches += target_res["t3_only_matches"]

            target_m1_all.extend(target_res["match1_list"])
            target_m2_only_all.extend(target_res["match2_only_list"])
            target_m3_only_all.extend(target_res["match3_only_list"])

            # Peer Opponent
            o_ratings.append(opp_rating)
            o_decisions += opp_res["decisions"]
            o_cp_loss += opp_res["cp_loss"]
            peer_cp_all.extend(opp_res["cp_list"])

            o_t1_matches += opp_res["t1_matches"]
            o_t2_only_matches += opp_res["t2_only_matches"]
            o_t3_only_matches += opp_res["t3_only_matches"]

            peer_m1_all.extend(opp_res["match1_list"])
            peer_m2_only_all.extend(opp_res["match2_only_list"])
            peer_m3_only_all.extend(opp_res["match3_only_list"])

            g_m1 = (target_res["t1_matches"] / target_res["decisions"]) * 100
            g_m2_only = (target_res["t2_only_matches"] / target_res["decisions"]) * 100
            g_m3_only = (target_res["t3_only_matches"] / target_res["decisions"]) * 100
            g_t123 = (
                (
                    target_res["t1_matches"]
                    + target_res["t2_only_matches"]
                    + target_res["t3_only_matches"]
                )
                / target_res["decisions"]
            ) * 100
            g_acpl = target_res["cp_loss"] / target_res["decisions"]

            tour_tag = (
                f" [{game.headers.get('Tournament_Name', '')}]" if is_tour else ""
            )
            progress_str = f"{t_decisions}/{TARGET_MAX_DECISIONS}"

            row_str = f"[{games_analyzed:02d}] {progress_str:>8} {g_m1:5.1f} {g_m2_only:5.1f} {g_m3_only:5.1f} {g_t123:6.1f} {g_acpl:6.1f}  vs {opp_name} ({opp_rating}){tour_tag}"
            stream_write(row_str, log_file)

    engine.quit()

    if t_decisions < MIN_PEER_DECISIONS:
        stream_write(
            f"\n[!] Could not gather minimum required decisions ({MIN_PEER_DECISIONS}). Total gathered: {t_decisions}. Exiting.",
            log_file,
        )
        if log_file:
            log_file.close()
        sys.exit(1)

    # Metrics Summary Calculation
    avg_target_rating = int(sum(t_ratings) / len(t_ratings))

    actual_m1_rate = (t_t1_matches / t_decisions) * 100
    actual_m2_rate = (t_t2_only_matches / t_decisions) * 100
    actual_m3_rate = (t_t3_only_matches / t_decisions) * 100
    actual_t123_rate = (
        (t_t1_matches + t_t2_only_matches + t_t3_only_matches) / t_decisions
    ) * 100
    actual_acpl = t_cp_loss / t_decisions

    peer_m1_rate = (o_t1_matches / o_decisions) * 100 if o_decisions > 0 else 0.0
    peer_m2_rate = (o_t2_only_matches / o_decisions) * 100 if o_decisions > 0 else 0.0
    peer_m3_rate = (o_t3_only_matches / o_decisions) * 100 if o_decisions > 0 else 0.0
    peer_t123_rate = (
        (o_t1_matches + o_t2_only_matches + o_t3_only_matches) / o_decisions
    ) * 100 if o_decisions > 0 else 0.0
    peer_acpl = o_cp_loss / o_decisions if o_decisions > 0 else 0.0

    # Percentages & Deltas
    m1_delta = actual_m1_rate - peer_m1_rate
    m2_delta = actual_m2_rate - peer_m2_rate
    m3_delta = actual_m3_rate - peer_m3_rate
    t123_delta = actual_t123_rate - peer_t123_rate
    acpl_delta = peer_acpl - actual_acpl

    m1_pct_rel = (m1_delta / peer_m1_rate * 100) if peer_m1_rate > 0 else 0.0
    t123_pct_rel = (t123_delta / peer_t123_rate * 100) if peer_t123_rate > 0 else 0.0
    acpl_efficiency_pct = (acpl_delta / peer_acpl * 100) if peer_acpl > 0 else 0.0

    min_peer_r = min(o_ratings) if o_ratings else avg_target_rating - rating_window_val
    max_peer_r = max(o_ratings) if o_ratings else avg_target_rating + rating_window_val
    display_peer_count = len(harvested_opponents - {clean_user})

    # Output Final Report Block (Clean Summary - No duplicate config repetition)
    stream_write("\n" + "=" * 78, log_file)
    stream_write(f" SUMMARY EVALUATION REPORT: {username} ({tc_label})", log_file)
    stream_write("=" * 78, log_file)
    stream_write(
        f" Specific Time Control  : {'Tournament Pool' if is_tour else exact_time_control if exact_time_control else 'All in Category'}",
        log_file,
    )
    stream_write(
        f" Target Decisions       : {t_decisions} moves across {games_analyzed} valid games",
        log_file,
    )
    stream_write(
        f" Peer Baseline Volume   : {o_decisions} moves across {display_peer_count} active opponents",
        log_file,
    )
    stream_write(f" Peer Rating Range      : [{min_peer_r} to {max_peer_r}] (Avg Target: {avg_target_rating})", log_file)
    stream_write("-" * 78, log_file)
    stream_write(
        " PERFORMANCE METRICS     |  TARGET PLAYER  | PEER BASELINE | ABS DELTA | REL CHANGE",
        log_file,
    )
    stream_write("-" * 78, log_file)
    stream_write(
        f" T1 Match Rate          |      {actual_m1_rate:5.1f}%       |     {peer_m1_rate:5.1f}%     |   {m1_delta:+5.1f}%   |  {m1_pct_rel:+6.1f}%",
        log_file,
    )
    stream_write(
        f" T2-Only Match Rate     |      {actual_m2_rate:5.1f}%       |     {peer_m2_rate:5.1f}%     |   {m2_delta:+5.1f}%   |      --",
        log_file,
    )
    stream_write(
        f" T3-Only Match Rate     |      {actual_m3_rate:5.1f}%       |     {peer_m3_rate:5.1f}%     |   {m3_delta:+5.1f}%   |      --",
        log_file,
    )
    stream_write(
        f" Cumulative T123 Rate   |      {actual_t123_rate:5.1f}%       |     {peer_t123_rate:5.1f}%     |   {t123_delta:+5.1f}%   |  {t123_pct_rel:+6.1f}%",
        log_file,
    )
    stream_write(
        f" Filtered ACPL (CP)     |      {actual_acpl:5.1f}        |     {peer_acpl:5.1f}      |  {acpl_delta:+5.1f} CP  |  {acpl_efficiency_pct:+6.1f}% (Precision)",
        log_file,
    )

    # Statistical Diagnostics
    stats_res = compute_statistical_diagnostics(
        target_cp_all,
        peer_cp_all,
        target_m1_all,
        peer_m1_all,
        target_m2_only_all,
        peer_m2_only_all,
        target_m3_only_all,
        peer_m3_only_all,
    )

    stream_write("-" * 78, log_file)
    stream_write(" STATISTICAL DIAGNOSTICS (Mann-Whitney U & Proportion Z-Tests)", log_file)
    stream_write("-" * 78, log_file)
    if stats_res:
        p_cpl = stats_res["p_val_cpl"]
        p_m1 = stats_res["p_m1"]
        z_m1 = stats_res["z_m1"]
        med_t = stats_res["median_cpl_t"]
        med_p = stats_res["median_cpl_p"]

        stream_write(
            f" • CPL Mann-Whitney U    : U = {stats_res['u_stat_cpl']:.1f} | p-value = {p_cpl:.4f}",
            log_file,
        )
        stream_write(
            f" • Median CPL (Target/Peer) : {med_t:.1f} CP vs {med_p:.1f} CP",
            log_file,
        )
        stream_write(
            f" • T1 Match Z-Score     : {z_m1:+.2f}  (p-value = {p_m1:.4f} | SE: {stats_res['se_m1']*100:.2f}%)",
            log_file,
        )
        stream_write(
            f" • T2-Only Z-Score      : {stats_res['z_m2']:+.2f}  (p-value = {stats_res['p_m2']:.4f})",
            log_file,
        )
        stream_write(
            f" • T3-Only Z-Score      : {stats_res['z_m3']:+.2f}  (p-value = {stats_res['p_m3']:.4f})",
            log_file,
        )

        stream_write("\nEVALUATION:", log_file)
        if p_cpl < 0.01 or p_m1 < 0.01:
            if z_m1 > 3.0 or (p_cpl < 0.001 and med_t < med_p):
                stream_write(
                    " [🔥] ANOMALOUS OVERPERFORMANCE: Highly statistically significant divergence from peer baseline.",
                    log_file,
                )
            elif z_m1 > 2.0 or (p_cpl < 0.05 and med_t < med_p):
                stream_write(
                    " [!] OUTLIER PERFORMANCE: Statistically significant superior precision vs peer baseline (p < 0.05).",
                    log_file,
                )
            elif z_m1 < -2.0 or (p_cpl < 0.05 and med_t > med_p):
                stream_write(
                    " [▼] SIGNIFICANT UNDERPERFORMANCE: Statistically significant underperformance vs peer baseline.",
                    log_file,
                )
            else:
                stream_write(
                    " [✓] NORMAL VARIANCE: Differences vs peers are within expected statistical limits.",
                    log_file,
                )
        else:
            stream_write(
                " [✓] NORMAL VARIANCE: Play style sits completely within expectable human variation (p >= 0.05).",
                log_file,
            )
    else:
        stream_write(" [!] INSUFFICIENT DECISION VOLUME FOR STATISTICAL TESTS", log_file)
        stream_write(
            f"     • Target Player Decisions : {t_decisions} moves (Required: {MIN_PEER_DECISIONS}+)",
            log_file,
        )
        stream_write(
            f"     • Peer Baseline Decisions : {o_decisions} moves (Required: {MIN_PEER_DECISIONS}+)",
            log_file,
        )
    stream_write("=" * 78, log_file)

    if log_file:
        log_file.close()
        print(f"\n[+] Audit log cleanly written to: {log_file_path}")


if __name__ == "__main__":
    main()
