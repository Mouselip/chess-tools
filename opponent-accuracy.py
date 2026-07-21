#!/usr/bin/env python3
import sys
import time
from datetime import datetime, timezone, timedelta
import requests

# Set a custom User-Agent as required by Chess.com PubAPI guidelines
HEADERS = {
    "User-Agent": "OpponentAccuracyTracker/1.0 (Contact: my-script@local.dev)"
}


def fetch_json(url):
    """Fetch JSON with basic error handling and rate-limiting delay."""
    try:
        response = requests.get(url, headers=HEADERS, timeout=10)
        if response.status_code == 429:
            print("\n[!] Rate limited by Chess.com. Waiting 5 seconds...")
            time.sleep(5)
            return fetch_json(url)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"\n[!] Request failed for {url}: {e}")
        return None


def get_monthly_archives(username):
    """Retrieve the list of monthly archive URLs for a target player."""
    url = f"https://api.chess.com/pub/player/{username}/games/archives"
    print(f"Fetching archive index for '{username}'...")
    data = fetch_json(url)
    return data.get("archives", []) if data else []


def calculate_metrics(username):
    archives = get_monthly_archives(username)
    if not archives:
        print(f"No archives found for user '{username}'. Check spelling or connection.")
        return None

    total_months = len(archives)
    print(f"Found {total_months} monthly archive(s). Processing games...\n")

    now = datetime.now(timezone.utc)
    d7 = now - timedelta(days=7)
    d30 = now - timedelta(days=30)
    d90 = now - timedelta(days=90)
    ytd_start = datetime(now.year, 1, 1, tzinfo=timezone.utc)

    # Structure: [player_sum_acc, opp_sum_acc, analyzed_games_count, total_games]
    def default_bucket():
        return [0.0, 0.0, 0, 0]

    stats = {
        "7 Days": default_bucket(),
        "30 Days": default_bucket(),
        "90 Days": default_bucket(),
        "Year to Date": default_bucket(),
        "Yearly": {}  # year -> bucket
    }

    for idx, archive_url in enumerate(archives, 1):
        print(f"\rFetching month {idx}/{total_months}...", end="", flush=True)

        data = fetch_json(archive_url)
        if not data or "games" not in data:
            continue

        for game in data["games"]:
            end_time = datetime.fromtimestamp(game["end_time"], tz=timezone.utc)
            year = end_time.year

            if year not in stats["Yearly"]:
                stats["Yearly"][year] = default_bucket()

            # Identify player color
            is_white = game["white"]["username"].lower() == username.lower()
            player_color = "white" if is_white else "black"
            opp_color = "black" if is_white else "white"

            # Check for accuracy payload
            accuracies = game.get("accuracies")
            player_acc = accuracies.get(player_color) if accuracies else None
            opp_acc = accuracies.get(opp_color) if accuracies else None

            # Helper accumulator
            def accumulate(bucket):
                bucket[3] += 1  # total games count
                # Only count analyzed games where BOTH accuracies exist
                if player_acc is not None and opp_acc is not None:
                    bucket[0] += player_acc
                    bucket[1] += opp_acc
                    bucket[2] += 1  # evaluated games count

            # Populate time window buckets
            if end_time >= d7: accumulate(stats["7 Days"])
            if end_time >= d30: accumulate(stats["30 Days"])
            if end_time >= d90: accumulate(stats["90 Days"])
            if end_time >= ytd_start: accumulate(stats["Year to Date"])
            accumulate(stats["Yearly"][year])

    print("\nProcessing complete!\n")
    return stats


def print_report(username, stats):
    if not stats:
        return

    print("\n" + "=" * 78)
    print(f"  ACCURACY SUMMARY FOR: {username}")
    print("=" * 78)
    print(f"{'Timeframe':<16} | {'Player Avg Acc':<16} | {'Opp Avg Acc':<16} | {'Analyzed / Total':<18}")
    print("-" * 78)

    def format_row(label, bucket):
        p_sum, opp_sum, analyzed, total = bucket
        if analyzed > 0:
            p_avg = f"{p_sum / analyzed:.2f}%"
            opp_avg = f"{opp_sum / analyzed:.2f}%"
        else:
            p_avg = "N/A"
            opp_avg = "N/A"
        counts = f"{analyzed} / {total}"
        return f"{label:<16} | {p_avg:<16} | {opp_avg:<16} | {counts:<18}"

    # Print relative periods
    for window in ["7 Days", "30 Days", "90 Days", "Year to Date"]:
        print(format_row(window, stats[window]))

    print("-" * 78)
    print("  YEARLY BREAKDOWN")
    print("-" * 78)

    # Print sorted yearly totals
    for year in sorted(stats["Yearly"].keys(), reverse=True):
        print(format_row(str(year), stats["Yearly"][year]))

    print("=" * 78 + "\n")


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
