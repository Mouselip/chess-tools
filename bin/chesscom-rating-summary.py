#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright (c) 2026 Tyrin R. Price
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
#
# Description:
#   Scans the Chess.com public archives for a target player, filtering
#   for rated games in the bullet, blitz, and rapid categories. Reports
#   game counts, latest rating, and last played date per category, and
#   calculates the rating spread between the highest and lowest categories.
#
# Version: v0.0.1

import argparse
import datetime
import json
import sys
import urllib.error
import urllib.request

HEADERS = {
    "User-Agent": "chesscom-rating-summary/0.0.1 (Contact: GitHub/Mouselip)"
}

TARGET_CATEGORIES = ("bullet", "blitz", "rapid")


def fetch_json(url):
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(f"[-] Error: Resource not found at {url}", file=sys.stderr)
        else:
            print(f"[-] HTTP Error {e.code}: {e.reason}", file=sys.stderr)
    except urllib.error.URLError as e:
        print(f"[-] URL Error: {e.reason}", file=sys.stderr)
    except Exception as e:
        print(f"[-] Unexpected error: {e}", file=sys.stderr)
    return None


def main():
    parser = argparse.ArgumentParser(
        description="Scan Chess.com archives for rated Bullet, Blitz, and Rapid games."
    )
    parser.add_argument("username", help="Chess.com target username")
    args = parser.parse_args()

    username = args.username.strip()
    username_lower = username.lower()

    archives_url = f"https://api.chess.com/pub/player/{username_lower}/games/archives"
    archives_data = fetch_json(archives_url)

    if not archives_data or "archives" not in archives_data:
        print(f"[-] Failed to retrieve archives for player: {username}", file=sys.stderr)
        sys.exit(1)

    archive_urls = archives_data.get("archives", [])
    if not archive_urls:
        print(f"[-] No game archives found for {username}.")
        sys.exit(0)

    # Category tracking dictionary
    stats = {
        cat: {
            "count": 0,
            "latest_rating": None,
            "last_played_ts": 0,
        }
        for cat in TARGET_CATEGORIES
    }

    total_months = len(archive_urls)
    print(f"[*] Scanning {total_months} monthly archives for '{username}'...")

    for idx, month_url in enumerate(archive_urls, start=1):
        month_data = fetch_json(month_url)
        if not month_data:
            continue

        for game in month_data.get("games", []):
            if not game.get("rated", False):
                continue

            time_class = game.get("time_class", "").lower()
            if time_class not in stats:
                continue

            white = game.get("white", {})
            black = game.get("black", {})

            white_user = white.get("username", "").lower()
            black_user = black.get("username", "").lower()

            if white_user == username_lower:
                player_rating = white.get("rating")
            elif black_user == username_lower:
                player_rating = black.get("rating")
            else:
                continue

            end_time = game.get("end_time", 0)

            stats[time_class]["count"] += 1
            if end_time >= stats[time_class]["last_played_ts"]:
                stats[time_class]["last_played_ts"] = end_time
                stats[time_class]["latest_rating"] = player_rating

    # Report Output
    print("\n" + "=" * 62)
    print(f" RATED RATING SUMMARY: {username}")
    print("=" * 62)
    print(f"{'Category':<10} | {'Games':<8} | {'Rating':<8} | {'Last Played (UTC)':<20}")
    print("-" * 62)

    active_categories = {}
    for cat in TARGET_CATEGORIES:
        data = stats[cat]
        count = data["count"]
        rating = data["latest_rating"]
        ts = data["last_played_ts"]

        if count > 0 and rating is not None:
            dt_str = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            rating_str = str(rating)
            active_categories[cat] = rating
        else:
            dt_str = "N/A"
            rating_str = "N/A"

        print(f"{cat.capitalize():<10} | {count:<8} | {rating_str:<8} | {dt_str:<20}")

    print("-" * 62)

    if len(active_categories) >= 2:
        highest_cat = max(active_categories, key=active_categories.get)
        lowest_cat = min(active_categories, key=active_categories.get)
        highest_rating = active_categories[highest_cat]
        lowest_rating = active_categories[lowest_cat]
        spread = highest_rating - lowest_rating

        print(f"Highest Rated Category : {highest_cat.capitalize()} ({highest_rating})")
        print(f"Lowest Rated Category  : {lowest_cat.capitalize()} ({lowest_rating})")
        print(f"Rating Difference      : {spread} points")
    elif len(active_categories) == 1:
        cat, rating = next(iter(active_categories.items()))
        print(f"Only one active rated category found: {cat.capitalize()} ({rating}). Spread not applicable.")
    else:
        print("No rated games found in bullet, blitz, or rapid categories.")

    print("=" * 62 + "\n")


if __name__ == "__main__":
    main()
