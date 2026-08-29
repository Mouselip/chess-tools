#!/usr/bin/env python3
"""
tc-pref.py - Scan a target player's Chess.com archives to find their most preferred time control.

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

import argparse
from collections import Counter
import json
import re
import sys
import urllib.error
import urllib.request

VERSION = "1.0.2"
USER_AGENT = "tc-pref/1.0.2"


def parse_args():
    parser = argparse.ArgumentParser(
        prog="tc-pref.py",
        usage="%(prog)s [OPTIONS] USERNAME",
        description="Scan a target player's Chess.com archives to find their most preferred time control.",
        epilog="Copyright (C) 2026 Tyrin R. Price. Released under GNU GPL v3.",
        formatter_class=argparse.RawTextHelpFormatter,
    )

    parser.add_argument(
        "username",
        help="Target Chess.com username",
    )
    parser.add_argument(
        "-m",
        "--months",
        type=int,
        default=None,
        help="Number of most recent archive months to check (default: all)",
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
        "-u",
        "--unrated",
        action="store_true",
        help="Filter to unrated games only (default: rated games only)",
    )
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"%(prog)s v{VERSION}",
    )

    if len(sys.argv) == 1:
        parser.print_help(sys.stderr)
        sys.exit(1)

    args = parser.parse_args()

    if args.months is not None and args.months <= 0:
        parser.error("-m/--months must be greater than 0")

    return args


def fetch_json(url):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        sys.stderr.write(f"HTTP Error {e.code}: {e.reason} ({url})\n")
        sys.exit(1)
    except urllib.error.URLError as e:
        sys.stderr.write(f"Network error: {e.reason}\n")
        sys.exit(1)


def verify_username(username):
    url = f"https://api.chess.com/pub/player/{username.lower()}"
    data = fetch_json(url)
    if not data or "username" not in data:
        sys.stderr.write(f"Error: Username '{username}' not found on Chess.com.\n")
        sys.exit(1)
    return data["username"]


def filter_archives(archive_urls, since, until, months):
    pattern = re.compile(r"/(\d{4})/(\d{2})$")
    extracted = []

    for url in archive_urls:
        match = pattern.search(url)
        if match:
            ym = f"{match.group(1)}-{match.group(2)}"
            extracted.append((ym, url))

    if since:
        extracted = [item for item in extracted if item[0] >= since]
    if until:
        extracted = [item for item in extracted if item[0] <= until]

    if not since and not until and months is not None:
        extracted = extracted[-months:]

    return [url for _, url in extracted]


def format_tc(tc_str):
    if "/" in tc_str:
        return tc_str
    if "+" in tc_str:
        base, inc = tc_str.split("+", 1)
        try:
            return f"{int(base)//60}+{inc}"
        except ValueError:
            return tc_str
    try:
        secs = int(tc_str)
        if secs % 60 == 0:
            return f"{secs // 60} min"
        return f"{secs}s"
    except ValueError:
        return tc_str


def has_setup_tag(pgn):
    if not pgn:
        return False
    for line in pgn.splitlines():
        if line.startswith("[SetUp "):
            return True
        if line.startswith("1. "):
            break
    return False


def main():
    args = parse_args()
    username = verify_username(args.username)

    archives_url = f"https://api.chess.com/pub/player/{username.lower()}/games/archives"
    archives_data = fetch_json(archives_url)

    if not archives_data or not archives_data.get("archives"):
        print(f"No game archives found for {username}.")
        sys.exit(0)

    target_archives = filter_archives(
        archives_data["archives"], args.since, args.until, args.months
    )

    if not target_archives:
        print("No archives matched the specified date range.")
        sys.exit(0)

    tc_counts = Counter()
    total_matched_games = 0
    rated_target = not args.unrated
    game_mode_label = "Unrated" if args.unrated else "Rated"
    total_archives = len(target_archives)

    ym_pattern = re.compile(r"/(\d{4})/(\d{2})$")

    for idx, url in enumerate(target_archives, start=1):
        match = ym_pattern.search(url)
        ym_label = f"{match.group(1)}-{match.group(2)}" if match else url
        sys.stderr.write(f"[{idx}/{total_archives}] Fetching {ym_label}...\n")

        month_data = fetch_json(url)
        if not month_data:
            continue
        for game in month_data.get("games", []):
            if game.get("rated") != rated_target:
                continue
            if has_setup_tag(game.get("pgn", "")):
                continue
            tc = game.get("time_control")
            if tc:
                tc_counts[tc] += 1
                total_matched_games += 1

    if not tc_counts:
        print(f"\nNo {game_mode_label.lower()} games found in the selected archives.")
        sys.exit(0)

    print(f"\nPlayer: {username}")
    print(f"Mode: {game_mode_label} Games")
    print(f"Scanned Archives: {total_archives} month(s)")
    print(f"Total Games Analyzed: {total_matched_games}\n")

    for tc, count in tc_counts.most_common():
        pct = (count / total_matched_games) * 100
        formatted = format_tc(tc)
        print(f"{tc:<10} ({formatted:<8}) : {count:>5} games ({pct:>5.1f}%)")


if __name__ == "__main__":
    main()
