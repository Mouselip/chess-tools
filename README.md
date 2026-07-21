# Chess Tools

A collection of terminal Python scripts for analyzing Chess.com account data, PGN archives, and player statistics.

## Included Tools

* **cc_archive_splitter**: Splits Chess.com monthly archive PGNs into separate categorized PGN files based on time control and game type.
* **chess_archive_parser**: Downloads monthly PGN archives for a given username, extracts unique opponents, queries account statuses, and categorizes closed accounts. Outputs texty and sql friendly csv files
* **wdl-history.py**: Parses game history to generate Win/Draw/Loss trends and breakdown metrics for a given player name.
* **opponent-accuracy.py**: Analyzes opponent play metrics and performance accuracy across archives for a given player name.

## Usage

Clone the repository:

```bash
git clone [https://github.com/Mouselip/chess-tools.git](https://github.com/Mouselip/chess-tools.git)
