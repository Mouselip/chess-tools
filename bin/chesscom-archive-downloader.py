#!/usr/bin/env python3

# chesscom-archive-downloader.py
# Copyright (C) 2026 Tyrin R. Price
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
Usage:
  chesscom-archive-downloader.py <chess.com_username>
  chesscom-archive-downloader.py -h | --help
  chesscom-archive-downloader.py -v | --version

Description:
  Downloads all monthly PGN game archives for a specified Chess.com username.

Output Location:
  - Game PGNs are saved in an 'archives' subdirectory within the current
    working directory (./archives/<username>_YYYY-MM.pgn).

Notes:
  - Archive downloads are cached: if an archive already exists, downloading
    is skipped, EXCEPT for the current calendar month, which is always
    re-downloaded to ensure newly played games are captured.
"""

import sys
import os
import datetime
import requests

__version__ = "0.0.1"
HEADERS = {"User-Agent": "Mozilla/5.0"}


def print_usage_and_exit(code=0):
    print(__doc__.strip())
    sys.exit(code)


def validate_user_exists(username):
    """
    Verify if a Chess.com username exists before proceeding.
    Returns True if valid, False if 404 or missing profile.
    """
    api_url = f"https://api.chess.com/pub/player/{username}"
    try:
        resp = requests.get(api_url, headers=HEADERS)
        if resp.status_code == 404:
            return False
        resp.raise_for_status()
        return True
    except requests.RequestException:
        return False


def get_archive_urls(username):
    """
    Fetch list of archive endpoint URLs for the given user.
    """
    url = f"https://api.chess.com/pub/player/{username}/games/archives"
    try:
        resp = requests.get(url, headers=HEADERS)
        resp.raise_for_status()
        return resp.json().get("archives", [])
    except requests.RequestException as e:
        print(f"❌ HTTP error: {e}")
        return []


def download_pgn(username, archive_url, download_dir, current_month_str):
    """
    Download monthly PGN file. Skips existing files unless it matches current month.
    """
    date_segment = archive_url.split("/")[-2:]
    date_str = "-".join(date_segment)
    filename = f"{username}_{date_str}.pgn"
    filepath = os.path.join(download_dir, filename)

    is_current_month = (date_str == current_month_str)

    if os.path.exists(filepath) and not is_current_month:
        print(f"✅ Already downloaded: {filename}")
        return filepath

    pgn_url = f"{archive_url}/pgn"
    try:
        resp = requests.get(pgn_url, headers=HEADERS)
        resp.raise_for_status()
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(resp.text)
        action_msg = "Updated (current month)" if is_current_month and os.path.exists(filepath) else "Downloaded"
        print(f"⬇️  {action_msg}: {filename}")
        return filepath
    except requests.RequestException as e:
        print(f"❌ Failed to download {pgn_url}: {e}")
        return None


def main():
    if len(sys.argv) == 2 and sys.argv[1] in ("-v", "--version"):
        print(f"chesscom-archive-downloader v{__version__}")
        sys.exit(0)

    if len(sys.argv) != 2 or sys.argv[1] in ("-h", "--help"):
        print_usage_and_exit(
            code=0 if len(sys.argv) == 2 and sys.argv[1] in ("-h", "--help") else 1
        )

    username = sys.argv[1].strip().lower()

    print(f"🔍 Validating Chess.com user '{username}'...")
    if not validate_user_exists(username):
        print(f"❌ Error: Chess.com username '{username}' does not exist.")
        sys.exit(1)

    cwd = os.getcwd()
    archive_dir = os.path.join(cwd, "archives")
    os.makedirs(archive_dir, exist_ok=True)

    current_month_str = datetime.datetime.now().strftime("%Y-%m")

    print(f"ℹ️  PGN archives directory: {archive_dir}")
    print(f"📁 Downloading Chess.com archives for '{username}'...")

    archive_urls = get_archive_urls(username)
    if not archive_urls:
        print(f"⚠️  No archives found for '{username}'.")
        sys.exit(0)

    for url in archive_urls:
        download_pgn(username, url, archive_dir, current_month_str)

    print("\n✅ Archive download process complete.")


if __name__ == "__main__":
    main()
