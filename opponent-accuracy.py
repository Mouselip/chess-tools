#!/usr/bin/env python3
import sys
import time
from datetime import datetime, timezone, timedelta
import requests

# Set a custom User-Agent as required by Chess.com PubAPI guidelines
# https://www.chess.com/news/view/published-data-api
HEADERS = {
    "User-Agent": "OpponentAccuracyTracker/1.0 (Contact: my-script@local.dev)"
}


def fetch_json(url):
    """Fetch JSON with basic error handling and rate-limiting delay."""
    try:
        response = requests.get(url, headers=HEADERS, timeout=10)
        if response.status_code == 429:
            print("[!] Rate limited by Chess.com. Waiting 5 seconds...")
            time.sleep(5)
            return fetch_json(url)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"[!] Request failed for {url}: {e}")
        return None


def get_monthly_archives(username):
    """Retrieve the list of monthly archive URLs for a target player."""
    url = f"https://api.chess.com/pub/player/{username}/games/archives"
    data = fetch_json(url)
    return data.get("archives", []) if data else []


def calculate_metrics(username):
    archives = get_monthly_archives(username)
    if not archives:
        print(f"No archives found for user '{username}'. Check spelling or connection.")
        return

    now = datetime.now(timezone.utc)
    d7 = now - timedelta(days=7)
    d30 = now - timedelta(days=30)
    d90 = now - timedelta(days=90)
    ytd_start = datetime(now.year, 1, 1, tzinfo=timezone.utc)

    # Structure: [sum_accuracy, count_with_accuracy, total_games]
    stats = {
        "7 Days": [0.0, 0, 0],
        "30 Days": [0.0, 0, 0],
        "90 Days": [0.0, 0, 0],
        "Year to Date": [0.0, 0, 0],
        "Yearly": {}  # year -> [sum_accuracy, count_with_accuracy, total_games]
    }

    print(f"Fetching game archives for '{username}'...")

    for archive_url in archives:
        # Avoid unnecessary fetches: parse year/month from URL structure (.../games/YYYY/MM)
        parts = archive_url.rstrip("/").split("/")
        arch_year, arch_month = int(parts[-2]), int(parts[-1])
        arch_start = datetime(arch_year, arch_month, 1, tzinfo=timezone.utc)

        # Skip monthly archives that are older than 90 days AND older than YTD
        if arch_start < (now - timedelta(days=120)) and arch_year < now.year:
            # We still keep old archives if we want complete historical yearly stats.
            # To fetch ALL historical years, we process all archives.
            pass

        data = fetch_json(archive_url)
        if not data or "games" not in data:
            continue

        for game in data["games"]:
            end_time = datetime.fromtimestamp(game["end_time"], tz=timezone.utc)
            year = end_time.year

            if year not in stats["Yearly"]:
                stats["Yearly"][year] = [0.0, 0, 0]

            # Identify opponent color
            is_white = game["white"]["username"].lower() == username.lower()
            opp_color = "black" if is_white else "white"

            # Check for accuracy payload
            accuracies = game.get("accuracies")
            opp_acc = accuracies.get(opp_color) if accuracies else None

            # Helper accumulator
            def accumulate(bucket):
                bucket[2] += 1  # total games
                if opp_acc is not None:
                    bucket[0] += opp_acc  # sum of accuracy
                    bucket[1] += 1        # evaluated games count

            # Populate time window buckets
            if end_time >= d7: accumulate(stats["7 Days"])
            if end_time >= d30: accumulate(stats["30 Days"])
            if end_time >= d90: accumulate(stats["90 Days"])
            if end_time >= ytd_start: accumulate(stats["Year to Date"])
            accumulate(stats["Yearly"][year])

        # Respectful delay between monthly API calls
        time.sleep(0.2)

    return stats


def print_report(username, stats):
    if not stats:
        return

    print("\n" + "=" * 65)
    print(f"  OPPONENT ACCURACY SUMMARY FOR: {username}")
    print("=" * 65)
    print(f"{'Timeframe':<18} | {'Avg Opp Accuracy':<18} | {'Analyzed / Total':<18} | {'Coverage':<8}")
    print("-" * 65)

    def format_row(label, bucket):
        sum_acc, analyzed, total = bucket
        if analyzed > 0:
            avg_acc = f"{sum_acc / analyzed:.2f}%"
            coverage = f"{(analyzed / total) * 100:.1f}%"
        else:
            avg_acc = "N/A"
            coverage = "0.0%"
        counts = f"{analyzed} / {total}"
        return f"{label:<18} | {avg_acc:<18} | {counts:<18} | {coverage:<8}"

    # Print relative periods
    for window in ["7 Days", "30 Days", "90 Days", "Year to Date"]:
        print(format_row(window, stats[window]))

    print("-" * 65)
    print("  YEARLY BREAKDOWN")
    print("-" * 65)

    # Print sorted yearly totals
    for year in sorted(stats["Yearly"].keys(), reverse=True):
        print(format_row(str(year), stats["Yearly"][year]))

    print("=" * 65 + "\n")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        target_user = sys.argv[1]
    else:
        target_user = input("Enter Chess.com username: ").strip()

    if target_user:
        data = calculate_metrics(target_user)
        print_report(target_user, data)
    else:
        print("No username provided. Exiting.")
