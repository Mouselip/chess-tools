#!/usr/bin/env python3
"""
tc-pref.py - Scan a Chess.com player's archives to determine preferred time controls.

Copyright (C) 2026 Tyrin R. Price
This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU General Public License for more details.
"""

import sys
import json
import argparse
import urllib.request
import urllib.error
from collections import Counter

VERSION = "0.0.1"
USER_AGENT = "tc-pref/0.0.1 (Chess.com PubAPI Tool)"


def parse_args():
    parser = argparse.ArgumentParser(
        prog="tc-pref.py",
        description="Scan Chess.com monthly archives to find a player's preferred time control.",
        epilog="Copyright (C) 2026 Tyrin R. Price. Released under the GNU GPL v3.",
        formatter_class=argparse.RawTextHelpFormatter,
    )

    if len(sys.argv) == 1:
        parser.print_help(sys.stderr)
        sys.exit(1)

    parser.add_argument(
        "username",
        help="Target Chess.com username to inspect",
    )
    parser.add_argument(
        "-m", "--months",
        type=int,
        default=3,
        help="Number of recent archive months to check (default: 3)",
    )
    parser.add_argument(
        "--since",
        type=str,
        metavar="YYYY-MM",
        help="Scan archives starting from this month (inclusive)",
    )
    parser.add_argument(
        "--until",
        type=str,
        metavar="YYYY-MM",
        help="Scan archives up to this month (inclusive)",
    )
    parser.add_argument(
        "-c", "--time-class",
        choices=["bullet", "blitz", "rapid", "daily"],
        help="Filter scan strictly to a time class tier",
    )
    parser.add_argument(
        "-u", "--unrated",
        action="store_true",
        help="Filter to unrated games only (default: rated games only)",
    )
    parser.add_argument(
        "-t", "--top",
        type=int,
        default=5,
        help="Show top N time controls (default: 5)",
    )
    parser.add_argument(
        "-q", "--quiet",
        action="store_true",
        help="Print only the winning time control identifier and exit",
    )
    parser.add_argument(
        "-v", "--version",
        action="version",
        version=f"%(prog)s v{VERSION}",
    )

    return parser.parse_args()


def api_request(url):
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
        sys.exit(f"API Error {e.code}: {e.reason} ({url})")
    except urllib.error.URLError as e:
        sys.exit(f```python
#!/usr/bin/env python3
"""tc-pref.py - Parse and analyze time control preferences from Chess.com game archives."""

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

__version__ = "0.0.1"


def parse_time_control(tc_str):
    """Normalize and categorize time control string.

    Format typically: 'base+inc' (e.g. '180+2', '600', '1/86400')
    """
    if not tc_str or tc_str == "-":
        return "Unknown", "Unknown"

    # Daily / Correspondence games
    if "/" in tc_str:
        try:
            moves, seconds = tc_str.split("/")
            days = int(seconds) // 86400
            label = f"{days}d/move" if moves == "1" else f"{moves}m/{days}d"
            return "Daily", label
        except ValueError:
            return "Daily", tc_str

    # Standard real-time games: seconds[+increment]
    parts = tc_str.split("+")
    try:
        base_sec = int(parts[0])
        inc_sec = int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        return "Custom", tc_str

    base_min = base_sec / 60
    base_display = f"{int(base_min)}" if base_min.is_integer() else f"{base_min:.1f}"
    label = f"{base_display}+{inc_sec}" if inc_sec > 0 else f"{base_display} min"

    # Standard chess speed categorization based on estimated total time (base + 40 * inc)
    total_time = base_sec + (40 * inc_sec)
    if total_time < 180:
        category = "Bullet"
    elif total_time < 600:
        category = "Blitz"
    elif total_time < 3600:
        category = "Rapid"
    else:
        category = "Classical"

    return category, label


def process_archive(file_path):
    """Read a JSON archive file or PGN-derived data and extract time control stats."""
    categories = Counter()
    controls = Counter()
    total_games = 0

    path = Path(file_path)
    if not path.exists():
        sys.stderr.write(f"Error: File '{file_path}' not found.\n")
        return None

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError:
        # Fallback line-by-line JSON parsing
        try:
            with open(path, "r", encoding="utf-8") as f:
                games = [json.loads(line) for line in f if line.strip()]
                data = {"games": games}
        except Exception as e:
            sys.stderr.write(f"Error parsing '{file_path}': {e}\n")
            return None
    except Exception as e:
        sys.stderr.write(f"Error reading '{file_path}': {e}\n")
        return None

    games_list = data.get("games", data if isinstance(data, list) else [])

    for game in games_list:
        tc = game.get("time_control") or game.get("time_class")
        if not tc and "pgn" in game:
            # Extract from PGN header if raw PGN exists
            for line in game["pgn"].splitlines():
                if line.startswith('[TimeControl "'):
                    tc = line.split('"')[1]
                    break

        if tc:
            cat, label = parse_time_control(str(tc))
            categories[cat] += 1
            controls[f"{cat:9} {label}"] += 1
            total_games += 1

    return {
        "total": total_games,
        "categories": categories,
        "controls": controls,
    }


def display_results(stats, min_count=1):
    """Format and print frequency tables to stdout."""
    total = stats["total"]
    if total == 0:
        print("No games found with valid time control data.")
        return

    print(f"\nTotal Games Analyzed: {total}\n")
    print(f"{'Category':<12} {'Count':>8} {'Percent':>8}")
    print("-" * 30)
    for cat, count in stats["categories"].most_common():
        pct = (count / total) * 100
        print(f"{cat:<12} {count:>8} {pct:>7.2f}%")

    print("\n" + "=" * 40 + "\n")
    print(f"{'Time Control':<22} {'Count':>8} {'Percent':>8}")
    print("-" * 40)
    for tc, count in stats["controls"].most_common():
        if count >= min_count:
            pct = (count / total) * 100
            print(f"{tc:<22} {count:>8} {pct:>7.2f}%")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Analyze time control preferences from Chess.com archive dumps."
    )
    parser.add_argument("input", help="Path to JSON archive file or exported data")
    parser.add_argument(
        "-m",
        "--min-games",
        type=int,
        default=1,
        help="Minimum games to display individual time control (default: 1)",
    )
    parser.add_argument(
        "-v", "--version", action="version", version=f"%(prog)s {__version__}"
    )

    args = parser.parse_args()

    stats = process_archive(args.input)
    if stats is None:
        sys.exit(1)

    display_results(stats, min_count=args.min_games)


if __name__ == "__main__":
    main()
