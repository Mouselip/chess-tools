# Chess Tools

A collection of terminal Python scripts for analyzing Chess.com account data, PGN archives, and player statistics.

NOTE: I "shebang" all of my scripts with "#!/usr/bin/env python3"
This is so I can put them in my path and just run them from anywhere. If you do not use a separate environment for python then you may want to remove the shebang and run them with "python <script-name>"

## Included Tools

* **cc_archive_splitter**: Splits Chess.com monthly archive PGNs into separate categorized PGN files based on time control and game type.
* **chess-perf-eval.py**: Analyzes player games against local Stockfish engine baselines using direct opponent harvesting, filtered ACPL stats, and empirical Z-score statistical diagnostics.
* **chess_archive_parser**: Downloads monthly PGN archives for a given username, extracts unique opponents, queries account statuses, and categorizes closed accounts. Outputs text and sql friendly csv files
* **clock-check.py**: A terminal-native chess clock anomaly analyzer that evaluates move-time pacing,low-variance streaks, instant response ratios, and time distribution anomalies for a target player using public API game PGNs.
* **opponent-accuracy.py**: Analyzes opponent play metrics and performance accuracy across archives for a given player name.
* **wdl-history.py**: Parses game history to generate Win/Draw/Loss trends and breakdown metrics for a given player name.

## Usage

Clone the repository:

```bash
git clone [https://github.com/Mouselip/chess-tools.git](https://github.com/Mouselip/chess-tools.git)
