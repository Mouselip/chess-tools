#!/usr/bin/env python3
import sys
import time
from datetime import datetime, timezone, timedelta
import requests

# Set a custom User-Agent as required by Chess.com PubAPI guidelines
HEADERS = {
    "User-Agent": "OpponentAccuracyTracker/1.0 (Contact: my-script@local.dev)"
}

TIME_CLASSES = ["daily", "bullet", "blitz", "rapid"]


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


def classify_game(time_control):
    """Classify game matching the cc_archive_splitter logic."""
    tc = str(time_control).strip() if time_control else ""

    if not tc or tc in ("-", "?"):
        return None

    try:
        if tc.startswith("1/"):
            return "daily"

        if "+" in tc:
            base, inc = map(int, tc.split("+")[:2])
        else:
            base = int(tc)
            inc = 0

        estimated = base + 40 * inc

        if estimated < 180:
            return "bullet"
        elif estimated < 600:
            return "blitz"
        else:
            return "rapid"

    except Exception:
        return None


def default_bucket():
    # [player_sum_acc, opp_sum_acc, analyzed_games_count, total_games]
    return [0.0, 0.0, 0, 0]


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

    # Buckets structure: timeframe -> time_class -> [p_sum, opp_sum, analyzed, total]
    timeframes = ["7 Days", "30 Days", "90 Days", "Year to Date"]
    stats = {tf: {tc: default_bucket() for tc in TIME_CLASSES} for tf in timeframes}
    stats["Yearly"] = {}  # year -> time_class -> default_bucket()

    for idx, archive_url in enumerate(archives, 1):
        print(f"\rFetching month {idx}/{total_months}...", end="", flush=True)

        data = fetch_json(archive_url)
        if not data or "games" not in data:
            continue

        for game in data["games"]:
            time_control = game.get("time_control")
            kind = classify_game(time_control)

            if not kind or kind not in TIME_CLASSES:
                continue

            end_time = datetime.fromtimestamp(game["end_time"], tz=timezone.utc)
            year = end_time.year

            if year not in stats["Yearly"]:
                stats["Yearly"][year] = {tc: default_bucket() for tc in TIME_CLASSES}

            is_white = game["white"]["username"].lower() == username.lower()
            player_color = "white" if is_white else "black"
            opp_color = "black" if is_white else "white"

            accuracies = game.get("accuracies")
            player_acc = accuracies.get(player_color) if accuracies else None
            opp_acc = accuracies.get(opp_color) if accuracies else None

            def accumulate(bucket):
                bucket[3] += 1  # total games
                if player_acc is not None and opp_acc is not None:
                    bucket[0] += player_acc
                    bucket[1] += opp_acc
                    bucket[2] += 1  # analyzed games

            if end_time >= d7: accumulate(stats["7 Days"][kind])
            if end_time >= d30: accumulate(stats["30 Days"][kind])
            if end_time >= d90: accumulate(stats["90 Days"][kind])
            if end_time >= ytd_start: accumulate(stats["Year to Date"][kind])
            accumulate(stats["Yearly"][year][kind])

    print("\nProcessing complete!\n")
    return stats


def print_report(username, stats):
    if not stats:
        return

    # Column widths
    W_CLASS = 18
    W_PAVG  = 16
    W_OAVG  = 16
    W_COUNT = 16
    TOTAL_WIDTH = W_CLASS + W_PAVG + W_OAVG + W_COUNT + 9  # account for ' | ' separators

    print("\n" + "=" * TOTAL_WIDTH)
    print(f"  ACCURACY SUMMARY FOR: {username}")
    print("=" * TOTAL_WIDTH)

    def print_section(section_label, tc_dict):
        # Filter down to classes that have at least 1 game in this window
        active_classes = [tc for tc in TIME_CLASSES if tc_dict[tc][3] > 0]
        if not active_classes:
            return

        print(f"\n{section_label}")
        print("-" * TOTAL_WIDTH)
        print(f"{'':<{W_CLASS}} | {'Player Avg Acc':<{W_PAVG}} | {'Opp Avg Acc':<{W_OAVG}} | {'Analyzed / Total':<{W_COUNT}}")
        print("-" * TOTAL_WIDTH)

        for tc in active_classes:
            p_sum, opp_sum, analyzed, total = tc_dict[tc]
            if analyzed > 0:
                p_avg = f"{p_sum / analyzed:.2f}%"
                opp_avg = f"{opp_sum / analyzed:.2f}%"
            else:
                p_avg = "N/A"
                opp_avg = "N/A"

            counts = f"{analyzed} / {total}"
            print(f"  {tc.capitalize():<{W_CLASS - 2}} | {p_avg:<{W_PAVG}} | {opp_avg:<{W_OAVG}} | {counts:<{W_COUNT}}")

    # Print relative periods
    for window in ["7 Days", "30 Days", "90 Days", "Year to Date"]:
        print_section(window, stats[window])

    # Print Yearly Breakdown
    if stats["Yearly"]:
        print("\n\n" + "=" * TOTAL_WIDTH)
        print("  YEARLY BREAKDOWN")
        print("=" * TOTAL_WIDTH)

        for year in sorted(stats["Yearly"].keys(), reverse=True):
            print_section(str(year), stats["Yearly"][year])

    print()


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
