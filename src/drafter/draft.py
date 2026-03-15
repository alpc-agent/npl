"""Draft state management — log picks, track available players, query state."""

from __future__ import annotations

import json
from difflib import get_close_matches
from pathlib import Path

from .config import LeagueConfig
from .models import DraftPick, DraftState, Player


class Draft:
    def __init__(
        self,
        players_path: str = "data/players.json",
        state_path: str = "draft_state.json",
        config: LeagueConfig | None = None,
    ):
        self.config = config or LeagueConfig()
        self.state_path = Path(state_path)
        self.players: dict[str, Player] = {}
        self._load_players(players_path)

        if self.state_path.exists():
            self.state = DraftState.load(self.state_path)
        else:
            self.state = DraftState(
                num_teams=self.config.num_teams,
                draft_type=self.config.draft_type,
            )

    def _load_players(self, path: str) -> None:
        with open(path) as f:
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

    def _resolve_player(self, name: str) -> Player:
        """Find player by exact or fuzzy name match."""
        if name in self.players:
            return self.players[name]
        # Case-insensitive
        for pname, player in self.players.items():
            if pname.lower() == name.lower():
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
    ) -> list[Player]:
        """Get available players, optionally filtered by position."""
        drafted = self.state.drafted_player_ids()
        avail = [
            p for p in self.players.values()
            if p.player_id not in drafted
            and (p.hitting_projections or p.pitching_projections)
        ]

        if position:
            pos = position.upper()
            if pos == "CI":
                avail = [p for p in avail if {"1B", "3B"} & set(p.positions)]
            elif pos == "MI":
                avail = [p for p in avail if {"2B", "SS"} & set(p.positions)]
            elif pos == "P":
                avail = [p for p in avail if {"SP", "RP"} & set(p.positions)]
            elif pos == "BENCH":
                pass  # Any player
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

    def _save(self) -> None:
        self.state.save(self.state_path)
