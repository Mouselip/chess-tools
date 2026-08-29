#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# smoke-detector.py (v2.1.0)
# Chess.com fair play and rating manipulation screener: analyzes move
# cadence, sharp-position engine alignment, and intentional losses to flag
# suspicious indicators ("smoke") without providing definitive proof of
# fair play or cheating.
#
# Copyright (C) 2026 Tyrin R. Price
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

__version__ = "2.1.0"
__author__ = "Tyrin R. Price"
__license__ = "GPL-3.0-or-later"

import sys
import os
import io
import re
import time
import shutil
import argparse
import urllib.request
import urllib.error
import json
import multiprocessing as mp
from concurrent.futures import ProcessPoolExecutor, as_completed
import numpy as np
import chess
import chess.pgn
import chess.polyglot
import chess.engine
from scipy.stats import spearmanr

USER_AGENT = "ChessCom-Forensic-Analyzer/2.1.0 (terminal-tool; python-chess)"
DEFAULT_ENGINE_TIMEOUT = 8.0

# -----------------------------------------------------------------------------
# Module 1: Time Control Categorization & HTTP Helpers
# -----------------------------------------------------------------------------

def parse_time_control(tc_header: str) -> tuple[float, float]:
    if not tc_header or tc_header == "?":
        return 0.0, 0.0
    parts = tc_header.split("+")
    base = float(parts[0]) if parts[0].isdigit() else 0.0
    inc = float(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0.0
    return base, inc

def classify_time_control(tc_str: str) -> str:
    if not tc_str or tc_str == "?":
        return "unknown"
    if tc_str.startswith("1/") or "/" in tc_str:
        return "daily"
    parts = tc_str.split("+")
    try:
        base = float(parts[0])
        inc = float(parts[1]) if len(parts) > 1 else 0.0
    except ValueError:
        return "unknown"
    effective_seconds = base + (40.0 * inc)
    if effective_seconds < 180.0:
        return "bullet"
    elif effective_seconds < 480.0:
        return "blitz"
    else:
        return "rapid_classical"

def fetch_json(url: str) -> dict | list | None:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
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

def has_setup_tag(pgn: str) -> bool:
    if not pgn:
        return False
    for line in pgn.splitlines():
        if line.startswith("[SetUp "):
            return True
        if line.startswith("1. "):
            break
    return False

# -----------------------------------------------------------------------------
# Module 2: Clock & Phase Boundaries
# -----------------------------------------------------------------------------

def parse_clock(comment: str) -> float | None:
    match = re.search(r'\[%clk\s+(\d+):(\d+):([\d\.]+)\]', comment)
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)

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

def get_phase_boundaries(game: chess.pgn.Game, reader: chess.polyglot.MemoryMappedReader | None) -> tuple[int, int]:
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

        if in_book and reader is not None:
            try:
                book_moves = [entry.move for entry in reader.find_all(board)]
                if not book_moves or move not in book_moves:
                    in_book = False
                    opening_end_ply = ply
            except Exception:
                in_book = False
                opening_end_ply = ply
        elif in_book and reader is None:
            in_book = False
            opening_end_ply = 1

        board.push(move)
        node = next_node

        if endgame_start_ply is None and is_endgame_state(board):
            endgame_start_ply = ply

    mg_start = opening_end_ply if opening_end_ply is not None else 1
    mg_end = (endgame_start_ply - 1) if endgame_start_ply is not None else ply
    return mg_start, mg_end

# -----------------------------------------------------------------------------
# Module 3: Pre-flight Verification & JSON Archive Harvester
# -----------------------------------------------------------------------------

def verify_and_fetch_games(
    username: str,
    target_tc: str,
    min_decisions: int,
    book_path: str,
    tc_category: str,
    allow_unrated: bool = False
) -> list[str]:
    print(f"[*] Step 1: Verifying Chess.com account for '{username}'...")
    profile_url = f"https://api.chess.com/pub/player/{username.lower()}"
    profile = fetch_json(profile_url)

    if profile is None:
        print(f"[-] Error: Player '{username}' not found on Chess.com (404).", file=sys.stderr)
        sys.exit(1)

    status = profile.get("status", "unknown")
    canonical_username = profile.get("username", username)
    print(f"[+] Account verified: {canonical_username} (Status: {status})")

    reader = None
    if tc_category != "bullet":
        try:
            reader = chess.polyglot.open_reader(book_path)
        except FileNotFoundError:
            print(f"[-] Error: Opening book not found at '{book_path}'", file=sys.stderr)
            sys.exit(1)

    print(f"[*] Step 2: Querying game archives...")
    archives_url = f"https://api.chess.com/pub/player/{canonical_username.lower()}/games/archives"
    archives_data = fetch_json(archives_url)

    if not archives_data or "archives" not in archives_data or not archives_data["archives"]:
        print(f"[-] Error: No game archives found for '{canonical_username}'.", file=sys.stderr)
        if reader is not None:
            reader.close()
        sys.exit(1)

    archive_urls = archives_data["archives"]
    archive_urls.reverse()
    print(f"[+] Found {len(archive_urls)} monthly archives. Scanning backwards for TC '{target_tc}'...")

    selected_game_pgns = []
    accumulated_decisions = 0
    rated_target = not allow_unrated

    for arch_url in archive_urls:
        month_label = f"{arch_url.split('/')[-2]}/{arch_url.split('/')[-1]}"
        print(f"[*] Fetching archive: {month_label}...")
        month_data = fetch_json(arch_url)
        if not month_data or "games" not in month_data:
            continue

        for game_obj in reversed(month_data.get("games", [])):
            if not allow_unrated and game_obj.get("rated") != rated_target:
                continue

            tc = game_obj.get("time_control", "")
            if tc != target_tc:
                continue

            pgn_str = game_obj.get("pgn", "")
            if not pgn_str or has_setup_tag(pgn_str):
                continue

            game = chess.pgn.read_game(io.StringIO(pgn_str))
            if game is None:
                continue

            headers = game.headers
            if headers.get("SetUp") == "1":
                continue

            white = headers.get("White", "")
            black = headers.get("Black", "")
            is_white = white.lower() == canonical_username.lower()
            is_black = black.lower() == canonical_username.lower()

            if not (is_white or is_black):
                continue

            target_color = chess.WHITE if is_white else chess.BLACK

            if tc_category == "bullet":
                player_moves = 0
                board = game.board()
                node = game
                while node.variations:
                    next_node = node.variation(0)
                    turn = board.turn
                    if turn == target_color:
                        player_moves += 1
                    board.push(next_node.move)
                    node = next_node

                if player_moves >= 5:
                    exporter = chess.pgn.StringExporter(headers=True, variations=True, comments=True)
                    selected_game_pgns.append(game.accept(exporter))
                    accumulated_decisions += player_moves
            else:
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
                    exporter = chess.pgn.StringExporter(headers=True, variations=True, comments=True)
                    selected_game_pgns.append(game.accept(exporter))
                    accumulated_decisions += player_mg_moves

            if accumulated_decisions >= min_decisions:
                if reader is not None:
                    reader.close()
                print(f"[+] Target decision threshold reached ({accumulated_decisions} >= {min_decisions}) across {len(selected_game_pgns)} games.")
                return selected_game_pgns

    if reader is not None:
        reader.close()
    print(f"[!] Reached end of available archives. Found {accumulated_decisions} decisions across {len(selected_game_pgns)} games.")
    if accumulated_decisions == 0:
        print(f"[-] Error: No valid decisions found for '{canonical_username}' in time control '{target_tc}'.", file=sys.stderr)
        sys.exit(1)

    return selected_game_pgns

# -----------------------------------------------------------------------------
# Module 4: Engine Evaluation & Categorization
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

def classify_game_state(score_white_cp: int, turn: chess.Color) -> int:
    player_score = score_white_cp if turn == chess.WHITE else -score_white_cp
    if player_score >= 400:
        return 0
    elif player_score >= 175:
        return 1
    elif player_score >= 60:
        return 2
    elif player_score > -60:
        return 3
    elif player_score > -175:
        return 4
    elif player_score > -400:
        return 5
    else:
        return 6

def classify_complexity(white_scores: list[int], num_legal_moves: int) -> int:
    if num_legal_moves <= 1 or len(white_scores) < 2:
        return 0
    d12 = abs(white_scores[0] - white_scores[1])
    d13 = abs(white_scores[0] - white_scores[2]) if len(white_scores) > 2 else d12

    if d12 >= 300:
        return 0
    elif 100 <= d12 < 300:
        return 1
    elif d12 < 50 and d13 < 100:
        return 3
    elif d12 < 100:
        return 2
    else:
        return 1

def spawn_engine(engine_path: str, hash_mb: int) -> chess.engine.SimpleEngine:
    engine = chess.engine.SimpleEngine.popen_uci(engine_path)
    engine.configure({
        "Threads": 1,
        "Hash": hash_mb
    })
    return engine

def safe_analyse_position(
    engine: chess.engine.SimpleEngine,
    board: chess.Board,
    multipv: int,
    depth: int,
    timeout: float,
    engine_path: str,
    hash_mb: int
) -> tuple[chess.engine.SimpleEngine, list, float]:
    if board.is_game_over() or not list(board.legal_moves):
        return engine, [], 0.0

    t0 = time.perf_counter()
    try:
        info = engine.analyse(
            board,
            chess.engine.Limit(depth=depth, time=timeout),
            multipv=multipv
        )
        eval_time = time.perf_counter() - t0
        return engine, info, eval_time
    except Exception:
        try:
            if hasattr(engine, "transport") and engine.transport:
                engine.transport.kill()
        except Exception:
            pass
        try:
            engine.close()
        except Exception:
            pass
        new_engine = spawn_engine(engine_path, hash_mb)
        eval_time = time.perf_counter() - t0
        return new_engine, [], eval_time

def evaluate_position(
    engine: chess.engine.SimpleEngine,
    board: chess.Board,
    depth: int,
    timeout: float,
    engine_path: str,
    hash_mb: int
) -> tuple[chess.engine.SimpleEngine, dict]:
    legal_moves = list(board.legal_moves)
    num_legal = len(legal_moves)
    if num_legal == 0 or board.is_game_over():
        return engine, {"scores": [0], "pv1": None, "num_legal": 0, "reached_depth": depth, "eval_time": 0.0}

    multipv_count = min(3, num_legal)
    engine, info, eval_time = safe_analyse_position(engine, board, multipv_count, depth, timeout, engine_path, hash_mb)

    white_scores = []
    pv1_move = None
    reached_depth = depth

    if info:
        reached_depth = info[0].get("depth", depth)
        for idx, entry in enumerate(info):
            score_obj = entry.get("score")
            if score_obj:
                cp = score_obj.white().score(mate_score=10000)
                white_scores.append(cp if cp is not None else 0)
            if idx == 0 and "pv" in entry and entry["pv"]:
                pv1_move = entry["pv"][0]

    if not white_scores:
        white_scores = [0]

    return engine, {
        "scores": white_scores,
        "pv1": pv1_move,
        "num_legal": num_legal,
        "reached_depth": reached_depth,
        "eval_time": eval_time
    }

# -----------------------------------------------------------------------------
# Module 5: Workers & Stream Execution
# -----------------------------------------------------------------------------

def analyze_single_game_engine(
    game_idx: int,
    pgn_str: str,
    username: str,
    target_tc: str,
    tc_category: str,
    book_path: str,
    engine_path: str,
    depth: int,
    hash_mb_per_worker: int,
    engine_timeout: float
) -> tuple[int, str, str, int, int, float, float, list[tuple[float, bool, int, int, int]], dict | None]:
    reader = None
    if os.path.exists(book_path):
        try:
            reader = chess.polyglot.open_reader(book_path)
        except Exception:
            reader = None

    engine = spawn_engine(engine_path, hash_mb_per_worker)

    game = chess.pgn.read_game(io.StringIO(pgn_str))
    headers = game.headers
    white = headers.get("White", "")
    black = headers.get("Black", "")
    is_white = white.lower() == username.lower()
    target_color = chess.WHITE if is_white else chess.BLACK
    opp = black if is_white else white
    date = headers.get("Date", "????.??.??")
    result_header = headers.get("Result", "*")

    mg_start, mg_end = get_phase_boundaries(game, reader)
    if reader is not None:
        reader.close()

    base_sec, inc_sec = parse_time_control(target_tc)
    prev_clocks = {chess.WHITE: base_sec, chess.BLACK: base_sec}

    board = game.board()
    node = game
    ply = 0
    results = []
    min_depth_reached = depth
    engine_times = []
    decisions_eval_history = []
    consecutive_blowout_plies = 0

    while node.variations:
        next_node = node.variation(0)
        move = next_node.move
        turn = board.turn
        ply += 1

        curr_clock = parse_clock(next_node.comment) if tc_category != "daily" else None
        time_spent = 0.0
        if curr_clock is not None:
            if prev_clocks[turn] is not None:
                time_spent = max(0.1, round(prev_clocks[turn] - curr_clock + inc_sec, 2))
            prev_clocks[turn] = curr_clock

        if mg_start <= ply <= mg_end and turn == target_color:
            if consecutive_blowout_plies >= 2:
                board.push(move)
                node = next_node
                continue

            engine, eval_res = evaluate_position(
                engine, board, depth=depth, timeout=engine_timeout,
                engine_path=engine_path, hash_mb=hash_mb_per_worker
            )
            white_scores = eval_res["scores"]
            pv1_move = eval_res["pv1"]
            pos_depth = eval_res["reached_depth"]
            eval_t = eval_res["eval_time"]

            engine_times.append(eval_t)

            if pos_depth < min_depth_reached:
                min_depth_reached = pos_depth

            c_idx = classify_complexity(white_scores, eval_res["num_legal"])
            g_idx_eval = classify_game_state(white_scores[0], turn)
            is_pv1 = (move == pv1_move)

            best_white_score = white_scores[0]
            board.push(move)

            if board.is_game_over():
                if board.is_checkmate():
                    after_white_score = 10000 if turn == chess.WHITE else -10000
                else:
                    after_white_score = 0
            else:
                engine, info_after, _ = safe_analyse_position(
                    engine, board, multipv=1, depth=depth, timeout=engine_timeout,
                    engine_path=engine_path, hash_mb=hash_mb_per_worker
                )
                after_white_score = best_white_score
                if info_after and "score" in info_after[0]:
                    score_after_obj = info_after[0]["score"]
                    after_val = score_after_obj.white().score(mate_score=10000)
                    if after_val is not None:
                        after_white_score = after_val

            board.pop()

            if turn == chess.WHITE:
                eval_drop = best_white_score - after_white_score
            else:
                eval_drop = after_white_score - best_white_score

            cpl = max(0, eval_drop)

            results.append((time_spent, is_pv1, c_idx, g_idx_eval, cpl))
            decisions_eval_history.append({
                "ply": ply,
                "move_num": (ply + 1) // 2,
                "san": board.san(move),
                "is_pv1": is_pv1,
                "best_white_score": best_white_score,
                "after_white_score": after_white_score,
                "eval_drop": eval_drop,
                "time_spent": time_spent,
                "curr_clock": curr_clock
            })

            if abs(after_white_score) >= 600:
                consecutive_blowout_plies += 1
            else:
                consecutive_blowout_plies = 0
        else:
            if 0 < consecutive_blowout_plies < 2:
                engine, eval_res = evaluate_position(
                    engine, board, depth=depth, timeout=engine_timeout,
                    engine_path=engine_path, hash_mb=hash_mb_per_worker
                )
                opp_best_white = eval_res["scores"][0]
                if abs(opp_best_white) >= 600:
                    consecutive_blowout_plies += 1
                else:
                    consecutive_blowout_plies = 0

        board.push(move)
        node = next_node

    try:
        engine.quit()
    except Exception:
        pass

    avg_engine_time = float(np.mean(engine_times)) if engine_times else 0.0
    max_engine_time = float(np.max(engine_times)) if engine_times else 0.0

    # -------------------------------------------------------------------------
    # Thrown Game Red Flag Detector (Gated on Target Loss & Absolute Scores)
    # -------------------------------------------------------------------------
    anomaly_flag = None
    target_lost = (is_white and result_header == "0-1") or (not is_white and result_header == "1-0")

    if target_lost and len(decisions_eval_history) >= 10 and tc_category != "bullet":
        final_d = decisions_eval_history[-1]
        penultimate_d = decisions_eval_history[-2]

        is_game_end_decision = (final_d["ply"] >= ply - 2)

        if is_white:
            prior_state_viable = (penultimate_d["after_white_score"] >= -100 or penultimate_d["best_white_score"] >= -100)
            is_terminal_blunder = (final_d["eval_drop"] >= 600 or final_d["after_white_score"] <= -800)
            eval_desc = f"-{final_d['eval_drop']} cp" if final_d["after_white_score"] > -8000 else "allowed mate"
            prior_eval_str = f"{penultimate_d['after_white_score']} cp"
        else:
            prior_state_viable = (penultimate_d["after_white_score"] <= 100 or penultimate_d["best_white_score"] <= 100)
            is_terminal_blunder = (final_d["eval_drop"] >= 600 or final_d["after_white_score"] >= 800)
            eval_desc = f"-{final_d['eval_drop']} cp" if final_d["after_white_score"] < 8000 else "allowed mate"
            prior_eval_str = f"{penultimate_d['after_white_score']} cp"

        has_comfortable_clock = (final_d["curr_clock"] is not None and final_d["curr_clock"] >= 60.0) or (tc_category == "daily")
        deliberate_time = final_d["time_spent"] >= 3.0 or tc_category == "daily"

        if is_game_end_decision and prior_state_viable and is_terminal_blunder and has_comfortable_clock and deliberate_time:
            clock_repr = f"{int(final_d['curr_clock']//60)}m{int(final_d['curr_clock']%60):02d}s" if final_d["curr_clock"] is not None else "Daily"
            anomaly_flag = {
                "game_idx": game_idx,
                "date": date,
                "opp": opp,
                "blunder_move": final_d["move_num"],
                "blunder_san": final_d["san"],
                "eval_desc": eval_desc,
                "prior_eval": prior_eval_str,
                "clock_left": clock_repr,
                "think_time": final_d["time_spent"]
            }

    return game_idx, date, opp, len(results), min_depth_reached, avg_engine_time, max_engine_time, results, anomaly_flag

def run_bullet_clock_analysis(username: str, target_tc: str, game_pgns: list[str]):
    print(f"\n[*] Step 3: Parsing clock timestamps across {len(game_pgns)} bullet games...")
    base_sec, inc_sec = parse_time_control(target_tc)
    all_move_times = []
    game_move_counts = []

    for pgn_str in game_pgns:
        game = chess.pgn.read_game(io.StringIO(pgn_str))
        headers = game.headers
        white = headers.get("White", "")
        black = headers.get("Black", "")
        is_white = white.lower() == username.lower()
        target_color = chess.WHITE if is_white else chess.BLACK

        board = game.board()
        node = game
        prev_clocks = {chess.WHITE: base_sec, chess.BLACK: base_sec}
        this_game_times = []

        while node.variations:
            next_node = node.variation(0)
            move = next_node.move
            turn = board.turn

            curr_clock = parse_clock(next_node.comment)
            if curr_clock is not None:
                if prev_clocks[turn] is not None:
                    time_spent = max(0.01, round(prev_clocks[turn] - curr_clock + inc_sec, 2))
                    if turn == target_color:
                        this_game_times.append(time_spent)
                prev_clocks[turn] = curr_clock

            board.push(move)
            node = next_node

        if this_game_times:
            all_move_times.extend(this_game_times)
            game_move_counts.append(len(this_game_times))

    total_moves = len(all_move_times)
    times_arr = np.array(all_move_times)

    mean_t = float(np.mean(times_arr))
    std_t = float(np.std(times_arr))
    median_t = float(np.median(times_arr))
    cv = std_t / mean_t if mean_t > 0 else 0.0

    if len(times_arr) > 1:
        lag1_r = float(np.corrcoef(times_arr[:-1], times_arr[1:])[0, 1])
    else:
        lag1_r = 0.0

    sub_half_sec = float(np.sum(times_arr < 0.5) / total_moves * 100)
    premoves = float(np.sum(times_arr <= 0.15) / total_moves * 100)

    print("\n" + "=" * 80)
    print(f"BULLET CADENCE & CLOCK PROFILE for '{username}'")
    print(f"Sample: {total_moves} moves across {len(game_pgns)} games | TC: {target_tc}")
    print("=" * 80)
    print(f"Mean Move Time:              {mean_t:.2f}s (Std: {std_t:.2f}s, Median: {median_t:.2f}s)")
    print(f"Coefficient of Variation (CV): {cv:.4f}")
    print(f"Lag-1 Autocorrelation (r):     {lag1_r:.4f}")
    print(f"Sub-0.5s Move Rate:          {sub_half_sec:.1f}%")
    print(f"Premove Rate (<=0.15s):      {premoves:.1f}%")
    print("-" * 80)

    if cv < 0.25:
        verdict = "[SUSPICIOUS] Metronomic cadence (Extremely low variance across moves)"
    elif cv < 0.40:
        verdict = "[BORDERLINE] Abnormally flat pacing for bullet time control"
    elif lag1_r > 0.45:
        verdict = "[SUSPICIOUS] High autocorrelation anomaly (Fixed timer/jitter signature)"
    else:
        verdict = "[CLEAN] Natural human dispersion (Chaotic motor variance)"

    print(f"Diagnostic Pacing Verdict:   {verdict}")
    print("=" * 80 + "\n")

# -----------------------------------------------------------------------------
# Module 6: Output Reports & Dispatcher
# -----------------------------------------------------------------------------

def print_behavioral_matrix_report(username: str, target_tc: str, total_decisions: int, total_games: int, matrix: list, pooled_times: list, pooled_tiers: list, all_results: list):
    print("\n" + "=" * 135)
    print(f"BEHAVIORAL MATRIX (Game State vs. Complexity) for '{username}'")
    print(f"Sample: {total_decisions} decisions across {total_games} games | TC: {target_tc}")
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

    if total_decisions > 0:
        corr, p_val = spearmanr(pooled_tiers, pooled_times)
        mean_time = np.mean(pooled_times)
        median_time = np.median(pooled_times)
        std_time = np.std(pooled_times)

        tier4_moves = [r for r in all_results if r[2] == 3]
        tier4_count = len(tier4_moves)
        tier4_matches = sum(1 for r in tier4_moves if r[1])
        tier4_pct = (tier4_matches / tier4_count * 100) if tier4_count > 0 else 0.0

        pv1_total_matches = sum(1 for r in all_results if r[1])
        pv1_total_pct = (pv1_total_matches / total_decisions * 100) if total_decisions > 0 else 0.0

        if corr >= 0.35:
            cadence_desc = "Normal Cognitive Scaling"
        elif 0.15 <= corr < 0.35:
            cadence_desc = "Moderate Cadence Scaling"
        elif -0.10 <= corr < 0.15:
            cadence_desc = "Flat / Decoupled"
        else:
            cadence_desc = "Inverted Pacing"

        print("\nDIAGNOSTIC SUMMARY & TIMING PROFILE:")
        print("-" * 60)
        print(f"Total Middlegame Decisions: {total_decisions}")
        print(f"Mean Think Time:            {mean_time:.2f}s (Std: {std_time:.2f}s, Median: {median_time:.2f}s)")
        print(f"Spearman Rank Corr (ρ):     {corr:.4f} (p-value: {p_val:.4e})")
        print(f"Cadence Profile:            {cadence_desc}")
        print("-" * 60)

        if total_decisions < 100 or p_val >= 0.05:
            final_verdict = "[INCONCLUSIVE] Insufficient decision volume or high p-value variance"
        elif corr < 0.15 and tier4_pct >= 65.0:
            final_verdict = "[SUSPICIOUS] High engine fidelity on complex nodes with decoupled cadence"
        elif tier4_pct >= 75.0:
            final_verdict = "[SUSPICIOUS] Super-GM/Engine precision on sharp Tier 4 branches"
        elif corr < 0.15 and tier4_pct < 45.0:
            final_verdict = "[CLEAN] Intuitive human play (Flat timing with degraded complex precision)"
        elif corr >= 0.35:
            final_verdict = "[CLEAN] Authentic human cognitive scaling"
        elif 0.15 <= corr < 0.35 and tier4_pct < 55.0:
            final_verdict = "[CLEAN] Standard human pacing and resolution profile"
        else:
            final_verdict = "[BORDERLINE] Intermediate precision / cadence alignment"

        print("\n" + "=" * 135)
        print("FINAL FORENSIC VERDICT:")
        print("-" * 135)
        print(f"  * Cadence Scaling:         {cadence_desc} (ρ: {corr:.4f}, p: {p_val:.4e})")
        print(f"  * Tier 4 Engine Accuracy:  {tier4_pct:.1f}% ({tier4_matches}/{tier4_count} matches on sharp nodes)")
        print(f"  * Overall Engine Match:    {pv1_total_pct:.1f}% ({pv1_total_matches}/{total_decisions})")
        print("-" * 135)
        print(f"  >> OVERALL VERDICT:        {final_verdict}")
        print("=" * 135 + "\n")

def print_daily_report(username: str, target_tc: str, total_decisions: int, total_games: int, all_results: list):
    pv1_matches = sum(1 for r in all_results if r[1])
    pv1_overall_pct = (pv1_matches / total_decisions * 100) if total_decisions > 0 else 0.0

    tier4_moves = [r for r in all_results if r[2] == 3]
    tier4_count = len(tier4_moves)
    tier4_matches = sum(1 for r in tier4_moves if r[1])
    tier4_pct = (tier4_matches / tier4_count * 100) if tier4_count > 0 else 0.0

    cpl_list = [r[4] for r in all_results]
    acpl = float(np.mean(cpl_list)) if cpl_list else 0.0
    blunders = sum(1 for c in cpl_list if c >= 100)
    blunder_pct = (blunders / total_decisions * 100) if total_decisions > 0 else 0.0

    print("\n" + "=" * 80)
    print(f"DAILY / CORRESPONDENCE ENGINE FIDELITY PROFILE for '{username}'")
    print(f"Sample: {total_decisions} middlegame decisions across {total_games} games | TC: {target_tc}")
    print("=" * 80)
    print(f"Overall PV1 Match Rate:          {pv1_overall_pct:.1f}% ({pv1_matches}/{total_decisions})")
    print(f"Tier 4 (Highly Complex) PV1 Match: {tier4_pct:.1f}% ({tier4_matches}/{tier4_count})")
    print(f"Average Centipawn Loss (ACPL):   {acpl:.1f} cp")
    print(f"Blunder Rate (>= 100 cp loss):   {blunder_pct:.1f}% ({blunders}/{total_decisions})")
    print("-" * 80)

    if tier4_pct > 80.0 and acpl < 15.0:
        verdict = "[SUSPICIOUS] High fidelity / engine-level resolution on complex nodes"
    elif tier4_pct > 65.0 or acpl < 25.0:
        verdict = "[BORDERLINE] Strong master-level correspondence play"
    else:
        verdict = "[CLEAN] Typical human correspondence profile (Sub-optimal resolution on complex branches)"

    print(f"Engine Alignment Verdict:        {verdict}")
    print("=" * 80 + "\n")

def print_detected_anomalies(anomalies: list[dict]):
    if not anomalies:
        return
    print("\n" + "!" * 80)
    print(f"FORENSIC ALERT: {len(anomalies)} SUSPICIOUS RESULT MANIPULATION / THROWN GAME ANOMALIES DETECTED")
    print("!" * 80)
    for a in anomalies:
        print(f"  * Game #{a['game_idx']} ({a['date']} vs {a['opp']}):")
        print(f"      - Position Viability: Prior move held stable eval ({a['prior_eval']})")
        print(f"      - Terminal Collapse:  Move {a['blunder_move']}. {a['blunder_san']} ({a['eval_desc']})")
        print(f"      - Non-Panic Clock:    {a['clock_left']} in reserve (Spent {a['think_time']:.1f}s calculating blunder)")
    print("!" * 80 + "\n")

def run_forensic_analysis(
    username: str,
    target_tc: str,
    min_decisions: int,
    book_path: str,
    engine_path: str | None,
    workers: int,
    hash_per_worker: int,
    depth: int,
    engine_timeout: float,
    allow_unrated: bool = False,
    export_pgn_path: str | None = None
):
    tc_category = classify_time_control(target_tc)

    if tc_category == "bullet":
        print("[*] Bullet time detected! Entering the Matrix...")
        print("[*] Bypassing engine evaluation: analyzing clock signatures and interval cadence only.")
    elif tc_category == "blitz":
        print("[*] Blitz stream detected.")
        print("[*] Evaluating complete middlegame decisions across all clock states.")
    elif tc_category == "rapid_classical":
        print("[*] Rapid/Classical stream detected.")
        print("[*] Launching full behavioral matrix profiling.")
    elif tc_category == "daily":
        print("[*] Daily/Correspondence stream detected.")
        print("[*] Bypassing clock timestamps: running pure engine fidelity and Tier-4 precision profiling.")

    if tc_category != "bullet":
        print("[*] Engine & Boundary Configuration:")
        print(f"    - Engine: Stockfish (Depth: {depth}, MultiPV: 3, Workers: {workers}, Hash/Worker: {hash_per_worker}MB, Max Time/Move: {engine_timeout}s)")
        print(f"    - Opening Book: {book_path}")
        print("    - Endgame Trigger: <= 4 non-pawn pieces OR (no queens AND <= 6 non-pawn pieces)")

    if tc_category != "bullet" and not engine_path:
        print("[-] Error: Stockfish binary not found in system PATH. Specify with --engine.", file=sys.stderr)
        sys.exit(1)

    game_pgns = verify_and_fetch_games(username, target_tc, min_decisions, book_path, tc_category, allow_unrated)
    total_games = len(game_pgns)

    if export_pgn_path:
        try:
            with open(export_pgn_path, "w", encoding="utf-8") as f:
                for pgn_str in game_pgns:
                    f.write(pgn_str.strip() + "\n\n")
            print(f"[+] Exported {total_games} target games to '{export_pgn_path}'.")
        except OSError as e:
            print(f"[-] Error writing PGN export to '{export_pgn_path}': {e}", file=sys.stderr)

    if tc_category == "bullet":
        run_bullet_clock_analysis(username, target_tc, game_pgns)
        return

    print(f"\n[*] Step 3: Launching parallel engine pool ({workers} workers, {hash_per_worker}MB hash/worker, Depth: {depth})...")

    matrix = [[[] for _ in range(4)] for _ in range(7)]
    pooled_times = []
    pooled_complexity_tiers = []
    all_results = []
    detected_anomalies = []
    total_decisions = 0
    processed_count = 0

    with ProcessPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                analyze_single_game_engine,
                idx,
                pgn,
                username,
                target_tc,
                tc_category,
                book_path,
                engine_path,
                depth,
                hash_per_worker,
                engine_timeout
            ): idx for idx, pgn in enumerate(game_pgns, 1)
        }

        for future in as_completed(futures):
            processed_count += 1
            try:
                g_idx, date, opp, game_decisions, min_depth_reached, avg_engine_t, max_engine_t, results, anomaly = future.result()
                total_decisions += game_decisions
                all_results.extend(results)

                if anomaly:
                    detected_anomalies.append(anomaly)

                for time_spent, is_pv1, c_idx, g_idx_eval, _ in results:
                    matrix[g_idx_eval][c_idx].append((time_spent, is_pv1))
                    pooled_times.append(time_spent)
                    pooled_complexity_tiers.append(c_idx + 1)

                depth_part = f"Min Depth: {min_depth_reached}" if min_depth_reached >= depth else f"Min Depth: {min_depth_reached} (CAPPED)"
                timing_str = f"[Avg: {avg_engine_t:.2f}s/mv | Max Time: {max_engine_t:.2f}s | {depth_part}]"
                flag_str = " [FLAG: THROWN_GAME_ANOMALY]" if anomaly else ""
                print(f"  Finished [{processed_count}/{total_games}] Game #{g_idx} ({date} vs {opp}) -> +{game_decisions} decisions (Total: {total_decisions}) {timing_str}{flag_str}")
            except Exception as e:
                print(f"  [-] Error analyzing game: {e}", file=sys.stderr)

    if tc_category == "daily":
        print_daily_report(username, target_tc, total_decisions, total_games, all_results)
    else:
        print_behavioral_matrix_report(username, target_tc, total_decisions, total_games, matrix, pooled_times, pooled_complexity_tiers, all_results)

    print_detected_anomalies(detected_anomalies)

# -----------------------------------------------------------------------------
# CLI Entry Point
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    system_stockfish = shutil.which("stockfish")
    default_workers = min(12, os.cpu_count() or 1)

    parser = argparse.ArgumentParser(
        description=f"smoke-detector (v{__version__}): Chess.com fair play and rating manipulation screener.",
        usage="%(prog)s player tc [options]"
    )
    parser.add_argument("player", help="Target Chess.com username")
    parser.add_argument("tc", help="Exact TimeControl (e.g., 900+10, 600, 180+2, 60, 1/86400)")
    parser.add_argument("-u", "--unrated", action="store_true", help="Include unrated/casual games (default: rated only)")
    parser.add_argument("--min-decisions", type=int, default=500, help="Target middlegame decision count (default: 500)")
    parser.add_argument("--workers", type=int, default=default_workers, help=f"Parallel worker processes (default: {default_workers})")
    parser.add_argument("--hash-per-worker", type=int, default=1024, help="Stockfish hash table per worker in MB (default: 1024)")
    parser.add_argument("--book", default="/usr/share/scid/books/Elo2400.bin", help="Path to Polyglot .bin book")
    parser.add_argument("--engine", default=system_stockfish, help="Path to Stockfish binary")
    parser.add_argument("--depth", type=int, default=18, help="Stockfish search depth (default: 18)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_ENGINE_TIMEOUT, help=f"Max engine calculation ceiling per move in seconds (default: {DEFAULT_ENGINE_TIMEOUT})")
    parser.add_argument("--export-pgn", default=None, help="Optional path to save all harvested candidate games as PGN")

    if len(sys.argv) == 1:
        parser.print_help(sys.stderr)
        sys.exit(1)

    args = parser.parse_args()

    run_forensic_analysis(
        username=args.player,
        target_tc=args.tc,
        min_decisions=args.min_decisions,
        book_path=args.book,
        engine_path=args.engine,
        workers=args.workers,
        hash_per_worker=args.hash_per_worker,
        depth=args.depth,
        engine_timeout=args.timeout,
        allow_unrated=args.unrated,
        export_pgn_path=args.export_pgn
    )
