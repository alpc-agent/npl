"""Draft state management — log picks, track available players, query state."""

from __future__ import annotations

import json
import unicodedata
from difflib import get_close_matches
from pathlib import Path

from .config import LeagueConfig
from .models import DraftPick, DraftState, Player
from .sheets import DraftSheetReader


class Draft:
    def __init__(
        self,
        players_path: str = "data/players.json",
        state_path: str = "draft_state.json",
        tags_path: str = "data/tags.json",
        config: LeagueConfig | None = None,
    ):
        self.config = config or LeagueConfig()
        self.state_path = Path(state_path)
        self.players: dict[str, Player] = {}
        self._load_players(players_path)
        self._load_tags(tags_path)

        if self.state_path.exists():
            self.state = DraftState.load(self.state_path)
        else:
            self.state = DraftState(
                num_teams=self.config.num_teams,
                draft_type=self.config.draft_type,
            )

    def _load_players(self, path: str) -> None:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for p in data:
            player = Player(
                name=p["name"],
                player_id=p["player_id"],
                team=p["team"],
                age=p["age"],
                positions=p["positions"],
                hitting_projections=p.get("hitting_projections", {}),
                pitching_projections=p.get("pitching_projections", {}),
                adp=p.get("adp", 999),
            )
            self.players[p["name"]] = player

    def _load_tags(self, path: str) -> None:
        """Load player tags from tags.json and attach to Player objects."""
        tags_path = Path(path)
        if not tags_path.exists():
            return
        with open(tags_path, encoding="utf-8") as f:
            data = json.load(f)
        for tag_type in ("rookie", "breakout", "sleeper"):
            for name in data.get(tag_type, []):
                try:
                    player = self._resolve_player(name)
                    if tag_type not in player.tags:
                        player.tags.append(tag_type)
                except ValueError:
                    pass

    def _normalize(self, text: str) -> str:
        nfkd = unicodedata.normalize("NFKD", text)
        return "".join(c for c in nfkd if not unicodedata.combining(c))

    def _resolve_player(self, name: str) -> Player:
        """Find player by exact, case-insensitive, normalized, or fuzzy name match."""
        if name in self.players:
            return self.players[name]
        # Case-insensitive
        for pname, player in self.players.items():
            if pname.lower() == name.lower():
                return player
        # Normalized (accent-insensitive)
        name_norm = self._normalize(name).lower()
        for pname, player in self.players.items():
            if self._normalize(pname).lower() == name_norm:
                return player
        # Fuzzy match
        matches = get_close_matches(name, self.players.keys(), n=1, cutoff=0.6)
        if matches:
            return self.players[matches[0]]
        raise ValueError(f"Player not found: '{name}'. No close matches.")

    def setup(
        self,
        my_team: str,
        draft_position: int,
        team_names: list[str] | None = None,
    ) -> str:
        """Set up the draft with your team info."""
        self.state.my_team = my_team
        self.state.my_draft_position = draft_position
        if team_names:
            self.state.team_names = team_names
        else:
            names = [f"Team {i+1}" for i in range(self.config.num_teams)]
            names[draft_position - 1] = my_team
            self.state.team_names = names
        self._save()
        return f"Draft set up: {my_team} picking at position {draft_position} in a {self.config.num_teams}-team {self.state.draft_type} draft."

    def pick(self, player_name: str, team_name: str | None = None) -> str:
        """Log a draft pick."""
        player = self._resolve_player(player_name)

        if player.player_id in self.state.drafted_player_ids():
            return f"{player.name} has already been drafted!"

        pick_num = self.state.current_pick
        round_num = self.state.current_round
        if team_name is None:
            team_name = self.state.picking_team()

        dp = DraftPick(
            pick_number=pick_num,
            round_number=round_num,
            player_name=player.name,
            player_id=player.player_id,
            team_name=team_name,
        )
        self.state.picks.append(dp)
        self._save()

        is_mine = team_name == self.state.my_team
        marker = " ⭐" if is_mine else ""
        next_info = ""
        picks_until = self.state.picks_until_mine()
        if picks_until is not None and picks_until > 0:
            next_info = f" | Your pick in {picks_until}"
        elif picks_until == 0:
            next_info = " | YOUR PICK IS NOW!"

        return (
            f"Pick {pick_num} (Rd {round_num}): {team_name} takes "
            f"{player.name} ({'/'.join(player.positions)}){marker}{next_info}"
        )

    def undo(self) -> str:
        """Undo the last pick."""
        if not self.state.picks:
            return "No picks to undo."
        removed = self.state.picks.pop()
        self._save()
        return f"Undid pick {removed.pick_number}: {removed.player_name} ({removed.team_name})"

    def available(
        self,
        position: str | None = None,
        limit: int = 20,
        pool: str | None = None,
    ) -> list[Player]:
        """Get available players, optionally filtered by position and pool.

        Args:
            pool: "hitter" or "pitcher" to restrict to that draft pool.
        """
        drafted = self.state.drafted_player_ids()
        avail = [
            p for p in self.players.values()
            if p.player_id not in drafted
            and (p.hitting_projections or p.pitching_projections)
        ]

        if pool == "hitter":
            avail = [p for p in avail if p.is_hitter]
        elif pool == "pitcher":
            avail = [p for p in avail if p.is_pitcher]

        if position:
            pos = position.upper()
            if pos == "IF":
                avail = [p for p in avail if {"C", "1B", "2B", "SS", "3B"} & set(p.positions)]
            elif pos == "OF":
                avail = [p for p in avail if {"LF", "CF", "RF", "OF"} & set(p.positions)]
            else:
                avail = [p for p in avail if pos in p.positions]

        avail.sort(key=lambda p: p.adp)
        return avail[:limit]

    def my_roster(self) -> list[DraftPick]:
        """Get my team's picks."""
        if not self.state.my_team:
            return []
        return self.state.team_picks(self.state.my_team)

    def my_roster_players(self) -> list[Player]:
        """Get Player objects for my roster."""
        picks = self.my_roster()
        return [self.players[p.player_name] for p in picks if p.player_name in self.players]

    def team_roster(self, team_name: str) -> list[DraftPick]:
        """Get a specific team's picks."""
        return self.state.team_picks(team_name)

    def status(self) -> str:
        """Get current draft status."""
        pick = self.state.current_pick
        rd = self.state.current_round
        team = self.state.picking_team()
        drafted = len(self.state.picks)
        total_avail = sum(
            1 for p in self.players.values()
            if p.player_id not in self.state.drafted_player_ids()
            and (p.hitting_projections or p.pitching_projections)
        )

        lines = [
            f"Pick {pick} (Round {rd}) — {team}'s turn",
            f"Drafted: {drafted} | Available: {total_avail}",
        ]

        if self.state.my_team:
            my_picks = self.my_roster()
            picks_until = self.state.picks_until_mine()
            lines.append(f"My team ({self.state.my_team}): {len(my_picks)} players")
            if picks_until == 0:
                lines.append(">>> IT'S YOUR PICK! <<<")
            elif picks_until:
                lines.append(f"Your next pick in: {picks_until} picks")

        return "\n".join(lines)

    def sync_from_sheet(self, sheet_reader: DraftSheetReader) -> dict:
        """Fetch picks from the Google Sheet and apply new ones directly.

        The sheet is the source of truth — picks are applied automatically.

        Returns a dict with:
          - applied: list of confirmation message strings for new picks
          - unmatched: list of (SheetPick, error_msg) that couldn't be resolved
          - already_drafted: count of picks already in our state
        """
        sheet_picks = sheet_reader.fetch_all_picks()

        # Build set of already-known picks (by owner + resolved player name)
        known = set()
        for dp in self.state.picks:
            known.add(f"{dp.team_name}:{dp.player_name}")

        applied = []
        unmatched = []
        already_drafted = 0

        drafted_ids = self.state.drafted_player_ids()
        for sp in sheet_picks:
            try:
                player = self._resolve_player(sp.player_name)
                key = f"{sp.owner}:{player.name}"
                if key in known:
                    already_drafted += 1
                    continue

                if player.player_id in drafted_ids:
                    applied.append(f"  SKIP: {player.name} already drafted")
                    continue

                dp = DraftPick(
                    pick_number=len(self.state.picks) + 1,
                    round_number=sp.round_number,
                    player_name=player.name,
                    player_id=player.player_id,
                    team_name=sp.owner,
                )
                self.state.picks.append(dp)
                drafted_ids.add(player.player_id)
                is_mine = sp.owner == self.state.my_team
                marker = " ⭐" if is_mine else ""
                applied.append(
                    f"  {sp.pick_type.upper()} Rd {sp.round_number}: "
                    f"{sp.owner} takes {player.name} "
                    f"({'/'.join(player.positions)}){marker}"
                )
            except ValueError as e:
                unmatched.append((sp, str(e)))

        if applied:
            self._save()

        return {
            "applied": applied,
            "unmatched": unmatched,
            "already_drafted": already_drafted,
        }

    def threat_window(self, pool: str | None = None) -> list[dict]:
        """Get picks before my next turn with team roster context.

        Returns list of dicts: {pick_number, team_name, positions_filled}
        where positions_filled is a set of positions that team has already drafted.
        """
        picks_before = self.state.picks_before_mine()
        if not picks_before:
            return []

        # Pre-build per-team positions_filled in one pass over all picks
        teams_in_window = {team for _, team in picks_before}
        team_filled: dict[str, set[str]] = {team: set() for team in teams_in_window}

        for dp in self.state.picks:
            if dp.team_name not in teams_in_window:
                continue
            if dp.player_name in self.players:
                p = self.players[dp.player_name]
                # Only count single-position players as definitively filling a slot.
                # Multi-position players are ambiguous — the team might slot them elsewhere.
                if len(p.positions) == 1:
                    team_filled[dp.team_name].add(p.positions[0])
                elif pool == "hitter":
                    hitter_pos = [pos for pos in p.positions if pos not in ("SP", "RP")]
                    if len(hitter_pos) == 1:
                        team_filled[dp.team_name].add(hitter_pos[0])

        return [
            {
                "pick_number": pick_num,
                "team_name": team_name,
                "positions_filled": team_filled[team_name],
            }
            for pick_num, team_name in picks_before
        ]

    def position_runs(self, lookback: int = 6) -> dict[str, int]:
        """Detect position runs in recent picks.

        Returns positions where 2+ players were drafted in the last `lookback` picks.
        """
        recent = self.state.picks[-lookback:] if len(self.state.picks) >= lookback else self.state.picks
        pos_counts: dict[str, int] = {}
        for dp in recent:
            if dp.player_name in self.players:
                for pos in self.players[dp.player_name].positions:
                    if pos not in ("OF", "DH"):  # Skip umbrella/generic positions
                        pos_counts[pos] = pos_counts.get(pos, 0) + 1
        return {pos: count for pos, count in pos_counts.items() if count >= 2}

    def _save(self) -> None:
        self.state.save(self.state_path)
