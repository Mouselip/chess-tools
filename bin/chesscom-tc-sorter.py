#!/usr/bin/env python3
# Copyright (C) 2026 Tyrin Price
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

"""
chesscom-tc-sorter.py - Organize Chess.com PGN archives by time control
Version: 0.0.1
"""

import os
import re
import sys
import argparse
import urllib.request
import urllib.error
import chess.pgn

__version__ = "0.0.1"


def validate_username(username: str) -> bool:
    """Validate username against Chess.com public API."""
    url = f"https://api.chess.com/pub/player/{username.lower()}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": f"chesscom-tc-sorter.py/{__version__} (Chess tools utility)"}
    )
    try:
        with urllib.request.urlopen(req) as response:
            return response.status == 200
    except (urllib.error.HTTPError, urllib.error.URLError):
        return False


def get_tc_filename(game) -> str:
    """
    Classify game variant/TimeControl and return target PGN output filename.
    """
    event = game.headers.get("Event", "").lower()
    variant = game.headers.get("Variant", "").lower()

    # 1. Chess960 games
    if variant == "chess960":
        return "chess960.pgn"

    # 2. Custom setup positions
    if game.headers.get("SetUp", "") == "1":
        return "setup.pgn"

    # 3. Unrated or odds games
    if "odds chess" in event or event.startswith("unrated"):
        return "skipped.pgn"

    # 4. Standard games classification by TimeControl
    tc = game.headers.get("TimeControl", "").strip()

    if not tc or tc in ("-", "?"):
        return "skipped.pgn"

    try:
        # Daily games (e.g., 1/86400, 1/259200)
        if tc.startswith("1/"):
            parts = tc.split("/")
            seconds_per_move = int(parts[1])
            days = seconds_per_move // 86400
            if days == 1:
                return "daily-1-day.pgn"
            return f"daily-{days}-days.pgn"

        # Real-time games (<base>+<inc> or <base>)
        if "+" in tc:
            base_str, inc_str = tc.split("+")
            base = int(base_str)
            inc = int(inc_str)
        else:
            base = int(tc)
            inc = 0

        # Estimate category based on 40 moves
        estimated = base + (40 * inc)

        if estimated < 180:
            category = "bullet"
        elif estimated < 600:
            category = "blitz"
        else:
            category = "rapid"

        # Format base time string
        if base >= 60:
            if base % 60 == 0:
                base_fmt = f"{base // 60}"
            else:
                base_fmt = f"{base / 60:.1f}"
        else:
            base_fmt = f"{base}s"

        return f"{category}-{base_fmt}-plus-{inc}.pgn"

    except Exception:
        return "skipped.pgn"


def main():
    parser = argparse.ArgumentParser(
        prog="chesscom-tc-sorter.py",
        description=(
            "Parses Chess.com monthly PGN archives in the current working directory "
            "and organizes games into distinct PGN files grouped by speed category "
            "and exact time control (e.g., rapid-15-plus-10.pgn, blitz-3-plus-2.pgn)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    parser.add_argument(
        "username",
        nargs="?",
        help="Valid Chess.com username (case-insensitive)"
    )
    parser.add_argument(
        "-n", "--dry-run",
        action="store_true",
        help="Show output filenames and game counts without writing files"
    )
    parser.add_argument(
        "-v", "--version",
        action="version",
        version=f"%(prog)s {__version__}"
    )

    args = parser.parse_args()

    # Check for missing username
    if not args.username:
        print("Error: A Chess.com username is required.\n", file=sys.stderr)
        parser.print_help()
        sys.exit(1)

    print(f"Verifying username '{args.username}' with Chess.com API...")
    if not validate_username(args.username):
        print(f"Error: '{args.username}' is not a valid Chess.com username.\n", file=sys.stderr)
        parser.print_help()
        sys.exit(1)

    print("Username verified successfully.")

    # Match monthly archives for user (e.g., Mouselip_2024-06.pgn)
    archive_pattern = re.compile(
        rf"^{re.escape(args.username)}_\d{{4}}-\d{{2}}\.pgn$",
        re.IGNORECASE
    )
    archive_files = [f for f in os.listdir(".") if archive_pattern.match(f)]

    if not archive_files:
        print(f"No monthly archive files found in CWD for user '{args.username}'.")
        print("Expected pattern: <username>_YYYY-MM.pgn (e.g., Mouselip_2024-06.pgn)")
        sys.exit(0)

    print(f"Found {len(archive_files)} archive file(s) to process.")
    if args.dry_run:
        print("[DRY-RUN MODE] No files will be created or modified.\n")

    open_files = {}
    game_counts = {}
    total_games = 0

    try:
        for filename in sorted(archive_files):
            print(f"Processing {filename}...")
            with open(filename, "r", encoding="utf-8") as f:
                while True:
                    try:
                        game = chess.pgn.read_game(f)
                    except Exception:
                        break

                    if game is None:
                        break

                    total_games += 1
                    target_file = get_tc_filename(game)
                    game_counts[target_file] = game_counts.get(target_file, 0) + 1

                    if not args.dry_run:
                        if target_file not in open_files:
                            open_files[target_file] = open(target_file, "w", encoding="utf-8")
                        print(game, file=open_files[target_file], end="\n\n")

    finally:
        for handle in open_files.values():
            handle.close()

    print("\n" + "=" * 50)
    if args.dry_run:
        print("Dry-Run Summary: Files that WOULD be written")
    else:
        print("Processing Complete: Files created/updated")
    print("=" * 50)

    if game_counts:
        for out_file, count in sorted(game_counts.items(), key=lambda x: x[0]):
            print(f"  {out_file:<24} : {count:>5} games")
    else:
        print("  No games found.")

    print("-" * 50)
    print(f"Total Games Processed: {total_games}")
    print("=" * 50)


if __name__ == "__main__":
    main()
