#!/usr/bin/env python3
"""
smoke-detector.py (v1.0.0)
Chess.com Forensic Middlegame & Cadence Profiler

Features:
- Pre-flight account validation and monthly archive streaming via PubAPI
- Polyglot opening book cutoff (Elo2400.bin)
- Disjunctive endgame transition detection:
    Pieces <= 4 OR (No Queens AND Pieces <= 6)
- Multi-threaded Stockfish MultiPV=3 search strictly on middlegame decisions
- Pure Structural Complexity scale (Trivial -> Simple -> Complex -> Highly Complex)
- Full 7-tier Game State classification
- Behavioral Matrix mapping (Game State x Complexity) with Spearman Rank diagnostics
"""

__version__ = "1.0.0"

import sys
import os
import io
import re
import shutil
import argparse
import urllib.request
import urllib.error
import json
import numpy as np
import chess
import chess.pgn
import chess.polyglot
import chess.engine
from scipy.stats import spearmanr

USER_AGENT = "ChessCom-Forensic-Analyzer/1.0.0 (terminal-tool; python-chess)"

# -----------------------------------------------------------------------------
# Module 1: Pre-flight Verification & PubAPI Streaming
# -----------------------------------------------------------------------------

def fetch_json(url: str) -> dict | list | None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        print(f"HTTP Error {e.code} fetching {url}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"Network error fetching {url}: {e}", file=sys.stderr)
        return None

def fetch_text(url: str) -> str | None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.read().decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"Error fetching PGN data from {url}: {e}", file=sys.stderr)
        return None

def verify_and_fetch_games(
    username: str,
    target_tc: str,
    min_decisions: int,
    reader: chess.polyglot.MemoryMappedReader
) -> list[tuple[chess.pgn.Game, int, int]]:
    print(f"[*] Step 1: Verifying Chess.com account for '{username}'...")
    profile_url = f"https://api.chess.com/pub/player/{username}"
    profile = fetch_json(profile_url)
    
    if profile is None:
        print(f"[-] Error: Player '{username}' not found on Chess.com (404).", file=sys.stderr)
        sys.exit(1)
    
    status = profile.get("status", "unknown")
    print(f"[+] Account verified: {profile.get('username', username)} (Status: {status})")

    print(f"[*] Step 2: Querying game archives...")
    archives_url = f"https://api.chess.com/pub/player/{username}/games/archives"
    archives_data = fetch_json(archives_url)
    
    if not archives_data or "archives" not in archives_data or not archives_data["archives"]:
        print(f"[-] Error: No game archives found for '{username}'.", file=sys.stderr)
        sys.exit(1)

    archive_urls = archives_data["archives"]
    archive_urls.reverse()  # Inspect most recent months first
    print(f"[+] Found {len(archive_urls)} monthly archives. Scanning backwards for TC '{target_tc}'...")

    selected_games = []
    accumulated_decisions = 0

    for arch_url in archive_urls:
        month_pgn_url = f"{arch_url}/pgn"
        month_label = f"{arch_url.split('/')[-2]}/{arch_url.split('/')[-1]}"
        print(f"[*] Fetching archive: {month_label}...")
        pgn_text = fetch_text(month_pgn_url)
        if not pgn_text:
            continue

        pgn_io = io.StringIO(pgn_text)
        month_games = []
        while True:
            g = chess.pgn.read_game(pgn_io)
            if g is None:
                break
            month_games.append(g)

        for game in reversed(month_games):
            headers = game.headers
            tc = headers.get("TimeControl", "")
            if tc != target_tc:
                continue

            white = headers.get("White", "")
            black = headers.get("Black", "")
            is_white = white.lower() == username.lower()
            is_black = black.lower() == username.lower()

            if not (is_white or is_black):
                continue

            target_color = chess.WHITE if is_white else chess.BLACK
            mg_start, mg_end = get_phase_boundaries(game, reader)
            if mg_start > mg_end:
                continue

            player_mg_moves = 0
            board = game.board()
            node = game
            ply = 0
            while node.variations:
                next_node = node.variation(0)
                turn = board.turn
                ply += 1
                if mg_start <= ply <= mg_end and turn == target_color:
                    player_mg_moves += 1
                board.push(next_node.move)
                node = next_node

            if player_mg_moves >= 3:
                selected_games.append((game, mg_start, mg_end))
                accumulated_decisions += player_mg_moves

            if accumulated_decisions >= min_decisions:
                print(f"[+] Target decision threshold reached ({accumulated_decisions} >= {min_decisions}) across {len(selected_games)} games.")
                return selected_games

    print(f"[!] Reached end of available archives. Found {accumulated_decisions} decisions across {len(selected_games)} games.")
    if accumulated_decisions == 0:
        print(f"[-] Error: No valid middlegame decisions found for '{username}' in time control '{target_tc}'.", file=sys.stderr)
        sys.exit(1)

    return selected_games

# -----------------------------------------------------------------------------
# Module 2: Clock & Phase Boundaries
# -----------------------------------------------------------------------------

def parse_clock(comment: str) -> float | None:
    match = re.search(r'\[%clk\s+(\d+):(\d+):([\d\.]+)\]', comment)
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)

def parse_time_control(tc_header: str) -> tuple[float, float]:
    if not tc_header or tc_header == "?":
        return 0.0, 0.0
    parts = tc_header.split("+")
    base = float(parts[0]) if parts[0].isdigit() else 0.0
    inc = float(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0.0
    return base, inc

def count_non_pawn_pieces(board: chess.Board) -> tuple[int, int, int]:
    wq = len(board.pieces(chess.QUEEN, chess.WHITE))
    bq = len(board.pieces(chess.QUEEN, chess.BLACK))
    total_pieces = sum(
        len(board.pieces(pt, chess.WHITE)) + len(board.pieces(pt, chess.BLACK))
        for pt in [chess.QUEEN, chess.ROOK, chess.BISHOP, chess.KNIGHT]
    )
    return wq, bq, total_pieces

def is_endgame_state(board: chess.Board) -> bool:
    wq, bq, pieces = count_non_pawn_pieces(board)
    no_queens = (wq == 0 and bq == 0)
    return pieces <= 4 or (no_queens and pieces <= 6)

def get_phase_boundaries(game: chess.pgn.Game, reader: chess.polyglot.MemoryMappedReader) -> tuple[int, int]:
    board = game.board()
    node = game
    ply = 0
    in_book = True
    opening_end_ply = None
    endgame_start_ply = None

    while node.variations:
        next_node = node.variation(0)
        move = next_node.move
        ply += 1

        if in_book:
            try:
                book_moves = [entry.move for entry in reader.find_all(board)]
                if not book_moves or move not in book_moves:
                    in_book = False
                    opening_end_ply = ply
            except Exception:
                in_book = False
                opening_end_ply = ply

        board.push(move)
        node = next_node

        if endgame_start_ply is None and is_endgame_state(board):
            endgame_start_ply = ply

    mg_start = opening_end_ply if opening_end_ply is not None else 1
    mg_end = (endgame_start_ply - 1) if endgame_start_ply is not None else ply
    return mg_start, mg_end

# -----------------------------------------------------------------------------
# Module 3: Position Evaluation & Categorization
# -----------------------------------------------------------------------------

GAME_STATES = [
    "Decisive Advantage",
    "Serious Advantage",
    "Slight Advantage",
    "Undecided / Dynamic Balance",
    "Slight Disadvantage",
    "Serious Disadvantage",
    "Losing Disadvantage"
]

COMPLEXITY_TIERS = [
    "Tier 1: Trivial",
    "Tier 2: Simple",
    "Tier 3: Complex",
    "Tier 4: Highly Complex"
]

def classify_game_state(score_cp: int) -> int:
    if score_cp >= 400:
        return 0
    elif score_cp >= 175:
        return 1
    elif score_cp >= 60:
        return 2
    elif score_cp > -60:
        return 3
    elif score_cp > -175:
        return 4
    elif score_cp > -400:
        return 5
    else:
        return 6

def classify_complexity(scores: list[int], num_legal_moves: int) -> int:
    if num_legal_moves <= 1 or len(scores) < 2:
        return 0  # Tier 1: Trivial

    d12 = abs(scores[0] - scores[1])
    d13 = abs(scores[0] - scores[2]) if len(scores) > 2 else d12

    if d12 >= 300:
        return 0  # Tier 1: Trivial
    elif 100 <= d12 < 300:
        return 1  # Tier 2: Simple
    elif d12 < 50 and d13 < 100:
        return 3  # Tier 4: Highly Complex
    elif d12 < 100:
        return 2  # Tier 3: Complex
    else:
        return 1  # Tier 2: Simple

def evaluate_position(engine: chess.engine.SimpleEngine, board: chess.Board, depth: int) -> dict:
    legal_moves = list(board.legal_moves)
    num_legal = len(legal_moves)
    if num_legal == 0:
        return {"scores": [0], "pv1": None, "num_legal": 0}

    multipv_count = min(3, num_legal)
    info = engine.analyse(board, chess.engine.Limit(depth=depth), multipv=multipv_count)

    scores = []
    pv1_move = None
    for idx, entry in enumerate(info):
        score_obj = entry.get("score")
        if score_obj:
            cp = score_obj.pov(board.turn).score(mate_score=10000)
            scores.append(cp if cp is not None else 0)
        if idx == 0 and "pv" in entry and entry["pv"]:
            pv1_move = entry["pv"][0]

    if not scores:
        scores = [0]

    return {"scores": scores, "pv1": pv1_move, "num_legal": num_legal}

# -----------------------------------------------------------------------------
# Module 4: Engine Execution & Matrix Reporting
# -----------------------------------------------------------------------------

def run_forensic_analysis(
    username: str,
    target_tc: str,
    min_decisions: int,
    book_path: str,
    engine_path: str | None,
    threads: int,
    hash_mb: int,
    depth: int
):
    if not engine_path:
        print("Error: Stockfish binary not found in system PATH. Specify with --engine.", file=sys.stderr)
        sys.exit(1)

    try:
        reader = chess.polyglot.open_reader(book_path)
    except FileNotFoundError:
        print(f"Error: Opening book not found at '{book_path}'", file=sys.stderr)
        sys.exit(1)

    games_to_analyze = verify_and_fetch_games(username, target_tc, min_decisions, reader)

    try:
        engine = chess.engine.SimpleEngine.popen_uci(engine_path)
        engine.configure({
            "Threads": threads,
            "Hash": hash_mb
        })
    except Exception as e:
        print(f"Error initializing engine at '{engine_path}': {e}", file=sys.stderr)
        reader.close()
        sys.exit(1)

    print(f"\n[*] Step 3: Running Stockfish ({threads} threads, {hash_mb}MB hash, Depth: {depth}) on {len(games_to_analyze)} game middlegames...")

    matrix = [[[] for _ in range(4)] for _ in range(7)]
    pooled_times = []
    pooled_complexity_tiers = []
    total_decisions = 0

    base_sec, inc_sec = parse_time_control(target_tc)

    for g_idx, (game, mg_start, mg_end) in enumerate(games_to_analyze, 1):
        headers = game.headers
        white = headers.get("White", "")
        black = headers.get("Black", "")
        is_white = white.lower() == username.lower()
        target_color = chess.WHITE if is_white else chess.BLACK
        opp = black if is_white else white
        date = headers.get("Date", "????.??.??")

        board = game.board()
        node = game
        ply = 0
        prev_clocks = {chess.WHITE: base_sec, chess.BLACK: base_sec}
        game_decisions = 0

        while node.variations:
            next_node = node.variation(0)
            move = next_node.move
            turn = board.turn
            ply += 1

            curr_clock = parse_clock(next_node.comment)
            time_spent = 0.0
            if curr_clock is not None:
                if prev_clocks[turn] is not None:
                    time_spent = max(0.1, round(prev_clocks[turn] - curr_clock + inc_sec, 2))
                prev_clocks[turn] = curr_clock

            if mg_start <= ply <= mg_end and turn == target_color:
                eval_res = evaluate_position(engine, board, depth=depth)
                scores = eval_res["scores"]
                pv1_move = eval_res["pv1"]

                c_idx = classify_complexity(scores, eval_res["num_legal"])
                g_idx_eval = classify_game_state(scores[0])
                is_pv1 = (move == pv1_move)

                matrix[g_idx_eval][c_idx].append((time_spent, is_pv1))
                pooled_times.append(time_spent)
                pooled_complexity_tiers.append(c_idx + 1)

                game_decisions += 1
                total_decisions += 1

            board.push(move)
            node = next_node

        print(f"  Processed [{g_idx}/{len(games_to_analyze)}] {date} vs {opp} (+{game_decisions} decisions | Total: {total_decisions})")

    reader.close()
    engine.quit()

    # -----------------------------------------------------------------------------
    # Module 5: Output Report
    # -----------------------------------------------------------------------------
    print("\n" + "=" * 135)
    print(f"BEHAVIORAL MATRIX (Game State vs. Complexity) for '{username}'")
    print(f"Sample: {total_decisions} decisions across {len(games_to_analyze)} games | TC: {target_tc}")
    print("Cell format: [ Mean Time (s) | PV1 Match % | (n) ]")
    print("=" * 135)

    header_row = f"{'Game State \\ Complexity':<30} | " + " | ".join([f"{t:<23}" for t in COMPLEXITY_TIERS])
    print(header_row)
    print("-" * 135)

    for g_idx_eval, g_label in enumerate(GAME_STATES):
        row_cells = [f"{g_label:<30}"]
        for c_idx in range(4):
            data = matrix[g_idx_eval][c_idx]
            if not data:
                cell_str = "    - |   - | (0)   "
            else:
                n = len(data)
                mean_t = sum(d[0] for d in data) / n
                pv1_pct = (sum(1 for d in data if d[1]) / n) * 100
                cell_str = f"{mean_t:>5.1f}s | {pv1_pct:>4.0f}% | ({n:<3})"
            row_cells.append(f"{cell_str:<23}")
        print(" | ".join(row_cells))

    print("=" * 135)

    corr, p_val = spearmanr(pooled_complexity_tiers, pooled_times)
    mean_time = np.mean(pooled_times)
    median_time = np.median(pooled_times)
    std_time = np.std(pooled_times)

    print("\nDIAGNOSTIC SUMMARY & TIMING PROFILE:")
    print("-" * 60)
    print(f"Total Middlegame Decisions: {total_decisions}")
    print(f"Mean Think Time:            {mean_time:.2f}s (Std: {std_time:.2f}s, Median: {median_time:.2f}s)")
    print(f"Spearman Rank Corr (ρ):     {corr:.4f} (p-value: {p_val:.4e})")

    if total_decisions < 100 or p_val >= 0.05:
        verdict = "INCONCLUSIVE (Insufficient decision volume or high p-value variance)"
    elif corr >= 0.35:
        verdict = "NORMAL HUMAN VARIANCE (Higher complexity -> deeper calculation)"
    elif 0.15 <= corr < 0.35:
        verdict = "MODERATE / MILD CADENCE SCALING"
    elif -0.10 <= corr < 0.15:
        verdict = "FLAT / DECOUPLED TIMING (Lack of cognitive scaling across difficulty tiers)"
    else:
        verdict = "INVERTED CADENCE ANOMALY (Faster on sharp nodes than trivial ones)"

    print(f"Aggregate Pacing Verdict:   {verdict}")
    print("=" * 60 + "\n")

# -----------------------------------------------------------------------------
# CLI Entry Point
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    system_stockfish = shutil.which("stockfish")

    parser = argparse.ArgumentParser(
        description=f"smoke-detector (v{__version__}): Longitudinal Chess Cadence and Complexity Profiler.",
        usage="%(prog)s player tc [options]"
    )
    parser.add_argument("player", help="Target Chess.com username")
    parser.add_argument("tc", help="Exact TimeControl (e.g., 900+10, 600, 180+2)")
    parser.add_argument("--min-decisions", type=int, default=500, help="Target middlegame decision count (default: 500)")
    parser.add_argument("--threads", type=int, default=12, help="Stockfish search worker threads (default: 12)")
    parser.add_argument("--hash", type=int, default=12288, help="Stockfish shared hash memory in MB (default: 12288)")
    parser.add_argument("--book", default="/usr/share/scid/books/Elo2400.bin", help="Path to Polyglot .bin book")
    parser.add_argument("--engine", default=system_stockfish, help="Path to Stockfish binary")
    parser.add_argument("--depth", type=int, default=14, help="Stockfish search depth (default: 14)")
    args = parser.parse_args()

    run_forensic_analysis(
        username=args.player,
        target_tc=args.tc,
        min_decisions=args.min_decisions,
        book_path=args.book,
        engine_path=args.engine,
        threads=args.threads,
        hash_mb=args.hash,
        depth=args.depth
    )
