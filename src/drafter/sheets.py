"""Read draft picks from the NPL Draft Google Sheet."""

from __future__ import annotations

import csv
import io
import urllib.request
from dataclasses import dataclass


@dataclass
class SheetPick:
    owner: str
    round_number: int
    player_name: str
    pick_type: str  # "hitter" or "pitcher"


def fetch_sheet_csv(sheet_id: str, gid: str) -> list[list[str]]:
    """Fetch a Google Sheet tab as CSV rows."""
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"
    resp = urllib.request.urlopen(url, timeout=30)
    data = resp.read().decode("utf-8")
    reader = csv.reader(io.StringIO(data))
    return list(reader)


def parse_selections_tab(rows: list[list[str]], pick_type: str) -> list[SheetPick]:
    """Parse a Selections tab (Hitter or Pitcher) into picks.

    Layout:
      Row 0: header — ["Owner", "1", "2", "3", ...]  (round numbers)
      Rows 1-12: one row per owner — [owner_name, pick_rd1, pick_rd2, ...]
      Row 13: blank
      Row 14: legend (keepers note)
      Row 16: "Position" header for roster breakdown (ignore)
      Rows 17+: roster breakdown (ignore)

    Any non-empty cell in rows 1-12 with a valid round column is a pick.
    """
    if not rows or len(rows) < 2:
        return []

    header = rows[0]
    # Find round columns: header values that are numeric
    round_cols: dict[int, int] = {}  # col_index -> round_number
    for col_idx, val in enumerate(header):
        val = val.strip()
        if val.isdigit():
            round_cols[col_idx] = int(val)

    picks = []
    for row_idx in range(1, min(len(rows), 14)):  # rows 1-13 (owners)
        row = rows[row_idx]
        if not row or not row[0].strip():
            continue

        owner = row[0].strip()

        for col_idx, round_num in round_cols.items():
            if col_idx >= len(row):
                continue
            player = row[col_idx].strip()
            if player and player not in ("", "N/A", "SKIP"):
                picks.append(SheetPick(
                    owner=owner,
                    round_number=round_num,
                    player_name=player,
                    pick_type=pick_type,
                ))

    return picks


class DraftSheetReader:
    """Reads draft state from the NPL Draft Google Sheet."""

    def __init__(self, sheet_id: str, hitter_gid: str, pitcher_gid: str,
                 owner_aliases: dict[str, str] | None = None):
        self.sheet_id = sheet_id
        self.hitter_gid = hitter_gid
        self.pitcher_gid = pitcher_gid
        self.owner_aliases = owner_aliases or {}  # e.g. {"Robocop": "Muppy"}

    def _resolve_owner(self, name: str) -> str:
        """Map sheet owner name to canonical name via aliases."""
        return self.owner_aliases.get(name, name)

    def fetch_all_picks(self) -> list[SheetPick]:
        """Fetch all picks from both Hitter and Pitcher Selections tabs."""
        hitter_rows = fetch_sheet_csv(self.sheet_id, self.hitter_gid)
        pitcher_rows = fetch_sheet_csv(self.sheet_id, self.pitcher_gid)

        hitter_picks = parse_selections_tab(hitter_rows, "hitter")
        pitcher_picks = parse_selections_tab(pitcher_rows, "pitcher")

        all_picks = hitter_picks + pitcher_picks
        for pick in all_picks:
            pick.owner = self._resolve_owner(pick.owner)
        return all_picks

    def get_owners(self) -> list[str]:
        """Get the list of owner names from the sheet."""
        rows = fetch_sheet_csv(self.sheet_id, self.hitter_gid)
        owners = []
        for row_idx in range(1, min(len(rows), 14)):
            if rows[row_idx] and rows[row_idx][0].strip():
                owners.append(self._resolve_owner(rows[row_idx][0].strip()))
        return owners

    def diff(self, known_picks: set[str]) -> list[SheetPick]:
        """Return only picks not already in the known set.

        known_picks should be a set of "owner:player_name" strings.
        """
        all_picks = self.fetch_all_picks()
        new_picks = []
        for pick in all_picks:
            key = f"{pick.owner}:{pick.player_name}"
            if key not in known_picks:
                new_picks.append(pick)
        return new_picks
