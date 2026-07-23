# Chess Tools

A collection of terminal Python scripts for analyzing Chess.com account data, PGN archives, and player statistics.

## Included Tools

* **cc_archive_splitter**: Splits Chess.com monthly archive PGNs into separate categorized PGN files based on time control and game type.
* **chess-perf-eval.py**: Aanalyzes player games against local Stockfish engine baselines using direct opponent harvesting, filtered ACPL stats, and empirical Z-score statistical diagnostics.
* **chess_archive_parser**: Downloads monthly PGN archives for a given username, extracts unique opponents, queries account statuses, and categorizes closed accounts. Outputs text and sql friendly csv files
* **opponent-accuracy.py**: Analyzes opponent play metrics and performance accuracy across archives for a given player name.
* **wdl-history.py**: Parses game history to generate Win/Draw/Loss trends and breakdown metrics for a given player name.

## Usage

Clone the repository:

```bash
git clone [https://github.com/Mouselip/chess-tools.git](https://github.com/Mouselip/chess-tools.git)
